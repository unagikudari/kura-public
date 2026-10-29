"""Private stdio source custodian used by the Kura credential handoff.

No credentials in argv/stdout logs. JSON stdout is a private pipe to the parent.
The parent closes stdin on any failure; process exit releases all source locks.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class CustodyRefused(Exception):
    pass


def environment(path):
    p = Path(path)
    st = p.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise CustodyRefused("private_environment_required")
    out = {}
    for line in p.read_text().splitlines():
        m = re.fullmatch(r"\s*(?:export\s+)?([A-Z_][A-Z_0-9]*)\s*=\s*(.*?)\s*", line)
        if m:
            out[m[1]] = m[2].strip("\"'")
    return out


def check_files(paths):
    denies, commitments = set(), []
    for name in paths:
        path = Path(name)
        if path.is_symlink() or path.parent.is_symlink():
            raise CustodyRefused("denylist_symlink")
        st, parent = path.stat(), path.parent.stat()
        if (not stat.S_ISREG(st.st_mode) or not stat.S_ISDIR(parent.st_mode)
                or st.st_uid != os.getuid() or parent.st_uid != os.getuid()
                or (st.st_mode | parent.st_mode) & 0o222):
            raise CustodyRefused("denylist_not_frozen")
        data = path.read_bytes()
        value = json.loads(data)
        if (not isinstance(value, dict) or set(value) != {"revoked"}
                or not isinstance(value["revoked"], list)
                or any(not isinstance(x, str) for x in value["revoked"])):
            raise CustodyRefused("denylist_unreadable")
        denies.update(value["revoked"])
        commitments.append((str(path), st.st_dev, st.st_ino, hashlib.sha256(data).hexdigest()))
    return sorted(denies), commitments


def check_writers(config):
    if not config["units"] or not config["denylist_files"] or not config["writer_paths"]:
        raise CustodyRefused("incomplete_source_inventory")
    observed = []
    for unit in config["units"]:
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.(service|timer|path|socket)", unit):
            raise CustodyRefused("invalid_unit")
        cp = subprocess.run(["systemctl", "--user", "show", unit,
                             "--property=ActiveState,UnitFileState"],
                            capture_output=True, text=True, timeout=5, check=True)
        props = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
        if props.get("ActiveState") != "inactive" or props.get("UnitFileState") not in ("masked", "masked-runtime"):
            raise CustodyRefused("source_writer_not_inhibited")
        observed.append({"unit": unit, "active_state": props["ActiveState"],
                         "unit_file_state": props["UnitFileState"]})
    for path in config["writer_paths"]:
        if not Path(path).is_absolute():
            raise CustodyRefused("writer_path_not_absolute")
    # Source CLI entries are inventoried too; root's stopped cut keeps invocation
    # custody. DB locks independently prevent their future SQL writes.
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise CustodyRefused("writer_inventory_unreadable") from None
        if any(os.fsencode(path) in argv for path in config["writer_paths"]):
            raise CustodyRefused("source_cli_still_running")
    return observed


def main():
    import psycopg
    config = json.loads(sys.stdin.readline())
    check_writers(config)
    denies, files = check_files(config["denylist_files"])
    env = environment(config["environment_file"])
    dsn = env.get("DATABASE_URL")
    if not dsn:
        raise CustodyRefused("source_database_not_configured")
    with psycopg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"), connect_timeout=5) as db:
        db.execute("SET TIME ZONE 'UTC'")
        db.execute("SET lock_timeout='5s'")
        db.execute("SET statement_timeout='15s'")
        db.execute("LOCK TABLE human_account IN SHARE MODE")
        rows = db.execute("SELECT email,key_hash,key_issued_at,retired_at FROM human_account ORDER BY email").fetchall()
        rows = [[email, digest, issued.isoformat() if issued else None,
                 retired.isoformat() if retired else None] for email, digest, issued, retired in rows]
        writers = check_writers(config)
        if check_files(config["denylist_files"]) != (denies, files):
            raise CustodyRefused("source_denylist_changed")
        print(json.dumps({"accounts": rows, "denials": denies, "files": files,
                          "backend_pid": db.info.backend_pid, "writers": writers}), flush=True)
        for line in sys.stdin:
            if line.strip() != "check":
                raise CustodyRefused("unknown_custody_command")
            check_writers(config)
            if check_files(config["denylist_files"]) != (denies, files):
                raise CustodyRefused("source_denylist_changed")
            held = db.execute("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() "
                              "AND relation='human_account'::regclass AND mode='ShareLock' AND granted").fetchone()[0]
            if held != 1:
                raise CustodyRefused("source_lock_lost")
            print('{"held":true}', flush=True)
        db.rollback()


if __name__ == "__main__":
    try:
        main()
    except CustodyRefused as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        raise SystemExit(2)
    except Exception as exc:
        # libpq/JSON errors may contain secret values; pipe carries only category.
        print(json.dumps({"error": "source_custody_refused:" + type(exc).__name__}), flush=True)
        raise SystemExit(2)
