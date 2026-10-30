#!/usr/bin/env python3
"""Add a folder of images or videos to kura from the command line.

    python3 scripts/kura_import.py --url http://127.0.0.1:8001 \
        --email you@example.org --key-file ~/kura-admin.key ~/Pictures/scans \
        [--tags "scan 2024"] [--safety safe] [--pool "Scans 2024" | --pool-per-folder] [--source URL]

It signs in with your email and sign-in key (the file create_admin.py wrote), sends every
image and video under the folder to /api/upload in natural order (page_2 before page_10),
and signs out. Files kura already holds come back as duplicates and are not added twice.
Each file sent is noted in a record file, so a run that was interrupted continues where it
stopped. The record belongs to one import: the same folder, the same kura and the same
options; `--record` names the file, the default is under ~/.local/state/kura/import/.

--url is the address you open kura at (the API on a native install, the web address with
Docker); it must be the final address, a redirect is refused. A worker that already has a
token can pass --token-file instead of signing in: a 0600 file with KURA_USER and KURA_TOKEN
lines, like the one tagger_worker_setup.py writes. Nothing secret is taken on the command line.

Needs only Python 3.10+; no packages.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kura_http_key import _KuraKeyHandler, install_api_key_opener  # noqa: E402  (a machine key, if this node has one)

PROGRESS_EVERY = 25
GIVE_UP_AFTER = 10          # consecutive failed sends: the server is most likely gone
SAFETY = ("safe", "sketchy", "unsafe")
CHUNK = 1 << 20


# ---- what counts as media -------------------------------------------------------------------

def media_type(head: bytes) -> str | None:
    """The media type of a file from its first bytes (the same table the API decides by), or
    None for anything that is not an image or video kura takes."""
    if len(head) < 12 or not any(head[:8]):
        return None                     # empty, truncated or zero-filled
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    return None


def natural_key(name: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def plan(folder: Path) -> tuple[list[Path], int]:
    """Every media file under `folder`, folders and files in natural order; and how many
    other files were passed over."""
    files: list[Path] = []
    skipped = 0

    def walk(d: Path) -> None:
        nonlocal skipped
        entries = sorted(d.iterdir(), key=lambda p: natural_key(p.name))
        for p in [e for e in entries if e.is_file()] + [e for e in entries if e.is_dir()]:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                walk(p)
                continue
            with open(p, "rb") as f:
                if media_type(f.read(12)) is None:
                    skipped += 1
                    continue
            files.append(p)
    walk(folder)
    return files, skipped


# ---- credentials ----------------------------------------------------------------------------

def private_file(path: Path) -> str:
    """The contents of a file only its owner can read; anything else is refused."""
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SystemExit(f"{path}: must be a regular file you own with mode 0600")
    return path.read_text()


def env_lines(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"\s*(?:export\s+)?([A-Z_][A-Z_0-9]*)\s*=\s*(.*?)\s*", line)
        if m:
            out[m[1]] = m[2].strip("\"'")
    return out


def token_header(user: str, token: str) -> str:
    import base64
    return "Token " + base64.b64encode(f"{user}:{token}".encode()).decode()


# ---- the API --------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to wherever it points; --url must be the final address."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FileChanged(OSError):
    """The file shrank while it was being sent: what went out is not the file."""


class Api:
    def __init__(self, url: str, ca_file: str | None):
        self.url = url.rstrip("/")
        install_api_key_opener()            # wraps urlopen; installs its own opener, replaced next
        handlers = [_NoRedirect(), _KuraKeyHandler()]
        if ca_file:
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        # One installed opener carries all three; `context=` is never passed to urlopen, which
        # would make urlopen build a fresh opener without them.
        urllib.request.install_opener(urllib.request.build_opener(*handlers))
        self.authorization: str | None = None
        self.signed_in = False

    def call(self, method: str, path: str, body=None, headers: dict | None = None,
             timeout: float = 60.0) -> tuple[int, dict]:
        h = {"accept": "application/json", **(headers or {})}
        if self.authorization:
            h["authorization"] = self.authorization
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=h)
        try:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, _json(r.read())
            except urllib.error.HTTPError as e:
                if 300 <= e.code < 400:
                    return e.code, {"detail": f"redirected to {e.headers.get('location')}: "
                                              f"give --url the final address"}
                return e.code, _json(e.read())    # may itself be cut off: handled below
        except urllib.error.URLError as e:
            if isinstance(e.reason, FileChanged):   # raised by the body, wrapped by urlopen
                raise e.reason from None
            raise
        except http.client.HTTPException as e:      # a truncated or malformed reply, success or error
            raise urllib.error.URLError(f"{type(e).__name__}: {e}") from None

    def sign_in(self, email: str, key: str) -> None:
        status, body = self.call("POST", "/api/auth/login", json.dumps({"email": email, "key": key}).encode(),
                                 {"content-type": "application/json"})
        if status != 200 or "token" not in body:
            raise SystemExit(f"sign-in refused ({status}): {detail(body)}")
        self.authorization = token_header(body["username"], body["token"])
        self.signed_in = True

    def sign_out(self) -> None:
        """Revoke the session token. Says so, instead of raising, when that did not work."""
        if not self.signed_in:
            return
        try:
            status, body = self.call("POST", "/api/auth/logout")
        except (urllib.error.URLError, OSError) as e:
            status, body = 0, {"detail": str(getattr(e, "reason", e))[:200]}
        if status == 200:
            self.signed_in = False
        else:
            say(f"could not sign out ({status or 'no connection'}: {detail(body)}); "
                f"the session token stays valid until it expires (30 days)")

    def upload(self, path: Path, fields: dict[str, str]) -> tuple[int, dict, os.stat_result]:
        """Send `path`; returns the status, the body and the stat of what was actually sent."""
        boundary = uuid.uuid4().hex
        parts = b"".join(
            f"--{boundary}\r\ncontent-disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            for k, v in fields.items() if v)
        with open(path, "rb", buffering=0) as f:    # unbuffered: a shrink is seen, not read from a cache
            st = os.fstat(f.fileno())           # the opened file, not whatever the name points to now
            kind = media_type(f.read(12))
            f.seek(0)
            name = path.name.replace('"', "_")
            head = parts + (f"--{boundary}\r\ncontent-disposition: form-data; name=\"file\"; "
                            f"filename=\"{name}\"\r\ncontent-type: {kind}\r\n\r\n").encode()
            tail = f"\r\n--{boundary}--\r\n".encode()

            def body():
                yield head
                left = st.st_size               # exactly the declared bytes, no more, no fewer
                while left:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        raise FileChanged(f"{path}: shorter than when it was opened")
                    left -= len(chunk)
                    yield chunk
                yield tail
            status, reply = self.call("POST", "/api/upload", body(), {
                "content-type": f"multipart/form-data; boundary={boundary}",
                "content-length": str(len(head) + st.st_size + len(tail))}, timeout=600.0)
            return status, reply, st


def _json(raw: bytes) -> dict:
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        return {"detail": raw[:200].decode(errors="replace")}
    return out if isinstance(out, dict) else {"detail": out}


def detail(body: dict) -> str:
    d = body.get("detail", body)
    if isinstance(d, dict):
        return d.get("fix") or d.get("error") or json.dumps(d)[:200]
    return str(d)[:200]


# ---- the record -----------------------------------------------------------------------------

def import_identity(args, folder: Path) -> dict:
    """What a record belongs to: one folder, one kura, one set of options. A different kura
    or different options is a different import and must not skip what this one sent."""
    return {"folder": str(folder), "url": args.url.rstrip("/"), "tags": args.tags, "safety": args.safety,
            "source": args.source, "pool": args.pool, "pool_per_folder": args.pool_per_folder}


def default_record(identity: dict) -> Path:
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "kura/import" / f"{digest}.jsonl"


def open_record(record: Path, identity: dict, create: bool = True) -> dict[str, tuple[int, int]]:
    """Make sure `record` is this import's, writing its header if it is new (unless `create`
    is off: a dry run only reads); returns relative path → (size, mtime_ns) of every file a
    previous run got an id for."""
    done = {}
    try:
        lines = record.read_text().splitlines() if record.exists() else []
        if not lines:
            if create:
                record.parent.mkdir(parents=True, exist_ok=True)
                with open(record, "a") as f:
                    f.write(json.dumps({"import": identity}, ensure_ascii=False) + "\n")
            return done
    except OSError as e:
        raise SystemExit(f"cannot write the record {record} ({e.strerror}); give --record a writable path")
    try:
        header = json.loads(lines[0]).get("import")
    except (ValueError, AttributeError):
        header = None
    if header != identity:
        raise SystemExit(f"{record} belongs to a different import ({json.dumps(header, ensure_ascii=False)}); "
                         f"give --record another file, or the same folder, --url and options")
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("id") is not None:
            done[row["path"]] = (row.get("size"), row.get("mtime_ns"))
    return done


# ---- main -----------------------------------------------------------------------------------

def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path)
    p.add_argument("--url", required=True, help="the address you open kura at")
    who = p.add_argument_group("who you are (one of)")
    who.add_argument("--key-file", type=Path, help="your sign-in key (with --email)")
    who.add_argument("--email", help="the email you sign in with")
    who.add_argument("--token-file", type=Path, help="a 0600 file with KURA_USER and KURA_TOKEN")
    what = p.add_argument_group("what each file gets")
    what.add_argument("--tags", default="", help="space-separated tags for every file")
    what.add_argument("--safety", choices=SAFETY, default="unsafe")
    what.add_argument("--source", default="", help="a source URL recorded on every file")
    what.add_argument("--pool", default="", help="put every file in this pool, in order")
    what.add_argument("--pool-per-folder", action="store_true",
                      help="each folder that holds files becomes a pool named after it")
    p.add_argument("--record", type=Path, help="where sent files are noted (default under ~/.local/state)")
    p.add_argument("--ca-file", help="the CA certificate to trust for an https --url")
    p.add_argument("--dry-run", action="store_true", help="only count and list; send nothing")
    a = p.parse_args(argv)
    if not a.dry_run and bool(a.key_file) == bool(a.token_file):
        p.error("give --key-file with --email, or --token-file")
    if a.key_file and not a.email:
        p.error("--key-file needs --email")
    if a.pool and a.pool_per_folder:
        p.error("--pool and --pool-per-folder exclude each other")
    if not a.folder.is_dir():
        p.error(f"{a.folder}: not a folder")
    return a


def pool_name(args, folder: Path, path: Path) -> str:
    if args.pool:
        return args.pool
    if args.pool_per_folder:
        rel = path.parent.relative_to(folder)
        return folder.name if rel == Path(".") else " / ".join(rel.parts)
    return ""


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv=None) -> int:
    args = arguments(argv)
    folder = args.folder.resolve()
    files, not_media = plan(folder)
    identity = import_identity(args, folder)
    record = args.record or default_record(identity)
    done = open_record(record, identity, create=not args.dry_run)
    todo = [p for p in files
            if done.get(str(p.relative_to(folder))) != (p.stat().st_size, p.stat().st_mtime_ns)]
    say(f"{folder}: {len(files)} media files, {not_media} other files passed over, "
        f"{len(files) - len(todo)} already sent (record: {record})")
    if args.dry_run:
        for p in todo:
            pool = pool_name(args, folder, p)
            print(f"{p.relative_to(folder)}" + (f"  → pool {pool}" if pool else ""))
        return 0

    api = Api(args.url, args.ca_file)
    if args.token_file:
        env = env_lines(private_file(args.token_file))
        if not env.get("KURA_USER") or not env.get("KURA_TOKEN"):
            raise SystemExit(f"{args.token_file}: needs KURA_USER and KURA_TOKEN")
        api.authorization = token_header(env["KURA_USER"], env["KURA_TOKEN"])
    else:
        key = private_file(args.key_file).strip()
        api.sign_in(args.email, key)

    counts = {"added": 0, "duplicates": 0, "failed": 0, "skipped": len(files) - len(todo),
              "not_media": not_media}
    started = time.monotonic()
    failures_in_a_row = 0
    try:                                        # from here on, sign-out happens whatever else does
        with open(record, "a") as out:
            for n, p in enumerate(todo, 1):
                rel = str(p.relative_to(folder))
                row: dict = {"path": rel}
                try:
                    status, body, st = api.upload(p, {"tags": args.tags, "safety": args.safety,
                                                      "source": args.source,
                                                      "pool": pool_name(args, folder, p)})
                    row.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
                except (urllib.error.URLError, OSError) as e:
                    status, body = 0, {"detail": str(getattr(e, "reason", e))[:200]}
                if status in (200, 202) and "id" in body:
                    row.update(id=body["id"], duplicate=bool(body.get("duplicate")))
                    counts["duplicates" if row["duplicate"] else "added"] += 1
                    failures_in_a_row = 0
                else:
                    row.update(status=status, error=detail(body))
                    counts["failed"] += 1
                    failures_in_a_row += 1
                    say(f"  {rel}: {status or 'no connection'} {row['error']}")
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                if n % PROGRESS_EVERY == 0 or n == len(todo):
                    rate = n / max(time.monotonic() - started, 1e-9)
                    left = (len(todo) - n) / rate if rate else 0
                    say(f"  {n}/{len(todo)} sent, {rate:.1f}/s, about {left / 60:.0f} min left")
                if failures_in_a_row >= GIVE_UP_AFTER:
                    say(f"stopping: {GIVE_UP_AFTER} sends in a row failed")
                    break
    finally:
        api.sign_out()
    counts["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(counts))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
