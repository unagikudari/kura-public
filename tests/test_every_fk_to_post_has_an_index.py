"""post を参照する FK には index が要る — 無いと 1 件の削除が全走査になる。

2026-09-05 の実害。`DELETE FROM post` が 806ms/回。原因は `post.duplicate_of` の
自己参照 FK に index が無く、親 1 行を消すたびに **post 自身 (102 万行 / 130GB) を
Parallel Seq Scan して 0 行を返していた** こと。index を足して 806ms → 21ms。

この形が厄介なのは症状が出にくいところで:

  ・`duplicate_of` は 1,026,600 行中 **708 行**しか使っていない。ほぼ常に NULL。
  ・FK 検査は「参照している子が居るか」を訊くだけなので、**答えは毎回 0 行**。
    つまり結果は正しく、遅いだけ。
  ・post の削除は dedup/cull のときだけ走るので、人が待つ画面には出ない。

だから「動いている」を見ても気づけない。**FK を足した瞬間に index も要る**という
関係を、コードではなくスキーマから導いて検査する。

適用範囲: post を参照する FK に限定する。fleet の他の表にも同じ問題はありうるが、
ここが 130GB で圧倒的に高くつくので、まずこの表を守る。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

API_ENV = Path(__file__).resolve().parent.parent / "api" / "env"


def _dsn() -> str | None:
    if os.environ.get("KURA_TEST_DSN"):
        return os.environ["KURA_TEST_DSN"]
    if not API_ENV.exists():
        return None
    m = re.search(r"^DATABASE_URL=(.*)$", API_ENV.read_text(), re.M)
    return m.group(1).strip().strip('"').strip("'").replace("+asyncpg", "") if m else None


def _conn():
    dsn = _dsn()
    if not dsn:
        pytest.skip("kura DSN が読めない: KURA_TEST_DSN を設定するか api/env を置くこと")
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        return psycopg2.connect(dsn)
    except Exception as e:  # サービスが落ちている / 認証が変わった
        pytest.skip(f"kura DB not reachable: {type(e).__name__}")


#: 参照側の列が index の **先頭** に無ければ FK 検査は使えない。
#: (複合 index の 2 列目に居ても、その列単独の等値検索には効かない)
_UNINDEXED_FK_SQL = """
SELECT c.conrelid::regclass::text AS child, a.attname AS col
  FROM pg_constraint c
  JOIN unnest(c.conkey) WITH ORDINALITY k(attnum, ord) ON k.ord = 1
  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
 WHERE c.contype = 'f'
   AND c.confrelid = 'post'::regclass
   AND NOT EXISTS (
       SELECT 1 FROM pg_index i
        WHERE i.indrelid = c.conrelid
          AND i.indkey[0] = a.attnum
   )
 ORDER BY child
"""


def test_every_fk_to_post_has_a_leading_index():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_UNINDEXED_FK_SQL)
            missing = cur.fetchall()
    finally:
        conn.close()
    assert not missing, (
        "post を参照する FK に index がありません — 削除のたびに子テーブルを全走査します: "
        + ", ".join(f"{t}.{c}" for t, c in missing)
        + "\n"
        "post は 130GB なので、自己参照 (post.duplicate_of) が抜けると 1 件の削除が "
        "806ms になります (2026-09-05 実測、index 追加後 21ms)。\n"
        "Fix: CREATE INDEX CONCURRENTLY ix_<table>_<col> ON <table> (<col>) "
        "WHERE <col> IS NOT NULL;  — FK 検査は非 NULL しか引かないので部分 index で足ります。"
    )


def test_the_check_can_actually_see_foreign_keys():
    """検査対象がゼロなら、この test は何も守っていない (空振りの固定)。"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_constraint "
                " WHERE contype='f' AND confrelid='post'::regclass"
            )
            n = cur.fetchone()[0]
    finally:
        conn.close()
    assert n >= 10, f"post を参照する FK が {n} 本しか見えない — 検査が対象を見失っている"
