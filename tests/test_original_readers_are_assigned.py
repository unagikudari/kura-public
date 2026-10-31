"""Every code path that opens a name under DATA_DIR/posts is assigned a way to handle an
archived original on a node without the archive disk (docs/notes/kura-on-nodeb-plan.md,
"Originals on another host", rev 8). A new reader fails here until it is assigned."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PATTERN = r"""content_path\(|names\(["']content["']|storage_directory\(["']content["']|POSTS_DIR|/ ?["']posts["']"""

ASSIGNED = {
    "api/config.py": "defines POSTS_DIR",
    "api/services/files.py": "names; write/delete replace archive links, never go through them (managed_archive_target)",
    "api/main.py": "the /data/posts route (originals.fetch_async) and the startup FS checks (names only)",
    "api/routers/posts.py": "video thumbnail seek (videos are local) and delete (files.delete_all_names)",
    "api/routers/upload.py": "writes new local originals (files.write_all_names)",
    "api/services/post_files.py": "writes new local originals (files.write_all_names)",
    "api/services/tagging.py": "content_url extension by os.path.lexists (the name, not its target)",
    "scripts/original_archive.py": "the archiver itself",
    "scripts/kura_move.py": "orphans-remove only compares --source with the configured originals directory; reading and removal are original_archive's",
    "scripts/export_censor_gt.py": "originals.classify / original_bytes with the post's sha256",
    "scripts/library_recovery_backup.py": "originals.classify: archived links recorded, not copied, off the archive node",
    "scripts/fs_salt_migrate.py": "renames names (links are moved as links)",
    "scripts/backfill_fingerprints.py": "one-off backfill run on the archive node",
    "scripts/repair_posts.py": "oneshot repair run on the archive node: reads every name (an archived link resolves there only)",
    # collector-next has its own content store (not DATA_DIR/posts):
    "api/collector/export.py": "collector store",
    "api/collector/router.py": "collector store",
    "api/collector/storage.py": "collector store",
    "scripts/collector_next_backup.py": "collector store",
    # kura1 (database/) is an experiment that stays on primary and reads primary's copy:
    "database/annotate_server.py": "kura1 on primary",
    "database/import_probe_skeleton.py": "kura1 on primary",
    "database/model_runner.py": "kura1 on primary",
    "database/select_probe_corpus.py": "kura1 on primary",
}


def test_every_posts_reader_is_assigned():
    out = subprocess.run(["git", "-C", str(REPO), "grep", "-lE", PATTERN, "--", "*.py",
                          ":!tests/**", ":!scripts/legacy/**", ":!api/alembic/**"],
                         capture_output=True, text=True, check=True)
    found = set(out.stdout.split())
    assert found - set(ASSIGNED) == set(), (
        "new code opens DATA_DIR/posts: decide how it reads an archived original off the "
        f"archive node and add it to ASSIGNED: {sorted(found - set(ASSIGNED))}")
    # an entry for a file this checkout does not have (the public tree leaves out the
    # private ones) is not stale here
    present = {p for p in ASSIGNED if (REPO / p).exists()}
    assert present - found == set(), f"stale entries: {sorted(present - found)}"


def test_content_url_extension_does_not_follow_links():
    src = (REPO / "api" / "services" / "tagging.py").read_text()
    body = src[src.index("posts_dir = DATA_DIR / \"posts\""):]
    body = body[:body.index("return mime_ext")]
    assert "os.path.lexists(" in body and ".exists()" not in body
