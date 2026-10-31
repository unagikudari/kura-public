#!/usr/bin/env python3
"""Move a Hydrus Network library into kura through the Hydrus Client API: every file with
its current tags, namespaces mapped to kura's tag categories, and its first known URL as
the source.

    python3 scripts/hydrus_migrate.py \
        --from http://127.0.0.1:45869 --from-key-file ~/hydrus-api.key \
        --url http://127.0.0.1:8001 --email you@example.org --key-file ~/kura-admin.key

In Hydrus: services → manage services → client api → add a key with "search for files"
permission, and put it in --from-key-file (0600). Tags like creator:name become kura tags
named name in the category the namespace maps to (--namespace creator=artist,
series=copyright, character=character by default; an unmapped namespace keeps the tag's
full text, with spaces as underscores). Hydrus has no safety rating, so every file gets
--safety (default unsafe). Files kura already holds come back as duplicates. A record file
lets an interrupted run continue; `--record` names it. Credentials come from files only.
Needs only Python 3.10+; no packages.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kura_http_key import install_api_key_opener  # noqa: E402  (kura-bound requests carry this node's machine key)
from kura_import import Api, env_lines, private_file, say, token_header  # noqa: E402
from szurubooru_migrate import Record, ShortDownload, copy_tag_categories, default_record, move_posts  # noqa: E402
install_api_key_opener()

CHUNK = 100
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
       "video/mp4": ".mp4", "video/webm": ".webm"}
DEFAULT_NAMESPACES = "creator=artist,series=copyright,character=character"


class Hydrus:
    """The Client API, read-only: search, metadata, file bytes. The access key goes only to
    the Hydrus address given."""

    def __init__(self, url: str, access_key: str):
        self.url = url.rstrip("/")
        self.origin = urllib.parse.urlparse(self.url)[:2]
        self.key = access_key

    def _headers(self, url: str) -> dict:
        h = {"accept": "application/json"}
        if urllib.parse.urlparse(url)[:2] == self.origin:
            h["Hydrus-Client-API-Access-Key"] = self.key
        return h

    def get(self, path: str, **params) -> dict:
        # Hydrus wants list and dict parameters JSON-encoded in the query string.
        q = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                    for k, v in params.items() if v is not None})
        url = f"{self.url}{path}" + (f"?{q}" if q else "")
        req = urllib.request.Request(url, headers=self._headers(url))
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def file_ids(self) -> list[int]:
        return sorted(self.get("/get_files/search_files", tags=["system:everything"]).get("file_ids", []))

    def metadata(self, ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), CHUNK):
            out.extend(self.get("/get_files/file_metadata", file_ids=ids[i:i + CHUNK]).get("metadata", []))
        return out

    def download(self, content_url: str, dest: Path) -> None:
        """`content_url` is the /files/<hash><ext> pseudo-path the post carries; the bytes
        come from /get_files/file?hash=."""
        sha = Path(urllib.parse.urlparse(content_url).path).stem
        url = f"{self.url}/get_files/file?hash={sha}"
        req = urllib.request.Request(url, headers=self._headers(url))
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
            declared = r.headers.get("content-length")
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
        if declared is not None and got != int(declared):
            raise ShortDownload(f"{url}: {got} of {declared} bytes")


def current_tags(meta: dict) -> list[str]:
    """The file's current tags across every tag service, in both API shapes (the newer
    `tags` keyed by service key, the older `service_names_to_statuses_to_display_tags`)."""
    names: list[str] = []
    for service in (meta.get("tags") or {}).values():
        for key in ("display_tags", "storage_tags"):
            for tag in (service.get(key) or {}).get("0", []):     # status 0 = current
                if tag not in names:
                    names.append(tag)
    for service in (meta.get("service_names_to_statuses_to_display_tags") or {}).values():
        for tag in service.get("0", []):
            if tag not in names:
                names.append(tag)
    return names


def kura_tag(tag: str, namespaces: dict[str, str]) -> tuple[str, str | None]:
    """A Hydrus tag → (kura tag name, category or None). creator:jane doe → (jane_doe, artist);
    an unmapped namespace keeps its text: rating:5 → rating:5 (kura tag names may hold a colon)."""
    ns, sep, rest = tag.partition(":")
    if sep and ns in namespaces:
        return rest.strip().replace(" ", "_"), namespaces[ns]
    return tag.strip().replace(" ", "_"), None


def as_posts(metadata: list[dict], namespaces: dict[str, str], safety: str) -> list[dict]:
    """Hydrus metadata in the shape move_posts and copy_tag_categories read (the szurubooru
    post shape: id, contentUrl, tags as micro tags, safety, source, mimeType)."""
    posts = []
    for m in metadata:
        if m.get("is_deleted") or m.get("is_trashed") or not m.get("hash"):
            continue
        mime = m.get("mime") or ""
        tags = []
        for t in current_tags(m):
            name, cat = kura_tag(t, namespaces)
            if name:
                tags.append({"names": [name], "category": cat or "default"})
        urls = [u for u in m.get("known_urls", []) if isinstance(u, str)]
        posts.append({"id": m["file_id"], "contentUrl": f"/files/{m['hash']}{EXT.get(mime, '')}",
                      "tags": tags, "safety": safety, "source": urls[0] if urls else "", "mimeType": mime})
    return sorted(posts, key=lambda p: p["id"])


def parse_namespaces(spec: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        ns, sep, cat = item.partition("=")
        if not sep:
            raise SystemExit(f"--namespace: expected namespace=category, got {item!r}")
        out[ns.strip()] = cat.strip()
    return out


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("the Hydrus to read")
    src.add_argument("--from", dest="src", required=True, help="the Client API address, e.g. http://127.0.0.1:45869")
    src.add_argument("--from-key-file", type=Path, required=True, help="a 0600 file holding the Client API access key")
    dst = p.add_argument_group("the kura to write")
    dst.add_argument("--url", required=True, help="the address you open kura at")
    dst.add_argument("--key-file", type=Path, help="your kura sign-in key (with --email)")
    dst.add_argument("--email")
    dst.add_argument("--token-file", type=Path, help="or a 0600 file with KURA_USER and KURA_TOKEN")
    dst.add_argument("--ca-file")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACES,
                   help=f"namespace=kura category pairs, comma separated (default: {DEFAULT_NAMESPACES})")
    p.add_argument("--safety", choices=("safe", "sketchy", "unsafe"), default="unsafe",
                   help="Hydrus has no safety rating; every file gets this one (default: unsafe)")
    p.add_argument("--skip-tag-categories", action="store_true")
    p.add_argument("--record", type=Path)
    p.add_argument("--dry-run", action="store_true", help="count and list; send nothing")
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
    src = Hydrus(args.src, private_file(args.from_key_file).strip())
    # The record belongs to one import: the same Hydrus, kura, safety and namespace mapping.
    # A run with other settings must not skip what this one sent (nor change what it sent).
    identity = {"source": "hydrus", "from": src.url, "url": args.url.rstrip("/"), "safety": args.safety,
                "namespaces": dict(sorted(namespaces.items()))}
    record_path = args.record or default_record(identity)
    record = Record(record_path, identity, create=not args.dry_run)

    ids = src.file_ids()
    posts = as_posts(src.metadata(ids), namespaces, args.safety)
    todo = [p for p in posts if p["id"] not in record.posts]
    say(f"{src.url}: {len(posts)} files ({len(posts) - len(todo)} already moved) (record: {record_path})")
    if args.dry_run:
        for p in todo[:20]:
            print(f"file {p['id']}  {p['mimeType']}  {' '.join(t['names'][0] for t in p['tags'])}  {p['source']}")
        if len(todo) > 20:
            print(f"… and {len(todo) - 20} more")
        return 0

    counts = {"posts": len(posts), "added": 0, "duplicates": 0, "failed": 0, "unsupported": 0,
              "skipped": len(posts) - len(todo), "tags_recategorized": 0}
    tmp = Path(tempfile.mkdtemp(prefix="kura-migrate-"))
    started = time.monotonic()
    try:
        if args.token_file:
            env = env_lines(private_file(args.token_file))
            if not env.get("KURA_USER") or not env.get("KURA_TOKEN"):
                raise SystemExit(f"{args.token_file}: needs KURA_USER and KURA_TOKEN")
            api.authorization = token_header(env["KURA_USER"], env["KURA_TOKEN"])
        else:
            api.sign_in(args.email, private_file(args.key_file).strip())
        if move_posts(src, api, record, todo, tmp, "artist", counts) and not args.skip_tag_categories:
            copy_tag_categories(api, posts, counts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        api.sign_out()
    counts["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(counts))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
