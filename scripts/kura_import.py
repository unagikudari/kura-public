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

With --watch the folder is kept in kura: it is looked at again every --watch seconds and
whatever was added or changed since is sent, once it has stopped changing for --settle
seconds (a file still being copied waits). A file kura refused stays out until it changes.
It runs until stopped (Ctrl+C, or SIGTERM from systemd) and signs in only when there is
something to send.

Needs only Python 3.10+; no packages.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
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


def plan(folder: Path, sent=None, settle: float = 0.0) -> tuple[list[Path], int, int]:
    """Every media file under `folder`, folders and files in natural order; how many other
    files were passed over; and how many are still changing (modified less than `settle`
    seconds ago: not looked into yet). `sent(rel, stat)` says a file needs no look at all
    (already in kura, unchanged), so a watched folder is not read again on every pass."""
    files: list[Path] = []
    skipped = unsettled = 0
    now = time.time()

    def walk(d: Path) -> None:
        nonlocal skipped, unsettled
        try:
            entries = sorted(d.iterdir(), key=lambda p: natural_key(p.name))
        except OSError:                 # removed or unreadable since it was listed
            return
        for p in [e for e in entries if e.is_file()] + [e for e in entries if e.is_dir()]:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                walk(p)
                continue
            try:
                st = p.stat()
                if sent and sent(str(p.relative_to(folder)), st):
                    files.append(p)
                    continue
                if settle and now - st.st_mtime < settle:
                    unsettled += 1
                    continue
                with open(p, "rb") as f:
                    if media_type(f.read(12)) is None:
                        skipped += 1
                        continue
            except OSError:             # gone between the listing and the look
                continue
            files.append(p)
    walk(folder)
    return files, skipped, unsettled


# ---- sidecar files --------------------------------------------------------------------------
#
# Tags that already sit next to the images, in the forms other tools write them:
#   photo.jpg.json / photo.json   a booru post's JSON (keys: tags, tag_string,
#                                 tag_string_artist/_character/_copyright, rating, source)
#   photo.jpg.xmp  / photo.xmp    XMP keywords (dc:subject), as digiKam, darktable or Lightroom write them
#   photo.jpg.txt  / photo.txt    one tag per line (Hydrus sidecars) or comma-separated (training captions)
# Every one present is read and their tags merged.

SIDECAR_SUFFIXES = (".json", ".xmp", ".txt")
NAMESPACES = {"artist": "artist", "creator": "artist", "character": "character",
              "series": "copyright", "copyright": "copyright"}
# Booru ratings, letters and words. Danbooru: g general, s sensitive, q questionable, e explicit.
RATINGS = {"g": "safe", "general": "safe", "safe": "safe",
           "s": "sketchy", "sensitive": "sketchy", "sketchy": "sketchy",
           "q": "unsafe", "questionable": "unsafe", "e": "unsafe", "explicit": "unsafe", "unsafe": "unsafe"}
XMP_LI = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li"
XMP_SUBJECT = "{http://purl.org/dc/elements/1.1/}subject"


def kura_name(tag: str) -> str:
    """A tag name the way kura stores it (upload lowercases, spaces become underscores); kura
    looks names up exactly, so every name compared or looked up goes through this."""
    return "_".join(str(tag).split()).lower()


class Sidecar:
    def __init__(self):
        self.tags: dict[str, str | None] = {}       # kura tag name → category (None: default)
        self.safety: str | None = None
        self.source: str | None = None
        self.files: list[str] = []

    def tag(self, raw: str, category: str | None = None) -> None:
        raw = raw.strip()
        if not raw:
            return
        ns, sep, rest = raw.partition(":")
        if sep and category is None:
            key = ns.strip().lower()
            if key == "rating":
                self.safety = self.safety or RATINGS.get(rest.strip().lower())
                if rest.strip().lower() in RATINGS:
                    return
            if key in NAMESPACES and rest.strip():
                raw, category = rest, NAMESPACES[key]
        name = kura_name(raw)                       # as kura stores it: a later lookup by name must find it
        if name and (category or name not in self.tags):
            self.tags[name] = category or self.tags.get(name)

    def as_fields(self) -> tuple[str, str]:
        """(tags, artist_tags) as /api/upload takes them."""
        return (" ".join(self.tags), " ".join(n for n, c in self.tags.items() if c == "artist"))


def _words(value) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _read_json(sc: Sidecar, data) -> None:
    if not isinstance(data, dict):
        return
    for key, cat in (("tag_string_artist", "artist"), ("tags_artist", "artist"), ("artist", "artist"),
                     ("tag_string_character", "character"), ("tags_character", "character"),
                     ("tag_string_copyright", "copyright"), ("tags_copyright", "copyright")):
        for t in _words(data.get(key)):
            sc.tag(t, cat)
    for key in ("tag_string", "tag_string_general", "tags_general", "tags"):
        value = data.get(key)
        if isinstance(value, dict):             # some extractors group tags by category
            for cat, names in value.items():
                for t in _words(names):
                    sc.tag(t, NAMESPACES.get(str(cat).lower()))
        else:
            for t in _words(value):
                sc.tag(t)
    rating = data.get("rating")
    if isinstance(rating, str):
        sc.safety = sc.safety or RATINGS.get(rating.strip().lower())
    source = data.get("source")
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        sc.source = sc.source or source


def _read_xmp(sc: Sidecar, raw: bytes) -> None:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return
    for subject in root.iter(XMP_SUBJECT):
        for li in subject.iter(XMP_LI):
            if li.text:
                sc.tag(li.text)


def _read_txt(sc: Sidecar, text: str) -> None:
    for line in text.splitlines():
        for part in line.split(","):
            sc.tag(part)


SIDECAR_LIMIT = 1 << 20                     # a sidecar is a few lines; anything bigger is not one


def _read_small(path: Path) -> bytes | None:
    """The bytes of a regular file of at most SIDECAR_LIMIT, read from one descriptor (a file
    that grows or is swapped after a look is still held to the limit); None otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)     # a FIFO put there must not hang us
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(SIDECAR_LIMIT + 1)
        return raw if len(raw) <= SIDECAR_LIMIT else None
    except OSError:
        return None
    finally:
        os.close(fd)


def sidecar_signature(path: Path) -> list:
    """[name, size, mtime_ns] of each sidecar file beside `path`: a record row keeps it, so a
    sidecar added or edited after the image went in sends the image again (kura adds the new
    tags to the post it already has)."""
    out = []
    for q in sidecar_paths(path):
        try:
            st = q.stat()
        except OSError:
            continue
        out.append([q.name, st.st_size, st.st_mtime_ns])
    return out


def sidecar(path: Path) -> Sidecar | None:
    """The tags, safety and source in the sidecar files next to `path`, or None if it has none."""
    sc = Sidecar()
    for suffix in SIDECAR_SUFFIXES:
        for candidate in (path.with_name(path.name + suffix), path.with_suffix(suffix)):
            raw = _read_small(candidate)
            if raw is None:
                continue
            if suffix == ".json":
                try:
                    _read_json(sc, json.loads(raw))
                except ValueError:
                    continue
            elif suffix == ".xmp":
                _read_xmp(sc, raw)
            else:
                _read_txt(sc, raw.decode("utf-8", errors="replace"))
            sc.files.append(candidate.name)
            break                               # photo.jpg.json wins over photo.json
    return sc if sc.files else None


def sidecar_paths(path: Path) -> list[Path]:
    """The sidecar files that exist next to `path` (for the settle check)."""
    out = []
    for suffix in SIDECAR_SUFFIXES:
        for candidate in (path.with_name(path.name + suffix), path.with_suffix(suffix)):
            if candidate.is_file():
                out.append(candidate)
    return out


def fields_for(args, folder: Path, path: Path, categories: dict | None = None) -> dict[str, str]:
    """What /api/upload gets for `path`: the command line's tags, safety and source, and with
    --sidecars what its sidecar files add (their rating and source only where the command line
    gave none). The sidecars' tag categories are collected into `categories`."""
    tags, artists, safety, source = args.tags, "", args.safety or "unsafe", args.source
    sc = sidecar(path) if args.sidecars else None
    if sc is not None:
        more, artists = sc.as_fields()
        tags = " ".join(t for t in (tags, more) if t)
        if args.safety is None and sc.safety:
            safety = sc.safety
        source = source or sc.source or ""
        if categories is not None:
            # artist too: artist_tags names the category only for a tag kura creates now
            categories.update({n: c for n, c in sc.tags.items() if c})
    return {"tags": tags, "safety": safety, "source": source, "pool": pool_name(args, folder, path),
            "artist_tags": artists}


def copy_tag_categories(api: Api, posts: list[dict], counts: dict) -> set[str]:
    """Give tags the kura category the source said they have. `posts` are szurubooru-shaped
    (tags as {"names": [...], "category": ...}); every tag seen on any of them is looked at, so
    an interrupted earlier run leaves nothing behind. Only a tag still in kura's default
    category is changed: a category someone set in kura stays."""
    tag_category: dict[str, str] = {}
    for p in posts:
        for t in p.get("tags", []):
            if t.get("names") and t.get("category"):
                # kura stores names the way upload normalizes them, and looks them up exactly
                tag_category[kura_name(t["names"][0])] = t["category"]
    settled: set[str] = set()
    status, cats = api.call("GET", "/api/tag-categories")
    kura_cats = cats.get("detail") if status == 200 else None
    if not isinstance(kura_cats, list):
        say(f"tag categories not copied: kura answered {status}")
        return settled
    known = {c["name"] for c in kura_cats}
    default = next((c["name"] for c in kura_cats if c.get("default")), "default")
    for name, cat in sorted(tag_category.items()):
        if cat not in known or cat in (default, "default"):
            settled.add(name)                   # nothing kura can do with it
            continue
        quoted = urllib.parse.quote(name, safe="")
        try:
            status, tag = api.call("GET", f"/api/tag/{quoted}")
            if status == 404:
                settled.add(name)               # no such tag in kura: nothing to give a category
                continue
            if status != 200:
                say(f"  tag {name}: category not set (could not read it: {status} {detail(tag)[:100]})")
                continue
            if tag.get("category", default) != default:
                settled.add(name)               # someone gave it one already: that stays
                continue
            # kura's tag update is optimistically locked: send the version just read
            status, reply = api.call("PATCH", f"/api/tag/{quoted}",
                                     json.dumps({"version": tag.get("version", 1), "category": cat}).encode(),
                                     {"content-type": "application/json"})
        except (urllib.error.URLError, OSError) as e:
            say(f"  tag {name}: category not set (no connection: {getattr(e, 'reason', e)})")
            continue
        if status == 200:
            counts["tags_recategorized"] += 1
            settled.add(name)
        else:
            say(f"  tag {name}: category not set ({status} {detail(reply)[:120]})")
    # szurubooru's own default category ("default") is kura's default, whatever kura calls it
    missing = sorted({c for c in tag_category.values() if c not in known and c != "default"})
    if missing:
        say(f"tag categories kura does not have, left at its default: {', '.join(missing)}")
    return settled


def set_categories(api: Api, record: Path, identity: dict, counts: dict) -> None:
    """Give the sidecars' artist, character and copyright tags their kura category (only tags
    still in kura's default category, as the migrations do). The work comes from the record —
    each sent file's row keeps its categories until a "categorized" row says they are done —
    so a run that was cut off, or a category kura would not set, is taken up next time."""
    pending: dict = {}
    open_record(record, identity, categories=pending)
    if not pending:
        return
    counts.setdefault("tags_recategorized", 0)
    settled = copy_tag_categories(
        api, [{"tags": [{"names": [n], "category": c} for n, c in pending.items()]}], counts)
    if settled:
        with open(record, "a") as out:
            out.write(json.dumps({"categorized": sorted(settled)}, ensure_ascii=False) + "\n")


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


class SignInRefused(SystemExit):
    """kura did not sign us in; `status` tells a refusal (4xx) from kura being away (5xx)."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
            raise SignInRefused(status, f"sign-in refused ({status}): {detail(body)}")
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

    def revoke(self, authorization: str) -> bool:
        """Revoke a session token kept from an earlier sign-out that did not get through.
        True once kura no longer takes it (signed out now, or already gone)."""
        req = urllib.request.Request(self.url + "/api/auth/logout", data=b"", method="POST",
                                     headers={"accept": "application/json", "authorization": authorization})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            return e.code == 401
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            return False

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
    identity = {"folder": str(folder), "url": args.url.rstrip("/"), "tags": args.tags,
                "safety": args.safety or "unsafe", "source": args.source, "pool": args.pool,
                "pool_per_folder": args.pool_per_folder}
    if args.sidecars:                   # only then: a record made before --sidecars existed stays this import's
        identity["sidecars"] = True
        # With sidecars, an omitted --safety (each sidecar's rating) and --safety unsafe (all
        # unsafe) send different ratings: they are different imports.
        identity["safety"] = args.safety
    return identity


def default_record(identity: dict) -> Path:
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "kura/import" / f"{digest}.jsonl"


# Refusals that say something about the file itself; anything else (no connection, 5xx, an
# expired sign-in, rate limiting) is tried again.
REFUSED_FOR_GOOD = frozenset(range(400, 500)) - {401, 403, 408, 409, 425, 429}
CATEGORY_RETRY = 600.0          # seconds between --watch passes that only retry tag categories


def file_state(args, path: Path, st: os.stat_result) -> tuple:
    """What a record row is compared with: the file's size and mtime, and with --sidecars the
    state of its sidecar files."""
    return (st.st_size, st.st_mtime_ns, sidecar_signature(path) if args.sidecars else None)


def open_record(record: Path, identity: dict, create: bool = True, refused: dict | None = None,
                retrying: dict | None = None, categories: dict | None = None) -> dict[str, tuple]:
    """Make sure `record` is this import's, writing its header if it is new (unless `create`
    is off: a dry run only reads); returns relative path → (size, mtime_ns) of every file a
    previous run got an id for (with its sidecar state, see file_state). `categories`, if given,
    gets the tag categories sent files' sidecars named that no "categorized" row settled yet.
    `refused`, if given, is filled the same way with the files
    kura last turned down for good (REFUSED_FOR_GOOD), and `retrying` with the paths whose
    last send failed otherwise, each with the line of that attempt (the watch tries them
    after the files not tried yet, the one tried longest ago first)."""
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
    for lineno, line in enumerate(lines[1:], 1):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "categorized" in row:
            if categories is not None:
                for name in row["categorized"]:
                    categories.pop(kura_name(name), None)
            continue
        path = row.get("path")
        if row.get("id") is not None:
            done[path] = (row.get("size"), row.get("mtime_ns"), row.get("sidecars"))
            if categories is not None and isinstance(row.get("categories"), dict):
                # names as kura holds them: a row written by 1.301.0 kept the sidecar's case
                categories.update({kura_name(n): c for n, c in row["categories"].items()})
            if refused is not None:
                refused.pop(path, None)
            if retrying is not None:
                retrying.pop(path, None)
        elif refused is not None and row.get("status") in REFUSED_FOR_GOOD and "size" in row:
            refused[path] = (row.get("size"), row.get("mtime_ns"), row.get("sidecars"))
            if retrying is not None:
                retrying.pop(path, None)
        elif retrying is not None and path is not None:
            retrying[path] = lineno
            if refused is not None:
                refused.pop(path, None)
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
    what.add_argument("--safety", choices=SAFETY, default=None,
                      help="for every file (default unsafe; with --sidecars, a sidecar's rating where it has one)")
    what.add_argument("--source", default="", help="a source URL recorded on every file")
    what.add_argument("--pool", default="", help="put every file in this pool, in order")
    what.add_argument("--pool-per-folder", action="store_true",
                      help="each folder that holds files becomes a pool named after it")
    what.add_argument("--sidecars", action="store_true",
                      help="also take tags, rating and source from files next to each image: "
                           "photo.jpg.json (a booru post's JSON), photo.jpg.xmp (XMP keywords), photo.jpg.txt "
                           "(a tag per line or comma-separated); photo.json etc. work too")
    p.add_argument("--record", type=Path, help="where sent files are noted (default under ~/.local/state)")
    p.add_argument("--ca-file", help="the CA certificate to trust for an https --url")
    p.add_argument("--dry-run", action="store_true", help="only count and list; send nothing")
    p.add_argument("--watch", type=float, metavar="SECONDS",
                   help="keep going: look at the folder again every SECONDS and send what is new")
    p.add_argument("--settle", type=float, default=10.0, metavar="SECONDS",
                   help="with --watch, send a file once it has not changed for this long (default 10)")
    a = p.parse_args(argv)
    if a.watch is not None and a.watch < 1:
        p.error("--watch needs at least 1 second")
    if a.watch is not None and a.dry_run:
        p.error("--watch and --dry-run exclude each other")
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


class Stop:
    """SIGTERM or Ctrl+C: finish the file being sent, then stop."""
    def __init__(self):
        self.event = threading.Event()

    def __call__(self, *_):
        self.event.set()

    def __bool__(self):
        return self.event.is_set()


def sign_in(api: Api, args) -> None:
    if args.token_file:
        env = env_lines(private_file(args.token_file))
        if not env.get("KURA_USER") or not env.get("KURA_TOKEN"):
            raise SystemExit(f"{args.token_file}: needs KURA_USER and KURA_TOKEN")
        api.authorization = token_header(env["KURA_USER"], env["KURA_TOKEN"])
    else:
        api.sign_in(args.email, private_file(args.key_file).strip())


def send(api: Api, args, folder: Path, record: Path, todo: list[Path], counts: dict, stop: Stop,
         settle: float = 0.0) -> None:
    """Send `todo` in order. With `settle`, each file is looked at again just before it goes:
    one that changed while the files before it were being sent waits for the next pass."""
    started = time.monotonic()
    failures_in_a_row = 0
    with open(record, "a") as out:
        for n, p in enumerate(todo, 1):
            if stop:
                say("stopping: asked to")
                break
            rel = str(p.relative_to(folder))
            if settle:
                try:
                    looked = [p] + (sidecar_paths(p) if args.sidecars else [])
                    changing = any(time.time() - q.stat().st_mtime < settle for q in looked)
                except OSError:             # gone since the pass looked
                    continue
                if changing:
                    counts["waiting"] = counts.get("waiting", 0) + 1
                    continue
            row: dict = {"path": rel}
            cats: dict = {}
            if args.sidecars:                   # taken before they are read: an edit after this is seen next time
                row["sidecars"] = sidecar_signature(p)
            try:
                status, body, st = api.upload(p, fields_for(args, folder, p, cats))
                row.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
            except (urllib.error.URLError, OSError) as e:
                status, body = 0, {"detail": str(getattr(e, "reason", e))[:200]}
            if status in (200, 202) and "id" in body:
                row.update(id=body["id"], duplicate=bool(body.get("duplicate")))
                if cats:
                    row["categories"] = cats
                counts["duplicates" if row["duplicate"] else "added"] += 1
                failures_in_a_row = 0
            else:
                row.update(status=status, error=detail(body))
                counts["failed"] += 1
                failures_in_a_row += 1
                say(f"  {rel}: {status or 'no connection'} {row['error']}")
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            if n % PROGRESS_EVERY == 0 or (n == len(todo) and n > 1):
                rate = n / max(time.monotonic() - started, 1e-9)
                left = (len(todo) - n) / rate if rate else 0
                say(f"  {n}/{len(todo)} sent, {rate:.1f}/s, about {left / 60:.0f} min left")
            if failures_in_a_row >= GIVE_UP_AFTER:
                say(f"stopping: {GIVE_UP_AFTER} sends in a row failed")
                break


def main(argv=None) -> int:
    args = arguments(argv)
    folder = args.folder.resolve()
    identity = import_identity(args, folder)
    record = args.record or default_record(identity)
    if args.watch is not None:
        return watch(args, folder, identity, record)
    done = open_record(record, identity, create=not args.dry_run)
    files, not_media, _ = plan(folder)
    todo = [p for p in files if done.get(str(p.relative_to(folder))) != file_state(args, p, p.stat())]
    say(f"{folder}: {len(files)} media files, {not_media} other files passed over, "
        f"{len(files) - len(todo)} already sent (record: {record})")
    if args.dry_run:
        for p in todo:
            f = fields_for(args, folder, p)
            print(f"{p.relative_to(folder)}" + (f"  → pool {f['pool']}" if f["pool"] else "")
                  + (f"  [{f['safety']}] {f['tags']}" if args.sidecars else ""))
        return 0

    api = Api(args.url, args.ca_file)
    sign_in(api, args)
    counts = {"added": 0, "duplicates": 0, "failed": 0, "skipped": len(files) - len(todo),
              "not_media": not_media}
    started = time.monotonic()
    try:                                        # from here on, sign-out happens whatever else does
        send(api, args, folder, record, todo, counts, Stop())
        if args.sidecars:
            set_categories(api, record, identity, counts)
    finally:
        api.sign_out()
    counts["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(counts))
    return 1 if counts["failed"] else 0


def watch(args, folder: Path, identity: dict, record: Path) -> int:
    """Keep `folder` in kura until stopped. Each pass sends what is new or changed and has
    settled; a file kura refused for good waits until it changes. Signs in only for a pass
    that has something to send, and out again after it."""
    stop = Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    open_record(record, identity)               # check (or write) the header before anything else
    say(f"watching {folder} every {args.watch:g} s (record: {record})")
    api = Api(args.url, args.ca_file)
    totals = {"added": 0, "duplicates": 0, "failed": 0}
    unrevoked: list[str] = []                   # session tokens whose sign-out did not get through
    last_category_try = float("-inf")

    def revoke_pending() -> None:
        unrevoked[:] = [a for a in unrevoked if not api.revoke(a)]

    def sign_out() -> None:
        if not api.signed_in:
            return
        api.sign_out()
        if api.signed_in:                       # kept, and tried again before anything else
            unrevoked.append(api.authorization)
            api.signed_in = False
        api.authorization = None if not args.token_file else api.authorization

    while not stop:
        if unrevoked:
            revoke_pending()
        refused: dict = {}
        retrying: dict = {}
        leftover: dict = {}
        done = open_record(record, identity, refused=refused, retrying=retrying, categories=leftover)

        def unchanged(rel, st, table):
            return rel in table and table[rel] == file_state(args, folder / rel, st)
        files, _, waiting = plan(folder, sent=lambda rel, st: unchanged(rel, st, done) or unchanged(rel, st, refused),
                                 settle=args.settle)
        todo = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            rel = str(p.relative_to(folder))
            if not unchanged(rel, st, done) and not unchanged(rel, st, refused):
                todo.append(p)
        # Files not tried yet go first, then the ones that failed before, the one tried longest
        # ago first: a run of failures anywhere in the folder must not keep the files behind it
        # from ever being tried (a pass gives up after GIVE_UP_AFTER failures in a row).
        fresh = [p for p in todo if str(p.relative_to(folder)) not in retrying]
        again = sorted((p for p in todo if str(p.relative_to(folder)) in retrying),
                       key=lambda p: retrying[str(p.relative_to(folder))])
        todo = fresh + again
        # Category work an earlier pass could not finish is taken up on its own as well, but
        # not on every pass: a category kura keeps refusing must not mean a sign-in a minute.
        categories_due = bool(leftover) and time.monotonic() - last_category_try >= CATEGORY_RETRY
        if todo or categories_due:
            if todo:
                say(f"{len(todo)} new or changed" + (f", {waiting} still changing" if waiting else ""))
            last_category_try = time.monotonic()
            counts = {"added": 0, "duplicates": 0, "failed": 0}
            try:
                sign_in(api, args)
                if todo:
                    send(api, args, folder, record, todo, counts, stop, settle=args.settle)
                if args.sidecars:
                    set_categories(api, record, identity, counts)
            except (urllib.error.URLError, OSError) as e:   # kura is away: try again next pass
                say(f"  could not reach kura ({getattr(e, 'reason', e)}); trying again in {args.watch:g} s")
            except SignInRefused as e:
                if e.status >= 500 or e.status in (408, 429):  # kura restarting or busy
                    say(f"  {e}; trying again in {args.watch:g} s")
                else:                                        # a wrong key does not get better by itself
                    say(str(e))
                    return 1
            finally:
                sign_out()
            say(f"  {json.dumps(counts)}")
            for k in totals:
                totals[k] += counts[k]
        stop.event.wait(args.watch)
    if unrevoked:
        revoke_pending()
        if unrevoked:
            say(f"{len(unrevoked)} session token(s) could not be revoked; they expire on their own (30 days)")
    print(json.dumps(totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
