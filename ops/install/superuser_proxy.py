#!/usr/bin/env python3
# Description: a short-lived socket that reaches PostgreSQL as the OS user postgres, for
#              `ops/install/database.py upgrade` only (docs/notes/fresh-install-baseline.md)
# Effects: listens on <dir>/.s.PGSQL.<port> (0600, owned by the operator) and forwards each
#          connection to PostgreSQL's socket from a process running as postgres; exits when its
#          stdin closes
"""Started by database.py as `sudo python3 superuser_proxy.py …`, stdlib only.

Peer authentication sees the forwarding process, which is `postgres`, so a connection through
this socket is the superuser — without a password, a role, or anything left in the database.
The socket sits in a directory the operator created 0700 and is chowned to the operator and
chmod 0600 before the process drops root: only the operator (who ran sudo) and root reach it.
The parent holds this process's stdin; when the parent ends, for any reason, stdin reaches EOF
and the proxy removes the socket and exits — an interrupted upgrade leaves nothing behind.
"""
from __future__ import annotations

import argparse
import os
import pwd
import select
import socket
import sys
import threading


def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--listen-dir", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--target", required=True, help="PostgreSQL's socket file")
    p.add_argument("--owner-uid", type=int, required=True)
    p.add_argument("--as-user", help="drop to this OS user after binding (postgres); tests omit it")
    a = p.parse_args()
    path = os.path.join(a.listen_dir, f".s.PGSQL.{a.port}")
    st = os.lstat(a.listen_dir)
    if st.st_uid != a.owner_uid or st.st_mode & 0o077 or not os.path.isdir(a.listen_dir):
        raise SystemExit("the listen directory must be the operator's own 0700 directory")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        listener.bind(path)
    finally:
        os.umask(old)
    os.chown(path, a.owner_uid, -1) if os.geteuid() == 0 else None
    os.chmod(path, 0o600)
    if a.as_user:
        user = pwd.getpwnam(a.as_user)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    listener.listen(8)
    print("ready", flush=True)
    try:
        while True:
            ready, _, _ = select.select([listener, sys.stdin], [], [])
            if sys.stdin in ready and not sys.stdin.buffer.read1(1):
                return 0                                   # the parent is gone or done
            if listener in ready:
                client, _ = listener.accept()
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.connect(a.target)
                for x, y in ((client, upstream), (upstream, client)):
                    threading.Thread(target=pump, args=(x, y), daemon=True).start()
    finally:
        listener.close()
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
