#!/usr/bin/env python3
"""Move a LANraragi library into kura through the two APIs: each archive becomes a pool of its
pages, in order, with the archive's tags on every page.

    python3 scripts/lanraragi_migrate.py \
        --from http://127.0.0.1:3000 [--from-key-file ~/lrr-api.key] \
        --url http://127.0.0.1:8001 --email you@example.org --key-file ~/kura-admin.key

LANraragi's tags are namespace:value pairs. artist: and group: become kura artist tags,
character: character tags, parody: and series: copyright tags (--namespace changes the
mapping); source: becomes the source, date_added: is left out, and any other namespace keeps
the tag's full text (female:glasses stays female:glasses). LANraragi has no safety rating, so
every page gets --safety (default unsafe). Archives are read as zip/cbz; a rar, 7z or pdf in
the library is named and passed over. Hidden entries, macOS leftovers and pages that would
unpack to far more than they look are passed over as kura_import.py --archives does.

The API key is needed only when LANraragi runs in No-Fun Mode; give it in a 0600 file. A
record file lets an interrupted run continue; `--record` names it. Credentials come from files
only. Needs only Python 3.10+; no packages.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kura_http_key import install_api_key_opener  # noqa: E402  (kura-bound requests carry this node's machine key)
from kura_import import (Api, FileChanged, Item, archive_members, copy_tag_categories, detail,  # noqa: E402
                         env_lines, kura_name, media_type, natural_key, private_file, say, token_header)
install_api_key_opener()

DEFAULT_NAMESPACES = "artist=artist,group=artist,character=character,parody=copyright,series=copyright"
LEFT_OUT = {"date_added"}
ZIP_EXTENSIONS = {"zip", "cbz"}
GIVE_UP_AFTER = 10


class ShortDownload(OSError):
    """The archive arrived shorter than LANraragi said it was."""


class Lanraragi:
    """The read-only part of the LANraragi API. The key goes only to the LANraragi address."""

    def __init__(self, url: str, key: str | None):
        self.url = url.rstrip("/")
        self.origin = urllib.parse.urlparse(self.url)[:2]
        self.key = key

    def _headers(self, url: str) -> dict:
        h = {"accept": "application/json"}
        if self.key and urllib.parse.urlparse(url)[:2] == self.origin:
            h["authorization"] = "Bearer " + base64.b64encode(self.key.encode()).decode()
        return h

    def archives(self) -> list[dict]:
        url = f"{self.url}/api/archives"
        with urllib.request.urlopen(urllib.request.Request(url, headers=self._headers(url)), timeout=120) as r:
            data = json.loads(r.read())
        if not isinstance(data, list):
            raise SystemExit(f"{url}: expected a list of archives, got {type(data).__name__}")
        return sorted((a for a in data if isinstance(a, dict) and a.get("arcid")),
                      key=lambda a: natural_key(a.get("title") or a["arcid"]))

    def download(self, arcid: str, dest: Path) -> None:
        url = f"{self.url}/api/archives/{urllib.parse.quote(arcid)}/download"
        req = urllib.request.Request(url, headers=self._headers(url))
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
            declared = r.headers.get("content-length")
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
        if declared is not None and got != int(declared):
            raise ShortDownload(f"{arcid}: {got} of {declared} bytes")


def parse_namespaces(spec: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        ns, sep, cat = item.partition("=")
        if not sep:
            raise SystemExit(f"--namespace: expected namespace=category, got {item!r}")
        out[ns.strip().lower()] = cat.strip()
    return out


def archive_fields(arc: dict, namespaces: dict[str, str]) -> tuple[list[tuple[str, str | None]], str]:
    """The archive's tags as (kura name, category or None), and its source URL."""
    tags: list[tuple[str, str | None]] = []
    source = ""
    for raw in str(arc.get("tags") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        ns, sep, value = raw.partition(":")
        key = ns.strip().lower()
        if sep and key == "source":
            v = value.strip()
            if not source:
                source = v if v.startswith(("http://", "https://")) else "https://" + v if "." in v else ""
            continue
        if sep and key in LEFT_OUT:
            continue
        if sep and key in namespaces and value.strip():
            tags.append((kura_name(value), namespaces[key]))
        else:
            tags.append((kura_name(raw), None))
    # one entry per name, in first-seen order; a category from any occurrence wins over none
    # ("jane, artist:jane" is the artist jane)
    order: dict[str, str | None] = {}
    for name, cat in tags:
        if name and (name not in order or (order[name] is None and cat)):
            order[name] = cat
    return list(order.items()), source


class Record:
    """What a previous run moved: (archive id, page name) → (kura post id, CRC), and which
    archives are complete. Bound to one migration (the same LANraragi, kura, safety, mapping)."""

    def __init__(self, path: Path, identity: dict, create: bool = True):
        self.path, self.pages, self.complete = path, {}, set()
        self.pools: dict[str, str] = {}             # arcid → the pool name chosen when it was first sent
        self.damaged: set[tuple[str, str, int]] = set()
        lines = path.read_text().splitlines() if path.exists() else []
        if not lines:
            if create:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._append({"migrate": identity})
            return
        try:
            header = json.loads(lines[0]).get("migrate")
        except (ValueError, AttributeError):
            header = None
        if header != identity:
            raise SystemExit(f"{path} belongs to a different migration ({json.dumps(header)}); give --record another file")
        for line in lines[1:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("complete"):
                self.complete.add(row["arc"])
            elif "pool" in row and "arc" in row and "page" not in row:
                self.pools[row["arc"]] = row["pool"]
            elif row.get("damaged") and "page" in row:
                self.damaged.add((row["arc"], row["page"], row.get("crc")))
            elif row.get("id") is not None:
                self.pages[(row["arc"], row["page"])] = (row["id"], row.get("crc"))

    def _append(self, row: dict) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def default_record(identity: dict) -> Path:
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "kura/migrate" / f"{digest}.jsonl"


def move_archive(api: Api, record: Record, arc: dict, file: Path, pool: str, namespaces: dict,
                 safety: str, counts: dict) -> bool:
    """Send the pages of one downloaded archive, in order. True when the archive is done: every
    page in kura, or passed over for good (not an image, or damaged so it cannot be unpacked).
    A page kura did not take stops the archive there, so the next run goes on from that page
    and the pool keeps the archive's order."""
    arcid, title = arc["arcid"], (arc.get("title") or arc["arcid"])
    tags, source = archive_fields(arc, namespaces)
    fields = {"tags": " ".join(n for n, _ in tags), "safety": safety, "source": source, "pool": pool,
              "artist_tags": " ".join(n for n, c in tags if c == "artist")}
    try:
        z = zipfile.ZipFile(file)
        infos = archive_members(z)
    except (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, NotImplementedError, EOFError) as e:
        counts["archives_unreadable"] += 1
        say(f"  {title}: not a zip archive kura can read ({e}); passed over")
        record._append({"arc": arcid, "complete": True, "unreadable": str(e)[:200]})   # not fetched again
        record.complete.add(arcid)
        return False
    with z:
        for info in infos:
            if Path(info.filename).name.lower() == "comicinfo.xml":
                continue
            done = record.pages.get((arcid, info.filename))
            if done and done[1] == info.CRC:
                counts["skipped"] += 1
                continue
            if (arcid, info.filename, info.CRC) in record.damaged:
                continue                                    # passed over for good in an earlier run
            row = {"arc": arcid, "page": info.filename, "crc": info.CRC}
            try:
                with z.open(info) as f:
                    head = f.read(12)
                if media_type(head) is None:
                    continue
                status, body, _ = api.upload(Item(f"{arcid}/{info.filename}", file, info.filename,
                                                  info.file_size, info.CRC), fields)
            except (FileChanged, *Api.UNPACK_ERRORS) as e:    # the page itself is damaged: it never will go
                counts["pages_damaged"] += 1
                say(f"  {title} / {info.filename}: damaged, passed over ({e})")
                record._append({**row, "damaged": str(e)[:200]})
                record.damaged.add((arcid, info.filename, info.CRC))
                continue
            except (urllib.error.URLError, OSError) as e:
                status, body = 0, {"detail": str(getattr(e, "reason", e))[:200]}
            if status in (200, 202) and "id" in body:
                row.update(id=body["id"], duplicate=bool(body.get("duplicate")))
                record.pages[(arcid, info.filename)] = (body["id"], info.CRC)
                counts["duplicates" if row["duplicate"] else "added"] += 1
                record._append(row)
                continue
            row.update(status=status, error=detail(body))
            counts["failed"] += 1
            record._append(row)
            say(f"  {title} / {info.filename}: {status or 'no connection'} {row['error']}; "
                f"the rest of this archive waits for the next run")
            return False
    return True


def pool_name(record: Record, arcs: list[dict], arc: dict) -> str:
    """The pool an archive's pages go to. Chosen once, the first time the archive is sent, and
    kept in the record: a same-titled archive appearing later never moves the rest of a book
    elsewhere. The title, unless another archive in the library has it or another archive's
    pool already took it; then the title with the archive id, as long as needed to be unique."""
    arcid = arc["arcid"]
    if arcid in record.pools:
        return record.pools[arcid]
    title = arc.get("title") or arcid
    taken = {name for other, name in record.pools.items() if other != arcid}
    taken |= {(a.get("title") or a["arcid"]) for a in arcs if a["arcid"] != arcid}
    candidates = [title] + [f"{title} [{arcid[:n]}]" for n in (8, 16, 40)]
    name = next((c for c in candidates if c not in taken), None)
    k = 2
    while name is None:                     # every candidate taken: number it until it is free
        if f"{title} [{arcid}] ({k})" not in taken:
            name = f"{title} [{arcid}] ({k})"
        k += 1
    record.pools[arcid] = name
    record._append({"arc": arcid, "pool": name})
    return name


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("the LANraragi to read")
    src.add_argument("--from", dest="src", required=True, help="its address, e.g. http://127.0.0.1:3000")
    src.add_argument("--from-key-file", type=Path, help="a 0600 file holding the API key (No-Fun Mode only)")
    dst = p.add_argument_group("the kura to write")
    dst.add_argument("--url", required=True, help="the address you open kura at")
    dst.add_argument("--key-file", type=Path, help="your kura sign-in key (with --email)")
    dst.add_argument("--email")
    dst.add_argument("--token-file", type=Path, help="or a 0600 file with KURA_USER and KURA_TOKEN")
    dst.add_argument("--ca-file")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACES,
                   help=f"namespace=kura category pairs, comma separated (default: {DEFAULT_NAMESPACES})")
    p.add_argument("--safety", choices=("safe", "sketchy", "unsafe"), default="unsafe",
                   help="LANraragi has no safety rating; every page gets this one (default: unsafe)")
    p.add_argument("--skip-tag-categories", action="store_true")
    p.add_argument("--record", type=Path)
    p.add_argument("--dry-run", action="store_true", help="list the archives and their tags; send nothing")
    a = p.parse_args(argv)
    if not a.dry_run and bool(a.key_file) == bool(a.token_file):
        p.error("give --key-file with --email, or --token-file")
    if a.key_file and not a.email:
        p.error("--key-file needs --email")
    return a


def main(argv=None) -> int:
    args = arguments(argv)
    namespaces = parse_namespaces(args.namespace)
    api = Api(args.url, args.ca_file)       # first: installs the opener that refuses redirects, for both sides
    src = Lanraragi(args.src, private_file(args.from_key_file).strip() if args.from_key_file else None)
    identity = {"source": "lanraragi", "from": src.url, "url": args.url.rstrip("/"), "safety": args.safety,
                "namespaces": dict(sorted(namespaces.items()))}
    record_path = args.record or default_record(identity)
    record = Record(record_path, identity, create=not args.dry_run)

    try:
        arcs = src.archives()
    except urllib.error.HTTPError as e:
        hint = " (LANraragi is in No-Fun Mode: give its API key with --from-key-file)" if e.code == 401 else ""
        raise SystemExit(f"{src.url}: listing the archives was refused ({e.code}){hint}") from None
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"{src.url}: cannot reach LANraragi ({getattr(e, 'reason', e)})") from None
    todo = [a for a in arcs if a["arcid"] not in record.complete]
    say(f"{src.url}: {len(arcs)} archives ({len(arcs) - len(todo)} already moved) (record: {record_path})")
    if args.dry_run:
        for a in todo[:20]:
            tags, source = archive_fields(a, namespaces)
            print(f"{a.get('title') or a['arcid']}  [{a.get('extension', '?')}, {a.get('pagecount', '?')} pages]  "
                  f"{' '.join(n for n, _ in tags)}  {source}")
        if len(todo) > 20:
            print(f"… and {len(todo) - 20} more")
        return 0

    counts = {"archives": len(arcs), "archives_moved": 0, "archives_skipped": len(arcs) - len(todo),
              "archives_unreadable": 0, "archives_failed": 0, "added": 0, "duplicates": 0, "failed": 0,
              "skipped": 0, "pages_damaged": 0, "tags_recategorized": 0}
    failed_in_a_row = 0
    tmp = Path(tempfile.mkdtemp(prefix="kura-lrr-"))
    started = time.monotonic()
    try:
        if args.token_file:
            env = env_lines(private_file(args.token_file))
            if not env.get("KURA_USER") or not env.get("KURA_TOKEN"):
                raise SystemExit(f"{args.token_file}: needs KURA_USER and KURA_TOKEN")
            api.authorization = token_header(env["KURA_USER"], env["KURA_TOKEN"])
        else:
            api.sign_in(args.email, private_file(args.key_file).strip())
        for n, arc in enumerate(todo, 1):
            title = arc.get("title") or arc["arcid"]
            ext = str(arc.get("extension") or "").lower()
            if ext and ext not in ZIP_EXTENSIONS:       # rar, 7z, pdf…: not fetched at all
                counts["archives_unreadable"] += 1
                say(f"  {title}: a .{ext} archive; only zip and cbz are read, passed over")
                continue
            file = tmp / "archive"
            try:
                src.download(arc["arcid"], file)
                # complete only when every page is in: an archive with a failed page is taken up
                # again next run, its pages already in kura skipped
                ok = move_archive(api, record, arc, file, pool_name(record, arcs, arc), namespaces,
                                  args.safety, counts)
            except (urllib.error.URLError, OSError) as e:
                counts["archives_failed"] += 1
                say(f"  {title}: download failed ({getattr(e, 'reason', e)})")
                ok = None
            finally:
                file.unlink(missing_ok=True)
            if ok:
                record._append({"arc": arc["arcid"], "complete": True})
                counts["archives_moved"] += 1
            failed_in_a_row = 0 if ok or arc["arcid"] in record.complete else failed_in_a_row + 1
            if failed_in_a_row >= GIVE_UP_AFTER:
                say(f"stopping: {GIVE_UP_AFTER} archives in a row did not go in")
                break
            say(f"  {n}/{len(todo)} archives: {title}")
        if not args.skip_tag_categories:
            posts = [{"tags": [{"names": [name], "category": cat}
                               for name, cat in archive_fields(a, namespaces)[0] if cat]} for a in arcs]
            copy_tag_categories(api, posts, counts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        api.sign_out()
    counts["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(counts))
    return 1 if counts["failed"] or counts["archives_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
