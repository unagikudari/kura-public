#!/usr/bin/env python3
"""**既に書かれた下書きが、いまの語彙で確定できるか**を確かめる。

なぜ要るか (2026-07-31 の実害 2 件):
  * 0059 で `horse_girl` を非推奨にしたとき、既存の下書き 6 件が確定できなくなった
  * 画面から来る空文字 ('') を NULL に落としていなかったので、関係を持つ下書き 6 件が
    INSERT で 500 になった (validate は通るので気付けない)

どちらも「**語彙や書き込み側を変えたときに、既に書いたものが取り残される**」形。
validate だけでは足りない — **投入まで走らせて rollback する**ことでしか判らない。

使い方: python3 database/drafts_are_committable.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import annotate_scene as A  # noqa: E402
import psycopg  # noqa: E402

DSN = os.environ.get("KURA1_DSN", "postgresql://desk@127.0.0.1:5541/kura1")
ANN_DIR = pathlib.Path(os.environ.get("KURA1_ANNOTATE_DIR",
                                          str(pathlib.Path.home() / "kura1" / "annotations")))


def main() -> int:
    files = sorted(ANN_DIR.glob("*.json"))
    bad = []
    with psycopg.connect(DSN, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        ont = A.Ontology(cur)
        for f in files:
            try:
                ann = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                bad.append((f.stem, f"読めない: {e}"))
                continue
            errs = A.validate(ann, ont)
            if errs:
                bad.append((f.stem, f"検証 {len(errs)} 件: {errs[0][:90]}"))
                continue
            # **投入まで走らせる。** validate を通っても INSERT で落ちることがある
            try:
                cur.execute("SAVEPOINT dac")
                A.write(cur, ann)
                cur.execute("ROLLBACK TO SAVEPOINT dac")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT dac")
                bad.append((f.stem, f"投入で例外 {type(e).__name__}: {str(e)[:90]}"))
        conn.rollback()

    for stem, why in bad:
        print(f"  NG  {stem}: {why}")
    print(f"下書き {len(files)} 件中、確定できないもの {len(bad)} 件")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
