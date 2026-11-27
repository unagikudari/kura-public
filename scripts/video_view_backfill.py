#!/usr/bin/env python3
"""Create compatible MP4 views for WebM posts in an explicitly selected pool.

Dry-run by default. --apply publishes immutable files, then attaches them using
existing rendition tables. Original files/identity/metadata remain unchanged;
post.version is incremented to invalidate the old content URL. Existing views
are skipped. Requires normal DATABASE_URL and media configuration in environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.services import files, renditions, video_view


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.digest()


def run(conn, pool, apply=False):
    with conn.cursor() as c:
        c.execute('''SELECT p.id,p.sha256,p."mime-type" FROM post p
            JOIN pool_post pp ON pp.post_id=p.id
            WHERE pp.pool_id=%s AND p."mime-type"='video/webm'
            AND NOT EXISTS(SELECT 1 FROM post_rendition r WHERE r.post_id=p.id AND r.kind='view')
            ORDER BY pp.ord''', (pool,))
        rows = c.fetchall()
    conn.rollback()
    for pid, sha, mime in rows:
        source = files.content_path(pid, mime)
        expected = bytes(sha) if sha else None
        if not expected or digest(source) != expected:
            raise ValueError(f'post {pid}: original hash mismatch')
        if not apply:
            yield {'post_id': pid, 'action': 'would_attach', 'original_bytes': source.stat().st_size}
            continue
        root = renditions.ROOT
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < (2 << 30) + source.stat().st_size * 10:
            raise RuntimeError('insufficient free space for video view')
        try:
            with tempfile.TemporaryDirectory(prefix='.video-view-', dir=root) as temp:
                output = Path(temp) / 'view.mp4'
                video_view.encode(source, output)
                if digest(source) != expected:
                    raise ValueError(f'post {pid}: original changed during encode')
                stored = renditions.store_video(output)
        except video_view.DurationUnavailable:
            yield {'post_id': pid, 'action': 'skipped', 'reason': 'duration_unavailable'}
            continue
        with conn, conn.cursor() as c:
            c.execute('SELECT sha256,"mime-type" FROM post WHERE id=%s FOR UPDATE', (pid,))
            current = c.fetchone()
            if not current or bytes(current[0]) != expected or current[1] != mime:
                raise ValueError(f'post {pid}: identity changed during encode')
            c.execute("SELECT 1 FROM post_rendition WHERE post_id=%s AND kind='view'", (pid,))
            if c.fetchone():
                result = {'post_id': pid, 'action': 'concurrent_view_preserved'}
            else:
                obj, ref = renditions.attach_params(pid, 'view', stored, video_view.RECIPE)
                c.execute(renditions.psycopg(renditions.UPSERT_OBJECT), obj)
                c.execute(renditions.psycopg(renditions.ATTACH), ref)
                c.execute('UPDATE post SET version=version+1 WHERE id=%s', (pid,))
                result = {'post_id': pid, 'action': 'attached', 'original_sha256': expected.hex(),
                          'view_sha256': stored.sha256.hex(), 'view_bytes': stored.byte_size,
                          'width': stored.width, 'height': stored.height}
        yield result


def main():
    import psycopg2
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--pool', type=int, required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
    conn = psycopg2.connect(url)
    try:
        for result in run(conn, a.pool, a.apply):
            print(json.dumps(result), flush=True)
    finally:
        conn.close()

if __name__ == '__main__': main()
