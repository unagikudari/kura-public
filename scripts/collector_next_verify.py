#!/usr/bin/env python3
"""Restore an alpha snapshot into a disposable PostgreSQL, never the live DB."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import psycopg2


def verify(snapshot: Path):
    snapshot = snapshot.resolve()
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if manifest.get("format") != "kura-collector-backup-v1":
        raise ValueError("Unknown snapshot format")
    dump = snapshot / "database.dump"
    with dump.open("rb") as inp:
        if hashlib.file_digest(inp, "sha256").hexdigest() != manifest["database_sha256"]:
            raise ValueError("Database dump checksum mismatch")
    expected = {}
    for original in manifest["originals"]:
        sha = original["sha256"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid original SHA256")
        relative = Path("originals") / sha[:2] / sha
        if str(relative) != original["path"] or not (snapshot / relative).resolve().is_relative_to(snapshot):
            raise ValueError("Unsafe original path")
        path = snapshot / relative
        with path.open("rb") as inp:
            if path.stat().st_size != original["bytes"] or hashlib.file_digest(inp, "sha256").hexdigest() != sha:
                raise ValueError("Original checksum mismatch")
        if sha in expected:
            raise ValueError("Duplicate manifest original")
        expected[sha] = original["bytes"]
    binary = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    with tempfile.TemporaryDirectory(prefix="kura-restore-") as folder:
        root = Path(folder)
        data, socket = root / "data", root / "socket"
        socket.mkdir()
        subprocess.run([str(binary / "initdb"), "-D", str(data), "-U", "collector_verify", "-A", "trust", "--no-locale"],
                       check=True, capture_output=True)
        subprocess.run([str(binary / "pg_ctl"), "-D", str(data), "-l", str(root / "log"), "-w", "start", "-o",
                        f"-k {socket} -p 65440 -c listen_addresses='' -c shared_buffers=16MB -c max_connections=10"],
                       check=True, capture_output=True)
        try:
            env = {**os.environ, "PGHOST": str(socket), "PGPORT": "65440", "PGUSER": "collector_verify", "PGPASSWORD": ""}
            subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "-d", "postgres", str(dump)],
                           env=env, check=True, capture_output=True)
            with psycopg2.connect(host=str(socket), port=65440, user="collector_verify", dbname="postgres") as db:
                with db.cursor() as cursor:
                    cursor.execute("SELECT encode(sha256,'hex'),byte_size FROM collector_next.blob")
                    if dict(cursor.fetchall()) != expected:
                        raise ValueError("Restored DB and original manifest differ")
                    cursor.execute("""SELECT
                        (SELECT count(*) FROM collector_next.asset),
                        (SELECT count(*) FROM collector_next.collection_entry),
                        (SELECT count(*) FROM collector_next.receipt)""")
                    assets, entries, receipts = cursor.fetchone()
                    import_checks = {}
                    cursor.execute("SELECT to_regclass('collector_next.import_operation')")
                    if cursor.fetchone()[0] is not None:
                        cursor.execute("""WITH counted AS (SELECT owner_id,operation_key,count(*) AS total,
                          count(*) FILTER(WHERE state=1) AS registered,
                          count(*) FILTER(WHERE state=2) AS rejected
                          FROM collector_next.import_item GROUP BY owner_id,operation_key)
                          SELECT 1 FROM collector_next.import_operation o LEFT JOIN counted c
                          ON c.owner_id=o.owner_id AND c.operation_key=o.request_key
                          WHERE o.item_count IS DISTINCT FROM c.total OR o.registered_count IS DISTINCT FROM c.registered
                            OR o.rejected_count IS DISTINCT FROM c.rejected LIMIT 1""")
                        if cursor.fetchone():raise ValueError('Restored import counters differ from items')
                        cursor.execute("""SELECT 1 FROM collector_next.import_item i
                          LEFT JOIN collector_next.receipt r ON r.owner_id=i.owner_id AND r.request_key=i.request_key
                          LEFT JOIN collector_next.asset a ON a.id=r.asset_id
                          LEFT JOIN collector_next.blob b ON b.id=a.blob_id
                          LEFT JOIN collector_next.collection_entry e ON e.id=r.entry_id
                          WHERE (i.state=1) IS DISTINCT FROM (r.request_key IS NOT NULL)
                            OR (i.state=1 AND (i.original_name IS DISTINCT FROM r.original_name
                              OR i.relative_path IS DISTINCT FROM r.relative_path OR i.sha256 IS DISTINCT FROM b.sha256
                              OR i.byte_size IS DISTINCT FROM b.byte_size OR i.collection_id IS DISTINCT FROM e.collection_id
                              OR i.collection_position IS DISTINCT FROM e.position)) LIMIT 1""")
                        if cursor.fetchone():raise ValueError('Restored import result differs from receipt or original')
                        cursor.execute('SELECT (SELECT count(*) FROM collector_next.import_operation),(SELECT count(*) FROM collector_next.import_item)')
                        operations,import_items=cursor.fetchone()
                        import_checks={'import_operations':operations,'import_items':import_items}
                    label_checks = {}
                    cursor.execute("SELECT to_regclass('collector_next.label_observation')")
                    if cursor.fetchone()[0] is not None:
                        cursor.execute("SELECT 1 FROM collector_next.label_operation WHERE policy_id<>1 LIMIT 1")
                        if cursor.fetchone():
                            raise ValueError("Unsupported label policy in snapshot")
                        cursor.execute("""WITH latest AS (
                          SELECT DISTINCT ON(asset_id,tag_id) owner_id,asset_id,tag_id,id,present
                          FROM collector_next.label_observation ORDER BY asset_id,tag_id,observed_at DESC,id DESC)
                          SELECT 1 FROM latest e FULL JOIN collector_next.label_head h USING(asset_id,tag_id)
                          WHERE e.id IS DISTINCT FROM h.observation_id OR e.present IS DISTINCT FROM h.present
                             OR e.owner_id IS DISTINCT FROM h.owner_id LIMIT 1""")
                        if cursor.fetchone():
                            raise ValueError("Restored label heads differ from immutable evidence")
                        cursor.execute("""WITH evidence AS (
                          SELECT owner_id,asset_id,count(DISTINCT request_key) AS revision,max(observed_at) AS at
                          FROM collector_next.label_observation GROUP BY owner_id,asset_id), positive AS (
                          SELECT owner_id,asset_id,count(*) AS count FROM collector_next.media_tag_current GROUP BY owner_id,asset_id)
                          SELECT 1 FROM collector_next.asset_label_state s FULL JOIN evidence e USING(owner_id,asset_id)
                          LEFT JOIN positive p ON p.asset_id=COALESCE(s.asset_id,e.asset_id) AND p.owner_id=COALESCE(s.owner_id,e.owner_id)
                          WHERE COALESCE(s.revision,0)<>COALESCE(e.revision,0) OR s.policy_id<>1
                            OR COALESCE(s.positive_count,0)<>COALESCE(p.count,0)
                            OR (e.asset_id IS NOT NULL AND s.last_observed_at IS DISTINCT FROM e.at) LIMIT 1""")
                        if cursor.fetchone():
                            raise ValueError("Restored label revisions or counts differ")
                        cursor.execute("""WITH counted AS (
                          SELECT owner_id,tag_id,count(*) AS count FROM collector_next.media_tag_current GROUP BY owner_id,tag_id)
                          SELECT 1 FROM collector_next.tag_usage u FULL JOIN counted c USING(owner_id,tag_id)
                          WHERE COALESCE(u.positive_count,0)<>COALESCE(c.count,0) LIMIT 1""")
                        if cursor.fetchone():
                            raise ValueError("Restored search counts differ")
                        cursor.execute("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='collector_next' AND table_name='tag_usage' AND column_name='logical_bytes')")
                        if cursor.fetchone()[0]:
                            cursor.execute("""WITH counted AS (
                              SELECT h.owner_id,h.tag_id,count(*) AS n,sum(b.byte_size) AS bytes
                              FROM collector_next.media_tag_current h JOIN collector_next.asset a ON a.id=h.asset_id
                              JOIN collector_next.blob b ON b.id=a.blob_id GROUP BY h.owner_id,h.tag_id)
                              SELECT 1 FROM collector_next.tag_usage u FULL JOIN counted c USING(owner_id,tag_id)
                              WHERE COALESCE(u.positive_count,0)<>COALESCE(c.n,0)
                                OR COALESCE(u.logical_bytes,0)<>COALESCE(c.bytes,0)
                                OR (u.representative_asset_id IS NOT NULL AND NOT EXISTS(
                                  SELECT 1 FROM collector_next.media_tag_current h WHERE h.owner_id=u.owner_id
                                    AND h.tag_id=u.tag_id AND h.asset_id=u.representative_asset_id)) LIMIT 1""")
                            if cursor.fetchone():raise ValueError("Restored tag summary bytes or representative differ")
                        cursor.execute("SELECT (SELECT count(*) FROM collector_next.label_observation),(SELECT count(*) FROM collector_next.media_tag_current)")
                        observations, current = cursor.fetchone()
                        label_checks = {"label_observations": observations, "current_labels": current}
            return {"verified": True, "originals": len(expected), "assets": assets,
                    "entries": entries, "receipts": receipts, **label_checks, **import_checks, "snapshot": str(snapshot)}
        finally:
            subprocess.run([str(binary / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    print(json.dumps(verify(parser.parse_args().snapshot), ensure_ascii=False))
