#!/usr/bin/env python3
"""blob.width / blob.height を実体から埋める (migration 0019 の実値側)。

なぜ必要か:
  「画面の向きはアスペクト比から自動的に定まる」(2026-07-28 指摘) — その通りだが、
  幅も高さもどこにも記録されていなかったので導きようが無かった。0019 で列を足し、
  ここで実体を読んで埋める。

読み方:
  blob.external_ref は data-images 上のパス (コピーしない設計)。Pillow で開いて
  size を取るだけ。**中身は変更しない** — 読み取り専用。

冪等: width が既に入っている blob は飛ばす (--force で再計測)。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import psycopg
from PIL import Image

DSN_DEFAULT = "host=127.0.0.1 port=5541 dbname=kura1 user=desk"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DSN_DEFAULT)
    ap.add_argument("--force", action="store_true", help="既に入っている blob も再計測")
    a = ap.parse_args()

    with psycopg.connect(a.dsn, autocommit=False) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("SELECT id, external_ref, width FROM blob ORDER BY id")
        rows = cur.fetchall()

        done = skipped = missing = failed = 0
        for bid, ref, w in rows:
            if w is not None and not a.force:
                skipped += 1
                continue
            p = pathlib.Path(ref)
            if not p.exists():
                print(f"  ! 実体が無い blob {bid}: {ref}", file=sys.stderr)
                missing += 1
                continue
            try:
                with Image.open(p) as im:
                    width, height = im.size
            except Exception as e:                      # noqa: BLE001 破損ファイルは飛ばす
                print(f"  ! 読めない blob {bid}: {e}", file=sys.stderr)
                failed += 1
                continue
            cur.execute("UPDATE blob SET width=%s, height=%s WHERE id=%s", (width, height, bid))
            done += 1
        c.commit()

    print(f"寸法を記録: {done} / 既存で飛ばした: {skipped} / 実体無し: {missing} / 読めない: {failed}")
    if done or skipped:
        with psycopg.connect(a.dsn) as c, c.cursor() as cur:
            cur.execute("SET search_path = kura, public")
            cur.execute(
                "SELECT CASE WHEN width>height THEN 'landscape'"
                "            WHEN width<height THEN 'portrait' ELSE 'square' END AS orient,"
                "       count(*) FROM blob WHERE width IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")
            for o, n in cur.fetchall():
                print(f"  {o:<10} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
