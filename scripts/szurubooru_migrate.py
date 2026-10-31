#!/usr/bin/env python3
"""Move a szurubooru library into kura: posts with their tags, safety and source, the tag
categories, and the pools in their order.

    python3 scripts/szurubooru_migrate.py \
        --from https://booru.example.org --from-user me --from-token-file ~/booru.token \
        --url http://127.0.0.1:8001 --email you@example.org --key-file ~/kura-admin.key

Both sides are talked to through their HTTP APIs only. Every szurubooru post is downloaded
to a temporary file and sent to kura's /api/upload with its tags, safety and source; a tag
whose szurubooru category is --artist-category (default "artist") is declared as an artist
tag. Files kura already holds come back as duplicates and are not added twice. Then each
tag's category is copied where kura has a category of the same name (only tags still in
kura's default category are changed), and each pool is created with its posts in order.

A record file notes every post and pool as it is done, so a run that was interrupted
continues where it stopped: posts that failed are tried again, and a pool whose posts were
not all in yet is completed once they are. `--record` names the file, the default is under
~/.local/state/kura/. Credentials come from files (0600, yours) or, for kura, a --token-file;
never from argv. The szurubooru credential is sent only to the szurubooru address given,
and neither address may redirect.

The szurubooru side needs a user token (Account → Login tokens) in --from-token-file, or
the password in --from-password-file. Needs only Python 3.10+; no packages.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kura_http_key import install_api_key_opener  # noqa: E402  (kura-bound requests carry this node's machine key)
from kura_import import Api, FileChanged, env_lines, media_type, private_file, say, token_header  # noqa: E402
install_api_key_opener()

PAGE = 100
GIVE_UP_AFTER = 10


class ShortDownload(OSError):
    """The file ended before the length the server declared."""


# ---- szurubooru -----------------------------------------------------------------------------

class Szurubooru:
    def __init__(self, url: str, user: str, token: str | None, password: str | None):
        self.url = url.rstrip("/")
        self.origin = urllib.parse.urlparse(self.url)[:2]      # scheme and host: where the credential may go
        cred = f"{user}:{token or password}".encode()
        self.authorization = ("Token " if token else "Basic ") + base64.b64encode(cred).decode()

    def _headers(self, url: str) -> dict:
        """The credential goes to the szurubooru itself, never to another origin a contentUrl
        may point at (a CDN, say)."""
        h = {"accept": "application/json"}
        if urllib.parse.urlparse(url)[:2] == self.origin:
            h["authorization"] = self.authorization
        return h

    def get(self, path: str, **params) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.url}/api/{path}" + (f"?{q}" if q else "")
        req = urllib.request.Request(url, headers=self._headers(url))
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def pages(self, path: str, fields: str, query: str = "") -> list[dict]:
        """Every result of a paged listing, in the server's order."""
        out: list[dict] = []
        offset = 0
        while True:
            page = self.get(path, offset=offset, limit=PAGE, fields=fields, query=query or None)
            results = page.get("results", [])
            out.extend(results)
            offset += len(results)          # the server may hand back a smaller page than asked
            if not results or offset >= page.get("total", 0):
                return out

    def download(self, content_url: str, dest: Path) -> None:
        url = content_url if content_url.startswith(("http://", "https://")) else f"{self.url}/{content_url.lstrip('/')}"
        req = urllib.request.Request(url, headers=self._headers(url))
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
            declared = r.headers.get("content-length")
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
        if declared is not None and got != int(declared):
            raise ShortDownload(f"{url}: {got} of {declared} bytes")


def micro_tag_names(tags: list[dict]) -> list[str]:
    return [t["names"][0] for t in tags if t.get("names")]


# ---- the record -----------------------------------------------------------------------------

class Record:
    def __init__(self, path: Path, identity: dict, create: bool):
        self.path = path
        self.posts: dict[int, int] = {}            # szurubooru post id → kura post id
        self.pools: dict[int, dict] = {}           # szurubooru pool id → {"id": kura id, "missing": n}
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
            raise SystemExit(f"{path} belongs to a different migration ({json.dumps(header)}); "
                             f"give --record another file")
        for line in lines[1:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("id") is None:
                continue
            if "post" in row:
                self.posts[row["post"]] = row["id"]
            elif "pool" in row:
                # A pool row written before 1.299.1 has no "included". The record is appended in
                # order and posts are moved before pools, so the members that row put in the pool
                # are exactly the posts recorded above it: completing such a pool later appends
                # only the posts that came in after it, never one the owner has since taken out.
                included = set(row["included"]) if "included" in row else set(self.posts)
                self.pools[row["pool"]] = {"id": row["id"], "missing": row.get("missing", 0), "included": included}

    def _append(self, row: dict) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def post(self, szuru_id: int, **row) -> None:
        self._append({"post": szuru_id, **row})
        if row.get("id") is not None:
            self.posts[szuru_id] = row["id"]

    def pool(self, szuru_id: int, **row) -> None:
        self._append({"pool": szuru_id, **row})
        if row.get("id") is not None:
            self.pools[szuru_id] = {"id": row["id"], "missing": row.get("missing", 0),
                                    "included": set(row.get("included", []))}


def default_record(identity: dict) -> Path:
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "kura/migrate" / f"{digest}.jsonl"


# ---- main -----------------------------------------------------------------------------------

def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("the szurubooru to read")
    src.add_argument("--from", dest="src", required=True, help="its address, e.g. https://booru.example.org")
    src.add_argument("--from-user", required=True)
    src.add_argument("--from-token-file", type=Path, help="a 0600 file holding a login token")
    src.add_argument("--from-password-file", type=Path, help="or a 0600 file holding the password")
    dst = p.add_argument_group("the kura to write")
    dst.add_argument("--url", required=True, help="the address you open kura at")
    dst.add_argument("--key-file", type=Path, help="your kura sign-in key (with --email)")
    dst.add_argument("--email")
    dst.add_argument("--token-file", type=Path, help="or a 0600 file with KURA_USER and KURA_TOKEN")
    dst.add_argument("--ca-file")
    p.add_argument("--artist-category", default="artist",
                   help="the szurubooru tag category whose tags are artists (default: artist)")
    p.add_argument("--skip-pools", action="store_true")
    p.add_argument("--skip-tag-categories", action="store_true")
    p.add_argument("--record", type=Path)
    p.add_argument("--dry-run", action="store_true", help="count and list; send nothing")
    a = p.parse_args(argv)
    if bool(a.from_token_file) == bool(a.from_password_file):
        p.error("give --from-token-file or --from-password-file")
    if not a.dry_run and bool(a.key_file) == bool(a.token_file):
        p.error("give --key-file with --email, or --token-file")
    if a.key_file and not a.email:
        p.error("--key-file needs --email")
    return a


def _detail(body: dict) -> str:
    d = body.get("detail", body)
    return (d.get("fix") or d.get("error") or json.dumps(d, ensure_ascii=False)) if isinstance(d, dict) \
        else json.dumps(d, ensure_ascii=False)


def move_posts(src: Szurubooru, api: Api, record: Record, todo: list[dict], tmp: Path, artist_category: str,
               counts: dict) -> bool:
    """Download and upload each post in `todo`; returns False when the run gave up."""
    started = time.monotonic()
    failures_in_a_row = 0
    for n, p in enumerate(todo, 1):
        tags = p.get("tags", [])
        # kura keeps the file name it was given; szurubooru's is <id>_<hash>.<ext>, worth keeping
        dest = tmp / (Path(urllib.parse.urlparse(p.get("contentUrl", "")).path).name or f"{p['id']}.bin")
        try:
            src.download(p["contentUrl"], dest)
            with open(dest, "rb") as f:
                kind = media_type(f.read(12))
            if kind is None:
                counts["unsupported"] += 1
                record.post(p["id"], error=f"not an image or video kura takes ({p.get('mimeType')})")
                say(f"  post {p['id']}: passed over ({p.get('mimeType')})")
                continue
            names = micro_tag_names(tags)
            artists = [t["names"][0] for t in tags if t.get("names") and t.get("category") == artist_category]
            status, body, _ = api.upload(dest, {"tags": " ".join(names), "safety": p.get("safety", "unsafe"),
                                                "source": p.get("source") or "", "artist_tags": " ".join(artists)})
        except (urllib.error.URLError, OSError, FileChanged) as e:
            status, body = 0, {"detail": str(getattr(e, "reason", e))[:200]}
        finally:
            dest.unlink(missing_ok=True)
        if status in (200, 202) and "id" in body:
            record.post(p["id"], id=body["id"], duplicate=bool(body.get("duplicate")))
            counts["duplicates" if body.get("duplicate") else "added"] += 1
            failures_in_a_row = 0
        else:
            record.post(p["id"], status=status, error=_detail(body)[:200])
            counts["failed"] += 1
            failures_in_a_row += 1
            say(f"  post {p['id']}: {status or 'no connection'} {_detail(body)[:160]}")
        if n % 25 == 0 or n == len(todo):
            rate = n / max(time.monotonic() - started, 1e-9)
            say(f"  {n}/{len(todo)} posts, {rate:.1f}/s, about {(len(todo) - n) / rate / 60:.0f} min left")
        if failures_in_a_row >= GIVE_UP_AFTER:
            say(f"stopping: {GIVE_UP_AFTER} posts in a row failed")
            return False
    return True


def copy_tag_categories(api: Api, posts: list[dict], counts: dict) -> None:
    """Every tag seen on any source post, so an interrupted earlier run leaves nothing behind."""
    tag_category: dict[str, str] = {}
    for p in posts:
        for t in p.get("tags", []):
            if t.get("names") and t.get("category"):
                tag_category[t["names"][0]] = t["category"]
    status, cats = api.call("GET", "/api/tag-categories")
    kura_cats = cats.get("detail") if status == 200 else None
    if not isinstance(kura_cats, list):
        say(f"tag categories not copied: kura answered {status}")
        return
    known = {c["name"] for c in kura_cats}
    default = next((c["name"] for c in kura_cats if c.get("default")), "default")
    for name, cat in sorted(tag_category.items()):
        if cat not in known or cat in (default, "default"):
            continue
        quoted = urllib.parse.quote(name, safe="")
        status, tag = api.call("GET", f"/api/tag/{quoted}")
        if status == 200 and tag.get("category", default) == default:
            # kura's tag update is optimistically locked: send the version just read
            status, reply = api.call("PATCH", f"/api/tag/{quoted}",
                                     json.dumps({"version": tag.get("version", 1), "category": cat}).encode(),
                                     {"content-type": "application/json"})
            if status == 200:
                counts["tags_recategorized"] += 1
            else:
                say(f"  tag {name}: category not set ({status} {_detail(reply)[:120]})")
    # szurubooru's own default category ("default") is kura's default, whatever kura calls it
    missing = sorted({c for c in tag_category.values() if c not in known and c != "default"})
    if missing:
        say(f"tag categories kura does not have, left at its default: {', '.join(missing)}")


def move_pools(api: Api, record: Record, pools: list[dict], counts: dict) -> None:
    for pool in pools:
        members = [mp["id"] for mp in pool.get("posts", []) if mp.get("id") in record.posts]   # szurubooru ids
        ids = [record.posts[m] for m in members]
        missing = len(pool.get("posts", [])) - len(ids)
        name = (pool.get("names") or [f"pool-{pool['id']}"])[0]
        done = record.pools.get(pool["id"])
        if done is not None:
            if done["missing"] and missing < done["missing"]:    # posts that failed before are in now
                # Completing means adding what was missing — never replacing the list. The owner
                # may have reordered the pool, added to it or taken a post out since the last run;
                # kura's current list is read first and only the posts moved since the last write
                # are appended, in szurubooru order, under kura's version lock (kura#159).
                status, current = api.call("GET", f"/api/pools/{done['id']}")
                if status != 200 or not isinstance(current.get("posts"), list):
                    counts["pools_failed"] += 1
                    say(f"  pool {pool['id']} ({name}): not completed (could not read it: {status} {_detail(current)[:100]})")
                    continue
                have = [p if isinstance(p, int) else p.get("id") for p in current["posts"]]
                new = [record.posts[m] for m in members if m not in done["included"]]
                body = {"posts": have + [i for i in new if i not in have]}
                if "version" in current:
                    body["version"] = current["version"]
                status, reply = api.call("PUT", f"/api/pools/{done['id']}", json.dumps(body).encode(),
                                         {"content-type": "application/json"})
                if status == 200:
                    record.pool(pool["id"], id=done["id"], posts=len(ids), missing=missing, included=members)
                    counts["pools_completed"] += 1
                else:                       # nothing recorded: the next run tries again; the run fails
                    counts["pools_failed"] += 1
                    say(f"  pool {pool['id']} ({name}): not completed ({status} {_detail(reply)[:120]})")
            elif done["missing"]:
                missing = done["missing"]
            if missing:
                say(f"  pool {pool['id']} ({name}): {missing} of its posts are not in kura yet")
            continue
        body = {"names": pool.get("names") or [name], "description": pool.get("description") or None, "posts": ids}
        # A pool of that name already in kura is left exactly as it is: noted, never rewritten.
        status, existing = api.call("GET", f"/api/pools/{urllib.parse.quote(name, safe='')}")
        if status == 200 and "id" in existing:
            record.pool(pool["id"], id=existing["id"], existing=True)
            counts["pools_existing"] += 1
            say(f"  pool {pool['id']} ({name}): kura already has it (#{existing['id']}), left as is")
            continue
        status, reply = api.call("POST", "/api/pools", json.dumps(body).encode(), {"content-type": "application/json"})
        if status == 200 and "id" in reply:
            record.pool(pool["id"], id=reply["id"], posts=len(ids), missing=missing, included=members)
            counts["pools_created"] += 1
            if missing:
                say(f"  pool {pool['id']} ({name}): created without {missing} posts that are not in kura yet")
        else:
            record.pool(pool["id"], status=status, error=_detail(reply)[:200])
            counts["pools_failed"] += 1
            say(f"  pool {pool['id']} ({name}): {status} {_detail(reply)[:160]}")


def main(argv=None) -> int:
    args = arguments(argv)
    api = Api(args.url, args.ca_file)       # first: installs the opener that refuses redirects, for both sides
    src = Szurubooru(args.src, args.from_user,
                     private_file(args.from_token_file).strip() if args.from_token_file else None,
                     private_file(args.from_password_file).strip() if args.from_password_file else None)
    # The record belongs to one import: the same szurubooru, kura and artist category.
    identity = {"source": "szurubooru", "from": src.url, "url": args.url.rstrip("/"),
                "artist_category": args.artist_category}
    record_path = args.record or default_record(identity)
    record = Record(record_path, identity, create=not args.dry_run)

    # -sort:id is szurubooru's ascending id order (sort:id is newest first), so kura's ids follow it
    posts = src.pages("posts/", "id,contentUrl,tags,safety,source,mimeType", "-sort:id")
    pools = [] if args.skip_pools else src.pages("pools/", "id,names,description,posts")
    todo = [p for p in posts if p["id"] not in record.posts]
    say(f"{src.url}: {len(posts)} posts ({len(posts) - len(todo)} already moved), {len(pools)} pools "
        f"(record: {record_path})")
    if args.dry_run:
        for p in todo[:20]:
            print(f"post {p['id']}  {p.get('mimeType', '')}  {p.get('safety')}  {' '.join(micro_tag_names(p.get('tags', [])))}")
        if len(todo) > 20:
            print(f"… and {len(todo) - 20} more")
        for pool in pools:
            print(f"pool {pool['id']}  {pool['names'][0] if pool.get('names') else ''}  {len(pool.get('posts', []))} posts")
        return 0

    counts = {"posts": len(posts), "added": 0, "duplicates": 0, "failed": 0, "unsupported": 0,
              "skipped": len(posts) - len(todo), "tags_recategorized": 0, "pools_created": 0,
              "pools_completed": 0, "pools_existing": 0, "pools_failed": 0}
    tmp = Path(tempfile.mkdtemp(prefix="kura-migrate-"))     # before sign-in: nothing to undo if it fails
    started = time.monotonic()
    try:
        if args.token_file:
            env = env_lines(private_file(args.token_file))
            if not env.get("KURA_USER") or not env.get("KURA_TOKEN"):
                raise SystemExit(f"{args.token_file}: needs KURA_USER and KURA_TOKEN")
            api.authorization = token_header(env["KURA_USER"], env["KURA_TOKEN"])
        else:
            api.sign_in(args.email, private_file(args.key_file).strip())
        if move_posts(src, api, record, todo, tmp, args.artist_category, counts):
            if not args.skip_tag_categories:
                copy_tag_categories(api, posts, counts)
            move_pools(api, record, pools, counts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        api.sign_out()
    counts["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(counts))
    return 1 if counts["failed"] or counts["pools_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
