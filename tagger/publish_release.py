#!/usr/bin/env python3
# Description: publish a build_release.py output as the new active tagger release.
# Kind: tool
# Effects: mutating (writes to the kura Postgres DB)
# Requires: sqlalchemy[asyncio], asyncpg, api.config.DATABASE_URL
# Runs-on: primary (kura DB is a single instance, no replication policy applies)
"""publish_release.py — insert a tagger_release row and atomically flip the
active pointer, in one DB transaction (plan:kura-tagger-registry rev2 Step 4,
codex#1 / gemini#1: release atomicity).

Deliberately NOT an HTTP endpoint: publishing is a rare, privileged, audited
step (this script is what runs after build_release.py has already passed
every freeze check), whereas api/routers/tagger.py is the many-readers
DISTRIBUTION surface every node's release_client.py polls. Giving it a
separate direct-DB-write path keeps the read API simple and keeps "who is
allowed to publish" equal to "who can run this script", not "who holds an
API key".

Atomicity: INSERT INTO tagger_release + UPDATE tagger_release_active happen
inside ONE `engine.begin()` transaction. If anything raises before commit
(including a checksum mismatch caught here), NEITHER write lands -- there is
no state where a release row exists without also being the active pointer,
and no state where the active pointer names a release that failed to insert.

    python3 publish_release.py --build-dir /path/to/build/output [--created-by NAME]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class PublishError(Exception):
    pass


def _load_build(build_dir: Path) -> dict:
    release_path = build_dir / "release.json"
    if not release_path.is_file():
        raise PublishError(f"{release_path} not found -- run build_release.py first")
    release = json.loads(release_path.read_text())
    model_path = Path(release["model_path"])
    csv_path = Path(release["selected_tags_csv_path"])
    if not model_path.is_file() or not csv_path.is_file():
        raise PublishError(f"release.json names missing artifact(s): {model_path}, {csv_path}")

    model_bytes = model_path.read_bytes()
    actual_sha256 = hashlib.sha256(model_bytes).hexdigest()
    if actual_sha256 != release["model_sha256"]:
        raise PublishError(
            f"model file sha256 does not match release.json "
            f"(recorded={release['model_sha256']}, actual={actual_sha256}) -- "
            "artifact was modified after build, refusing to publish")

    return {
        **release,
        "model_bytes": model_bytes,
        "csv_bytes": csv_path.read_bytes(),
    }


async def publish(build_dir: Path, created_by: str | None) -> int:
    import sqlalchemy as sa  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    from api.config import DATABASE_URL  # noqa: PLC0415

    release = _load_build(build_dir)
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.begin() as conn:
            # tagger_release/tagger_release_active are governed (0079) --
            # tagger_governed_publish_release (0087) does the INSERT +
            # active-pointer flip (with correct generation increment,
            # which this script's pre-0087 raw UPDATE never did) as one
            # call; still inside this same client transaction, so the
            # original atomicity (release insert + active flip land
            # together or neither does) is unchanged.
            row = (await conn.execute(sa.text("""
                SELECT tagger_governed_publish_release(
                    :commit_sha, :base_model_repo, :base_model_revision, :patch_set_digest,
                    :model_sha256, :model_data, :selected_tags_csv, :n_stock, :n_out,
                    CAST(:patched AS jsonb), :lockfile, :created_by
                ) AS release_id
            """), {
                "commit_sha": release["commit_sha"],
                "base_model_repo": release["base_model_repo"],
                "base_model_revision": release["base_model_revision"],
                "patch_set_digest": release["patch_set_digest"],
                "model_sha256": release["model_sha256"],
                "model_data": release["model_bytes"],
                "selected_tags_csv": release["csv_bytes"],
                "n_stock": release["n_stock"],
                "n_out": release["n_out"],
                "patched": json.dumps(release["patched"]),
                "lockfile": release.get("lockfile"),
                "created_by": created_by,
            })).one()
            release_id = row[0]
            # registry sync: tagger_patch を patches/*.json + content-head に同期。
            # release publish と同一トランザクション = release と head registry が atomic。
            from tagger.sync_patch_registry import sync_registry  # noqa: PLC0415
            await sync_registry(conn)
            # Transaction commits here (engine.begin() context exit) -- release
            # insert + active-pointer flip land together, or neither does.
        return release_id
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("--created-by", default=None)
    args = ap.parse_args()
    try:
        release_id = asyncio.run(publish(Path(args.build_dir), args.created_by))
    except PublishError as e:
        print(f"[publish_release] REFUSED: {e}", file=sys.stderr)
        return 1
    print(f"published tagger_release id={release_id} (now active)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
