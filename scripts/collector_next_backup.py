#!/usr/bin/env python3
"""Verified snapshot of the experimental module, not a full legacy-library backup.

DB snapshot first, then immutable CAS originals. No GC/deletion runs in this core.
Thumbnails are disposable and can be regenerated from originals on restore.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse, unquote, parse_qs
from uuid import uuid4

import psycopg2

from api.collector.storage import content_path
from api.config import DATABASE_URL_SYNC


def pg_environment(dsn: str):
    url = urlparse(dsn)
    env = os.environ.copy()
    values = {"PGHOST": url.hostname or "localhost", "PGPORT": str(url.port or 5432),
              "PGUSER": unquote(url.username or "kura"), "PGDATABASE": url.path.lstrip("/"),
              "PGPASSWORD": unquote(url.password or "")}
    query = parse_qs(url.query)
    for key in ("host", "port"):
        if key in query:
            values[f"PG{key.upper()}"] = query[key][0]
    env.update(values)
    return env


def backup(dsn: str, root: Path, destination: Path, mount: Path):
    mount = mount.resolve()
    destination = destination.resolve()
    if not mount.is_mount() or not destination.is_relative_to(mount) or destination == mount:
        raise RuntimeError("バックアップ用ディスクのマウントと保存先を確認してください")
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        pending = destination / (".incomplete-" + stamp)
        pending.mkdir()
        env = pg_environment(dsn)
        with psycopg2.connect(host=env["PGHOST"], port=env["PGPORT"], user=env["PGUSER"],
                              password=env["PGPASSWORD"], dbname=env["PGDATABASE"]) as db:
            db.set_session(isolation_level="REPEATABLE READ", readonly=True)
            with db.cursor() as cursor:
                cursor.execute("SELECT pg_export_snapshot()")
                snapshot = cursor.fetchone()[0]
                cursor.execute("SELECT encode(sha256,'hex'),byte_size FROM collector_next.blob ORDER BY id")
                originals = cursor.fetchall()
                with (pending / "database.dump").open("wb") as dump:
                    subprocess.run(["pg_dump", "-Fc", "--no-owner", "--no-acl", "--schema=collector_next",
                                    "--snapshot=" + snapshot], env=env, stdout=dump, check=True)
                    dump.flush()
                    os.fsync(dump.fileno())
        # Keep each snapshot self-contained: originals are hard-linked from a verified
        # HDD CAS, then copied as ordinary files if exporting this snapshot elsewhere.
        manifest = []
        for sha, size in originals:
            source = content_path(root, bytes.fromhex(sha))
            target = content_path(destination / "objects", bytes.fromhex(sha))
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temp = target.with_name(target.name + ".tmp-" + uuid4().hex)
                try:
                    with source.open("rb") as inp, temp.open("xb") as out:
                        shutil.copyfileobj(inp, out, length=1024 * 1024)
                        out.flush()
                        os.fsync(out.fileno())
                    with temp.open("rb") as inp:
                        if temp.stat().st_size != size or hashlib.file_digest(inp, "sha256").hexdigest() != sha:
                            raise RuntimeError("バックアップ原本の検証に失敗しました")
                    os.link(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
            with target.open("rb") as inp:
                if target.stat().st_size != size or hashlib.file_digest(inp, "sha256").hexdigest() != sha:
                    raise RuntimeError("バックアップ済み原本が一致しません")
            relative = Path("originals") / sha[:2] / sha
            output = pending / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            os.link(target, output)
            manifest.append({"sha256": sha, "bytes": size, "path": str(relative)})
        with (pending / "database.dump").open("rb") as dump:
            dump_hash = hashlib.file_digest(dump, "sha256").hexdigest()
        with (pending / "manifest.json").open("w") as out:
            json.dump({"format": "kura-collector-backup-v1", "snapshot": stamp,
                       "database_sha256": dump_hash, "originals": manifest,
                       "thumbnails": "rebuildable"}, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        # syncfs equivalent for all files/directories on this one destination filesystem.
        subprocess.run(["sync", "-f", str(pending)], check=True)
        result = destination / stamp
        pending.rename(result)
        subprocess.run(["sync", "-f", str(destination)], check=True)
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--mount", type=Path, default=Path("/mnt/hd-sgda"))
    p.add_argument("--destination", type=Path, default=Path("/mnt/hd-sgda/collector-next-backup"))
    args = p.parse_args()
    print(backup(DATABASE_URL_SYNC, args.root, args.destination, args.mount))


if __name__ == "__main__":
    main()
