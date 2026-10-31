#!/usr/bin/env python3
# Description: merge a post whose original is byte-identical to another post's into that post —
#              one image is one post; the contexts it appears in are pools (owner, 2026-10-25:
#              "それは pool で表現する。画像そのものが重複して保存されるのは違う")
# Effects: dry-run by default; --apply, per pair in one transaction: pool membership, pool covers
#          and the tags the representative lacks move to it; the duplicate becomes
#          status='exact_dup', duplicate_of=<representative>; both versions bumped
"""Merge exact duplicates into one post, the way szurubooru's post merge does, but reversibly.

Input: pairs "<duplicate> <representative>" (one per line), e.g. the "sha256 already held by
post N" refusals of scripts/repair_posts.py. For each pair, both post rows are locked in id
order; both originals (each the file whose SHA1 is its post's checksum) must be byte-equal, and
both posts must be ok. A duplicate whose checksum's file is gone merges only when the pair is in
--lost-original (checked by hand) and its files and served view are still what was checked
(journaled as dup_original_lost). Then:

- pool_post: the representative takes the duplicate's place (same pool, same ord); where it is
  already in that pool, the duplicate's row is dropped. Pool covers pointing at the duplicate
  point at the representative.
- post_tag: the tags the representative lacks are added as machine tags (audited as the auto
  tagger's are, actor merge_duplicate_posts:dup=N); one it has pinned absent stays absent.
- post_favorite: each person's favorite goes to the representative (one per person; one the
  representative already has keeps its own time), and the duplicate's rows go (#157).
- tagger_gt_label: the duplicate keeps none, so one image counts once in the training set
  (#158). Labels derived from its pins are retracted through tagger_governed_retract_pin_gt;
  a duplicate holding any other label (a pool seed, an implicit review) is refused.
- Human pins and GT labels are governed: a pair whose duplicate carries ones the
  representative lacks is refused and reported, not merged — unless --carry-pins (an owner-approved
  run): then each pin goes to the representative with its pinned_by and an audit row saying it was
  carried (an absent pin also takes the representative's machine tag off), and gt_sync derives the
  GT from it; GT no carried pin explains is still refused.
- The duplicate's history, tagger output and display renditions stay with it; it becomes
  status='exact_dup' with duplicate_of = the representative. The journal holds what was done
  (pool rows, covers, favorites, the tags and favorites actually added, old status and versions)
  for an undo.

Every change is journaled before its commit (scripts/repair_posts.py's Journal); a failure after
the plan is on disk stops the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from api.config import DATABASE_URL_SYNC  # noqa: E402
from api.services import files  # noqa: E402
from api.tagger_core import gt_lock  # noqa: E402
from scripts.repair_posts import Journal, Refused, Stop, _sync_dir, contents, current_rendition, original_bytes  # noqa: E402

LOG = Path(os.environ.get("KURA_MERGE_LOG", Path.home() / ".local/state/kura-merge-posts"))

LOCK = """
SELECT id, status, sha256, checksum, "mime-type" AS mime, version, duplicate_of FROM post
 WHERE id = ANY(%s) ORDER BY id FOR UPDATE
"""


def merge(pg, dup: int, rep: int, apply: bool, journal: Journal | None = None, lost_ok: dict | None = None,
          carry_pins: bool = False) -> dict:
    planned = []
    try:
        record = _merge(pg, dup, rep, apply, journal, planned, lost_ok or {}, carry_pins)
    except Refused:
        raise
    except Exception as e:
        if planned:
            raise Stop(f"pair {dup}->{rep}: outcome unknown after its planned record: {type(e).__name__}: {e}") from e
        raise
    if planned:
        try:
            journal.write({"dup": dup, "rep": record["rep"], "requested_rep": rep, "stage": "committed"})
        except Exception as e:
            raise Stop(f"pair {dup}->{rep}: committed, but the committed record failed: {e}") from e
    return record


def ok_root(pg, rep: int, limit: int = 10) -> int:
    """The ok post a representative was itself merged into: follow duplicate_of from an
    exact_dup to the post that is ok now (pairs found before an earlier merge name the post
    that was merged away)."""
    with pg.cursor() as db:
        for _ in range(limit):
            db.execute("SELECT status, duplicate_of FROM post WHERE id = %s", (rep,))
            row = db.fetchone()
            if row is None or row[0] != "exact_dup" or row[1] is None:
                return rep
            rep = row[1]
    raise Refused("duplicate_of does not lead to an ok post")


def served_view_sha256(db, pid: int) -> str | None:
    """sha256 of the view the API serves: the rendition, else the legacy column (the API's order)."""
    sha, body = current_rendition(db, pid, "view")
    if sha is not None:
        sha = hashlib.sha256(body).hexdigest()       # the bytes read, not the address they claim
    else:
        db.execute("SELECT data FROM post WHERE id = %s", (pid,))
        row = db.fetchone()
        sha = hashlib.sha256(bytes(row["data"])).hexdigest() if row and row["data"] is not None else None
    return sha


def _merge(pg, dup: int, rep: int, apply: bool, journal, planned: list, lost_ok: dict, carry_pins: bool) -> dict:
    requested = rep
    rep = ok_root(pg, rep)          # read in the transaction the merge below commits
    if dup == rep:
        raise Refused("a post cannot be merged into itself")
    with pg:
        with pg.cursor(cursor_factory=RealDictCursor) as db:
            if apply:
                # Before the row locks: a GT writer that read the duplicate as ok must not land a
                # label on it after this demotes it, and one that waited sees it demoted (#158).
                db.execute(gt_lock.SQL)
            db.execute(LOCK if apply else LOCK.replace("FOR UPDATE", ""), ([dup, rep],))
            rows = {r["id"]: r for r in db.fetchall()}
            if set(rows) != {dup, rep}:
                raise Refused("a post of the pair is gone")
            d, r = rows[dup], rows[rep]
            if r["status"] != "ok":
                raise Refused(f"the representative is {r['status']}, not ok")
            if d["status"] != "ok":
                raise Refused(f"the duplicate is already {d['status']}")
            # Both originals, each chosen by its own SHA1: the derived hash columns are known to be
            # wrong on some posts, so only the bytes prove the two posts hold one image.
            lost = None
            for pid in sorted((dup, rep)):
                with files.content_lock(pid):
                    if pid == rep:
                        rep_bytes, _ = original_bytes(rep, r["mime"], r["checksum"])
                    else:
                        found, names = contents(dup, d["mime"])
            # The duplicate's own original: its checksum's file, or, for a pair checked by hand
            # (--lost-original), the one content its files hold (a gallery pair,
            # 2026-10-25: posts whose recorded webp is lost and whose files are the other gallery's jpg).
            if d["checksum"] in found:
                dup_bytes = found[d["checksum"]]
            elif (dup, rep) in lost_ok and len(found) == 1:
                # Only a pair someone looked at: one content alone does not prove it is the lost
                # original, so the checked file content and served view must still be what was seen.
                ((sha1, dup_bytes),) = found.items()
                view = served_view_sha256(db, dup)
                seen = lost_ok[(dup, rep)]
                if (sha1, view) != (seen["files_sha1"], seen["view_sha256"]):
                    raise Refused("the lost-original pair changed since it was checked")
                lost = {"checksum": d["checksum"], "files": sorted(n for ns in names.values() for n in ns), **seen}
            else:
                raise Refused("original missing" if not found else "no file under the post's names has SHA1 = post.checksum")
            if dup_bytes != rep_bytes:
                raise Refused("the two originals differ")

            db.execute("SELECT pool_id, ord FROM pool_post WHERE post_id = %s ORDER BY pool_id", (dup,))
            dup_pools = [(p["pool_id"], p["ord"]) for p in db.fetchall()]
            db.execute("SELECT pool_id FROM pool_post WHERE post_id = %s", (rep,))
            rep_pools = {p["pool_id"] for p in db.fetchall()}
            db.execute("SELECT id FROM pool WHERE cover_post_id = %s ORDER BY id", (dup,))
            covers = [p["id"] for p in db.fetchall()]
            # Favorites are a person's choice of this image: they go with it to the representative
            # (#157), one per person, and a favorite the representative already has keeps its time.
            db.execute("SELECT user_id, time FROM post_favorite WHERE post_id = %s ORDER BY user_id", (dup,))
            dup_favs = [(f["user_id"], f["time"]) for f in db.fetchall()]
            db.execute("SELECT user_id FROM post_favorite WHERE post_id = %s", (rep,))
            rep_favs = {f["user_id"] for f in db.fetchall()}
            # Human pins and GT labels are governed (GT is written only through kura_reconciler):
            # a pair that would need them moved is reported, not merged here.
            db.execute("""SELECT tag_id, state, pinned_by FROM post_tag_pin WHERE post_id = %s
                          AND tag_id NOT IN (SELECT tag_id FROM post_tag_pin WHERE post_id = %s)
                          ORDER BY tag_id""", (dup, rep))
            pins = [(p["tag_id"], p["state"], p["pinned_by"]) for p in db.fetchall()]
            db.execute("""SELECT tag_id FROM tagger_gt_label g WHERE post_id = %s AND NOT EXISTS
                          (SELECT 1 FROM tagger_gt_label h WHERE h.post_id = %s AND h.tag_id = g.tag_id)""", (dup, rep))
            gt = [g["tag_id"] for g in db.fetchall()]
            # With --carry-pins (the owner's approval, 2026-10-25: 「手順で移していい」) the
            # duplicate's pins go to the representative as the person's decisions they are, and
            # the GT they explain follows from them through gt_sync's governed functions. GT that
            # no carried pin explains is still refused.
            unexplained = [g for g in gt if not carry_pins or g not in {p[0] for p in pins}]
            if (pins and not carry_pins) or unexplained:
                raise Refused(f"needs human pins {[p[:2] for p in pins]} / GT labels {unexplained or gt} moved: "
                              "the governed path, not this tool")
            # One image counts once in the training set (#158): the duplicate, hidden after this,
            # keeps no GT label. Labels derived from its pins (human_correction, no source pool)
            # are retracted through the governed per-post function; the representative's pins
            # (its own, or the ones carried) give it its labels on the next gt_sync. Any other
            # label on the duplicate (a pool seed, an implicit review) has no per-post governed
            # path, so the pair is refused rather than left counting twice.
            db.execute("""SELECT tag, verdict, derived_from, source_pool_id FROM tagger_gt_label
                          WHERE post_id = %s ORDER BY tag""", (dup,))
            dup_gt = [dict(g) for g in db.fetchall()]
            unretractable = [g for g in dup_gt if not (g["derived_from"] == "human_correction"
                                                       and g["source_pool_id"] is None)]
            if unretractable:
                raise Refused(f"the duplicate holds GT labels no governed per-post path can retract "
                              f"{[(g['tag'], g['derived_from']) for g in unretractable]}; it would count twice")
            record = {"dup": dup, "rep": rep, "requested_rep": requested, "dup_original_lost": lost,
                      "pins": [{"tag": p[0], "state": p[1], "pinned_by": p[2]} for p in pins],
                      "pools": [{"pool": p, "ord": o, "rep_already_member": p in rep_pools} for p, o in dup_pools],
                      "covers": covers,
                      "gt_retracted": [{"tag": g["tag"], "verdict": g["verdict"]} for g in dup_gt],
                      "favorites": [{"user": u, "time": t.isoformat() if hasattr(t, "isoformat") else str(t),
                                     "rep_already": u in rep_favs} for u, t in dup_favs],
                      "old": {"dup": {"status": d["status"], "version": d["version"],
                                      "duplicate_of": d["duplicate_of"]},
                              "rep": {"version": r["version"]}}}
            if not apply:
                return record

            for pool, ord_ in dup_pools:
                if pool in rep_pools:
                    db.execute("DELETE FROM pool_post WHERE pool_id = %s AND post_id = %s", (pool, dup))
                else:
                    db.execute("UPDATE pool_post SET post_id = %s WHERE pool_id = %s AND post_id = %s", (rep, pool, dup))
            if covers:
                # Set explicitly after the moves: a pool_post trigger may already have re-pointed a
                # cover, and then a WHERE cover_post_id = dup would match nothing.
                db.execute("UPDATE pool SET cover_post_id = %s WHERE id = ANY(%s)", (rep, covers))
                db.execute("SELECT count(*) AS n FROM pool WHERE id = ANY(%s) AND cover_post_id = %s", (covers, rep))
                if db.fetchone()["n"] != len(covers):
                    raise Refused("a pool cover did not end on the representative")
            fav_added = []
            if dup_favs:
                db.execute("""INSERT INTO post_favorite (post_id, user_id, time)
                              SELECT %s, user_id, time FROM post_favorite WHERE post_id = %s
                              ON CONFLICT DO NOTHING RETURNING user_id""", (rep, dup))
                fav_added = sorted(f["user_id"] for f in db.fetchall())
                db.execute("DELETE FROM post_favorite WHERE post_id = %s", (dup,))
            # Machine-added tags, like the auto tagger's; a tag the representative has pinned
            # absent is dropped by the pin guard and so is not among the rows returned.
            db.execute("""INSERT INTO post_tag (post_id, tag_id, locked)
                          SELECT %s, tag_id, locked FROM post_tag WHERE post_id = %s ON CONFLICT DO NOTHING
                          RETURNING tag_id""", (rep, dup))
            added = sorted(t["tag_id"] for t in db.fetchall())
            if added:
                db.execute("""INSERT INTO post_tag_audit (post_id, actor_type, actor_id, source, added_tags, removed_tags, created_at, host)
                              SELECT %s, 'script', %s, 'auto_tag',
                                     (SELECT array_agg(name ORDER BY name) FROM tag_name WHERE tag_id = ANY(%s) AND ord = 0),
                                     '{}', now(), %s""",
                           (rep, f"merge_duplicate_posts:dup={dup}", added, os.uname().nodename))
            carried = []
            for tag_id, state, pinned_by in pins:
                # An absent pin means the tag is not on the image: the representative's machine tag
                # goes (the pin guard only protects a present pin, which it has none of here). A
                # present pin needs the tag on the representative, merged above if the duplicate had it.
                removed = []
                if state == "absent":
                    db.execute("DELETE FROM post_tag WHERE post_id = %s AND tag_id = %s RETURNING tag_id", (rep, tag_id))
                    removed = [r["tag_id"] for r in db.fetchall()]
                else:
                    db.execute("SELECT 1 FROM post_tag WHERE post_id = %s AND tag_id = %s", (rep, tag_id))
                    if db.fetchone() is None:
                        raise Refused(f"a present pin on tag {tag_id} with the tag not on the representative")
                db.execute("""INSERT INTO post_tag_audit (post_id, actor_type, actor_id, source, added_tags, removed_tags, created_at, host)
                              SELECT %s, 'script', %s, 'manual', '{}',
                                     COALESCE((SELECT array_agg(name) FROM tag_name WHERE tag_id = ANY(%s) AND ord = 0), '{}'),
                                     now(), %s RETURNING id""",
                           (rep, f"merge_duplicate_posts:dup={dup} carries {pinned_by}'s {state} pin", removed,
                            os.uname().nodename))
                audit_id = db.fetchone()["id"]
                db.execute("""INSERT INTO post_tag_pin (post_id, tag_id, state, pinned_by, audit_id)
                              VALUES (%s, %s, %s, %s, %s)""", (rep, tag_id, state, pinned_by, audit_id))
                carried.append({"tag": tag_id, "state": state, "pinned_by": pinned_by, "audit_id": audit_id,
                                "rep_tag_removed": bool(removed)})
            for tag in sorted({g["tag"] for g in dup_gt}):
                db.execute("SELECT public.tagger_governed_retract_pin_gt(%s, %s) AS n", (tag, dup))
            db.execute("SELECT count(*) AS n FROM tagger_gt_label WHERE post_id = %s", (dup,))
            if db.fetchone()["n"]:
                raise Refused("the duplicate still holds GT labels after the retraction")
            db.execute("UPDATE post SET status = 'exact_dup', duplicate_of = %s, version = version + 1 WHERE id = %s",
                       (rep, dup))
            db.execute("UPDATE post SET version = version + 1 WHERE id = %s", (rep,))
            record["applied"] = {"tags_added": added, "pool_rows": record["pools"], "covers_set": covers,
                                 "favorites_added": fav_added, "favorites_moved_from_dup": len(dup_favs),
                                 "pins_carried": carried,
                                 "dup": {"status": "exact_dup", "duplicate_of": rep}}
            if journal is not None:
                journal.write({**record, "stage": "planned"})   # on disk before the commit
                planned.append(True)
    return record


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pairs", type=Path, help='a file of "<duplicate> <representative>" lines')
    p.add_argument("--apply", action="store_true", help="write (default: report only)")
    p.add_argument("--carry-pins", action="store_true",
                   help="move the duplicate's human pins to the representative (owner-approved runs only); the GT "
                        "they explain follows through gt_sync")
    p.add_argument("--lost-original", type=Path,
                   help="JSON lines {dup, rep, files_sha1, view_sha256, checked}: pairs checked by hand whose "
                        "duplicate's recorded original is gone; merged only while its files and view are unchanged")
    a = p.parse_args(argv)
    lost_ok = {}
    if a.lost_original:
        for line in a.lost_original.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                lost_ok[(e["dup"], e["rep"])] = {k: e[k] for k in ("files_sha1", "view_sha256", "checked")}
    pairs = [tuple(int(x) for x in line.split()[:2]) for line in a.pairs.read_text().splitlines() if line.strip()]
    if not LOG.exists():
        LOG.mkdir(parents=True, mode=0o700)
        _sync_dir(LOG.parent)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{os.getpid()}"
    journal = Journal(LOG / f"{stamp}-{'apply' if a.apply else 'dry'}.jsonl")
    pg = psycopg2.connect(DATABASE_URL_SYNC)
    counts = {"pairs": len(pairs), "merged" if a.apply else "ready": 0, "refused": 0}
    for dup, rep in pairs:
        try:
            r = merge(pg, dup, rep, a.apply, journal if a.apply else None, lost_ok, a.carry_pins)
            counts["merged" if a.apply else "ready"] += 1
            if not a.apply:
                journal.write(r)
        except Stop as e:
            print(json.dumps({**counts, "apply": a.apply, "log": str(journal.path), "stopped": str(e)}))
            return 1
        except Refused as e:
            journal.write({"dup": dup, "rep": rep, "refused": str(e)}); counts["refused"] += 1
        except Exception as e:
            pg.rollback()
            journal.write({"dup": dup, "rep": rep, "refused": f"error: {type(e).__name__}: {e}"[:300]}); counts["refused"] += 1
    print(json.dumps({**counts, "apply": a.apply, "log": str(journal.path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
