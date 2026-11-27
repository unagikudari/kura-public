#!/usr/bin/env python3
"""Turn local-VLM tag-fix proposals into fixed human review sets (owner 2026-11-22:
「ローカルモデルにそこらへんで気づいたものは直す提案させて。後でまとめて人間が了承する」「それもタグ修正の経路にしよう」).

A screening job that already asks the local VLM about a picture compares the answer with the post's tags and appends
one JSON line per disagreement to a proposals file:

    {"post": 941321, "tag": "censored", "action": "remove", "why": "...", "model": "...", "at": "..."}

Nothing in that file is a label. This script freezes the file (receipt outside the queue directory), groups the
proposals by tag, and writes one `human-review-set/1` per tag (purpose `boundary_recheck`) into the configured review inventory, so the existing /tagger-dashboard#review page shows each tag's posts with the normal human tag controls.
A human pin is the only thing that changes a tag (docs/notes/fixed-human-review-sets.md).

    vlm_tag_proposals.py PROPOSALS.jsonl --model-sha256 HEX [--publish] [--queue-dir DIR] [--receipt-dir DIR]

Without --publish it only prints the sets it would write. Tags beyond the queue directory's 20-set limit are left
out (the largest tags go first) and named; publish them after older sets are retired.
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.services.review_queues import parse_queues, read_queue_blobs  # noqa: E402

MAX_SETS, MAX_IDS = 20, 500


def read(path):
    raw = Path(path).read_bytes()
    rows = []
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        x = json.loads(line)
        if not (isinstance(x.get('post'), int) and isinstance(x.get('tag'), str) and x.get('action') in ('add', 'remove')):
            raise SystemExit(f'proposal line invalid: {line[:120]}')
        rows.append(x)
    return raw, rows


def sets_from(rows, frame, model, room):
    by_tag = defaultdict(list)
    for x in rows:
        if x['post'] not in by_tag[x['tag']]:
            by_tag[x['tag']].append(x['post'])
    tags = sorted(by_tag, key=lambda t: -len(by_tag[t]))[:room]
    out = []
    for tag in tags:
        ids = by_tag[tag][:MAX_IDS]
        out.append({'schema': 'human-review-set/1', 'tag': tag, 'purpose': 'boundary_recheck',
                    'frame_sha256': frame, 'model_sha256': model, 'post_ids': ids})
    return out, [t for t in by_tag if t not in tags]


def write_queue_files(directory, incoming):
    for name, body in incoming.items():
        fd, part = tempfile.mkstemp(dir=directory.parent, prefix=".vlm-proposal-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.link(part, directory / name)
        finally:
            os.unlink(part)


async def plan_or_publish(args, raw, rows):
    from sqlalchemy import text
    from api.db.session import async_session
    from api.services import review_set_registry as registry

    frame = hashlib.sha256(raw).hexdigest()
    directory = Path(args.queue_dir).expanduser()
    async with async_session() as db:
        await db.execute(text("SET LOCAL statement_timeout='15s'"))
        await db.execute(text("SET LOCAL lock_timeout='2s'"))
        # Serialize legacy publication and the importer around activation.
        if args.publish:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0))"))
        active = await registry.available(db)
        existing = await registry.blobs(db) if active else read_queue_blobs(directory)
        existing = existing or {}
        proposed, _ = sets_from(rows, frame, args.model_sha256, len(rows))
        room = max(0, MAX_SETS - len(existing))
        incoming = {}
        left = []
        for queue in proposed:
            body = json.dumps(queue, sort_keys=True, separators=(",", ":")).encode()
            name = hashlib.sha256(body).hexdigest() + ".json"
            if name in existing:
                print(f"already published: {queue['tag']} {name}")
                continue
            if len(incoming) >= room:
                left.append(queue["tag"])
                continue
            incoming[name] = body
            print(f"{queue['tag']}: {len(queue['post_ids'])} posts")
        if left:
            print("not published (review inventory full):", ", ".join(left))
        if not incoming:
            return
        parse_queues(existing | incoming)
        if not args.publish:
            return

        receipts = Path(args.receipt_dir).expanduser()
        receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
        (receipts / f"{frame}.proposals.jsonl").write_bytes(raw)
        if active:
            await registry.import_sets(db, incoming)
            await db.commit()
        else:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_queue_files(directory, incoming)
        for name in incoming:
            print("published", name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("proposals")
    ap.add_argument("--model-sha256", required=True, help="sha256 of the VLM weights that made the proposals")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--queue-dir", default=os.environ.get("KURA_REVIEW_QUEUE_DIR", "~/.local/share/kura/review-queues"))
    ap.add_argument("--receipt-dir", default="~/.local/share/kura/review-queue-receipts")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[a-f0-9]{64}", args.model_sha256):
        raise SystemExit("--model-sha256 must be 64 hex")
    asyncio.run(plan_or_publish(args, *read(args.proposals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
