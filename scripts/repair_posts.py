#!/usr/bin/env python3
# Description: bring the posts an earlier ingest left behind to what the ingest path gives a post
#              today — identity hashes taken from the file, a view (animated WebP for an
#              animation) — so the original archive can move their originals
#              (docs/notes/kura-on-nodeb-plan.md, step 4 remedies)
# Effects: dry-run by default; --apply writes, per post in one transaction, post.sha256 /
#          checksum_md5 / type / media_location / "mime-type" / version and the post's view and
#          thumb renditions; every change and refusal is recorded
"""Repair posts one at a time, under the archive job's lock order.

For each candidate: lock the post row (FOR UPDATE), then the content-name lock; read the
original (every content name the post can have) and require
its SHA1 to equal post.checksum (other contents under its names are reported, untouched) —
anything else is refused and recorded, nothing changed. Then:

- hashes: sha256 / checksum_md5 set from the file where they differ; a sha256 another post
  already holds is refused, never merged;
- view: when the post has no view rendition or no media_location, or its original is animated
  and the view does not keep the motion (or the post is typed image), or a still original sits
  in an animation post — the same materialize call upload.py makes (animated WebP for more than
  one frame), the view and thumb renditions attached, type, media_location='pg' and mime set.

Any change bumps post.version so cached responses turn over. Candidates: posts without a view,
every animation post, and with --plan the archive plan's animated and SHA256 refusals.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402
from PIL import Image  # noqa: E402

from api.config import DATABASE_URL_SYNC  # noqa: E402
from api.services import files, renditions  # noqa: E402
from api.services.files import MIME_TO_EXT  # noqa: E402
from api.services.media_materialize import (  # noqa: E402
    materialize_animation_bytes, materialize_image_bytes, motion_signature)

LOG = Path(os.environ.get("KURA_REPAIR_LOG", Path.home() / ".local/state/kura-repair-posts"))

CANDIDATES = """
SELECT id FROM post p
 WHERE status = 'ok' AND type IN ('image', 'animation')
   AND (media_location IS NULL
        OR NOT EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'view'))
UNION
SELECT id FROM post WHERE status = 'ok' AND type = 'animation'
"""
LOCK_POST = """
SELECT id, type, status, checksum, checksum_md5, sha256, media_location, "mime-type" AS mime, version
  FROM post WHERE id = %s AND status = 'ok' AND type IN ('image', 'animation') FOR UPDATE
"""


class Refused(Exception):
    pass


class Stop(Exception):
    """A post whose commit may or did happen without its record completing: the run stops
    (exit 1) so it is reconciled against the database by its 'planned' line, never counted
    as a refusal."""


def plan_refusals(path: str) -> set[int]:
    plan = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return {pid for (pid,) in plan.execute(
        "SELECT pid FROM objects WHERE status = 'refused' AND "
        "(reason LIKE 'Animated%' OR reason LIKE 'Original SHA256 mismatch%')")}


def contents(pid: int, mime: str | None) -> tuple[dict[str, bytes], dict[str, list[str]]]:
    """Every content under every name the post can have: {sha1: bytes}, {sha1: [names]}."""
    found: dict[str, bytes] = {}
    names: dict[str, list[str]] = {}
    for path in dict.fromkeys(p for m in [mime, *MIME_TO_EXT] for p in files.names("content", pid, m)):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        sha1 = hashlib.sha1(data).hexdigest()
        found[sha1] = data
        names.setdefault(sha1, []).append(path.name)
    return found, names


def original_bytes(pid: int, mime: str | None, checksum: str) -> tuple[bytes, list[str]]:
    """The post's original: among every name it can have, the one content whose SHA1 is
    post.checksum. Other contents under its names (an older conversion left beside it, e.g. a
    .jpg next to the .png) are reported, not touched."""
    found, names = contents(pid, mime)
    if not found:
        raise Refused("original missing")
    if checksum not in found:
        raise Refused("no file under the post's names has SHA1 = post.checksum")
    others = sorted(n for sha1, ns in names.items() if sha1 != checksum for n in ns)
    return found[checksum], others


def current_rendition(db, pid: int, kind: str) -> tuple[str | None, bytes | None]:
    """(sha256 hex, bytes) of the post's rendition of `kind`, as the API serves it."""
    db.execute(renditions.psycopg(renditions.LOOKUP), {"post_id": pid, "kind": kind})
    obj = db.fetchone()
    if obj is None:
        return None, None
    sha = bytes(obj["sha256"])
    return sha.hex(), renditions.path_for(sha, obj["mime"]).read_bytes()


def decide(post: dict, data: bytes, view: bytes | None) -> tuple[dict, bool, bool]:
    """(hash changes, needs a new view, original is animated) for the original whose SHA1 is
    the post's checksum (original_bytes chose it)."""
    changes = {}
    sha256, md5 = hashlib.sha256(data).digest(), hashlib.md5(data).hexdigest()
    if bytes(post["sha256"] or b"") != sha256:
        changes["sha256"] = sha256
    if post["checksum_md5"] != md5:
        changes["checksum_md5"] = md5
    with Image.open(io.BytesIO(data)) as im:
        animated = bool(getattr(im, "is_animated", False))
        if view is None or post["media_location"] != "pg":
            need = True
        elif animated:
            need = post["type"] != "animation" or not _keeps_motion(im, view)
        else:
            need = post["type"] == "animation"
    return changes, need, animated


def _keeps_motion(original: Image.Image, view: bytes) -> bool:
    with Image.open(io.BytesIO(view)) as shown:
        if not getattr(shown, "is_animated", False):
            return False
        return motion_signature(original) == motion_signature(shown)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Journal:
    """Append-only JSON lines, created exclusively (never reopened over another run's) and
    fsynced line by line: what a post is about to become is on disk before its commit, and
    'committed' after it, so a stop between the two is visible and checkable."""

    def __init__(self, path: Path):
        self.f = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.path = path
        _sync_dir(path.parent)       # the file's name survives a power loss before any commit

    def write(self, value: dict) -> None:
        self.f.write(json.dumps(value) + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())


def repair(pg, pid: int, apply: bool, journal: Journal | None = None) -> dict:
    planned = []
    try:
        record = _repair(pg, pid, apply, journal, planned)
    except Refused:
        raise
    except Exception as e:
        if planned:   # the plan is on disk; whether the commit happened is unknown
            raise Stop(f"post {pid}: outcome unknown after its planned record: {type(e).__name__}: {e}") from e
        raise
    if planned:
        try:
            journal.write({"post": pid, "stage": "committed"})
        except Exception as e:
            raise Stop(f"post {pid}: committed, but the committed record failed: {e}") from e
    return record


def _repair(pg, pid: int, apply: bool, journal: Journal | None, planned: list) -> dict:
    with pg:
        with pg.cursor(cursor_factory=RealDictCursor) as db:
            db.execute(LOCK_POST if apply else LOCK_POST.replace("FOR UPDATE", ""), (pid,))
            post = db.fetchone()
            if post is None:
                raise Refused("no longer an ok image or animation post")
            with files.content_lock(pid):
                data, others = original_bytes(pid, post["mime"], post["checksum"])
                old_view_sha, view = current_rendition(db, pid, "view")
                old_thumb_sha, _ = current_rendition(db, pid, "thumb")
                changes, need, animated = decide(post, data, view)
                if "sha256" in changes:
                    # Only an ok post can hold it (ux_post_sha256_ok): an exact_dup already points at its
                    # ok representative, which is the one this post would duplicate.
                    db.execute("SELECT id FROM post WHERE sha256 = %s AND id <> %s AND status = 'ok' LIMIT 1",
                               (changes["sha256"], pid))
                    other = db.fetchone()
                    if other:
                        raise Refused(f"sha256 already held by post {other['id']}")
                record = {"post": pid, "view": need, "animated": animated, "other_contents": others,
                          "hashes": {k: (v.hex() if isinstance(v, bytes) else v) for k, v in changes.items()},
                          "old": {"sha256": bytes(post["sha256"]).hex() if post["sha256"] else None,
                                  "checksum_md5": post["checksum_md5"], "type": post["type"],
                                  "media_location": post["media_location"], "mime": post["mime"],
                                  "view": old_view_sha, "thumb": old_thumb_sha, "version": post["version"]}}
                if not apply or not (changes or need):
                    return record
                sets = dict(changes)
                new = {"version": post["version"] + 1}
                if need:
                    res = (materialize_animation_bytes if animated else materialize_image_bytes)(data)
                    if animated:
                        # The encoder can merge frames that differ too little (codex 1.277.0): a
                        # view that does not keep the motion is never attached as an animation.
                        with Image.open(io.BytesIO(data)) as im:
                            if not _keeps_motion(im, res["data"]):
                                raise Refused("the generated view does not keep the motion")
                    for kind, blob in (("view", res["data"]), ("thumb", res["thumbnail"])):
                        stored = renditions.store(blob)
                        obj, ref = renditions.attach_params(pid, kind, stored, renditions.LEGACY_RECIPE[kind])
                        db.execute(renditions.psycopg(renditions.UPSERT_OBJECT), obj)
                        db.execute(renditions.psycopg(renditions.ATTACH), ref)
                        new[kind] = bytes(stored.sha256).hex()
                    sets.update(type="animation" if animated else "image", media_location="pg",
                                **{"mime-type": res["mime"]})
                    new.update(type=sets["type"], media_location="pg", mime=res["mime"])
                columns = ", ".join(f'"{k}" = %s' for k in sets)
                db.execute(f"UPDATE post SET {columns}, version = version + 1 WHERE id = %s",
                           [*sets.values(), pid])
                record["new"] = {**new, **record["hashes"]}
                if journal is not None:
                    journal.write({**record, "stage": "planned"})   # on disk before the commit
                    planned.append(True)
    return record


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write (default: report only)")
    p.add_argument("--plan", help="an original archive plan whose animated / SHA256 refusals are candidates too")
    p.add_argument("--pids", type=Path, help="only these post ids (one per line), instead of the candidates")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--shard", default="0/1", help="k/n: only posts with id %% n == k (run n in parallel)")
    a = p.parse_args(argv)
    k, n = (int(x) for x in a.shard.split("/"))
    pg = psycopg2.connect(DATABASE_URL_SYNC)
    if a.pids:
        # e.g. the representatives whose own sha256 is what made a true non-duplicate collide
        ids = {int(line.split()[0]) for line in a.pids.read_text().splitlines() if line.strip()}
    else:
        with pg, pg.cursor() as q:
            q.execute(CANDIDATES)
            ids = {r[0] for r in q.fetchall()}
        if a.plan:
            ids |= plan_refusals(a.plan)
    ids = sorted(i for i in ids if i % n == k)[: a.limit or None]
    if not LOG.exists():
        LOG.mkdir(parents=True, mode=0o700)
        _sync_dir(LOG.parent)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{os.getpid()}"
    journal = Journal(LOG / f"{stamp}-{'apply' if a.apply else 'dry'}-{k}of{n}.jsonl")
    log = journal.path
    counts = {"candidates": len(ids), "unchanged": 0, "hashes": 0, "view": 0, "refused": 0}
    for pid in ids:
        try:
            r = repair(pg, pid, a.apply, journal if a.apply else None)
            counts["hashes"] += bool(r["hashes"]); counts["view"] += r["view"]
            counts["unchanged"] += not (r["hashes"] or r["view"])
            if not a.apply:
                journal.write(r)
        except Stop as e:
            print(json.dumps({**counts, "apply": a.apply, "log": str(log), "stopped": str(e)}))
            return 1
        except Refused as e:
            journal.write({"post": pid, "refused": str(e)}); counts["refused"] += 1
        except Exception as e:  # recorded and counted; one post never stops the run
            pg.rollback()
            journal.write({"post": pid, "refused": f"error: {type(e).__name__}: {e}"[:300]}); counts["refused"] += 1
    print(json.dumps({**counts, "apply": a.apply, "log": str(log)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
