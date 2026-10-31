"""単一タグ検索の高速な総件数が、従来の集計と**実データで一致する**こと。

**なぜこのテストが要るか** (2026-09-05, user 報告「kura が重い、特にタグ一括編集時」):
/api/posts は毎ページで検索条件の全件に count(*) を掛けており、1girl (93 万件) で
598ms — HTTP 時間の 90% 以上がこれだった。tag.post_count を使えば 2.4ms で済むが、
post_count は「フィルタ無しの全数」なので endpoint の絞り (status='ok' / 既定の
advertisement 除外) を引かないと**静かに過大な件数を返す**。総件数は UI の仮想
スクロール範囲にも使われるので、ズレは表示だけの問題では終わらない。

「速いほうと遅いほうが同じ数を返す」を実データで固定するのが唯一の担保。
DB に届かない環境では skip する (このテストの対象は実データであり、green は
「検証した」を意味する必要がある)。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

psycopg2 = pytest.importorskip("psycopg2")

from api.routers.posts import (  # noqa: E402
    FAST_TOTAL_MIN, NON_OK_STATUSES, _build_where_clauses, _fast_total_single_tag,
)


def _run(coro_factory):
    """テスト専用の engine で 1 つの coroutine を回す。

    共有 engine (api.db.session.engine) は使わない — dispose すると後続テストを
    壊し、dispose しないと次の asyncio.run が別ループになって
    "attached to a different loop" で落ちる。専用 engine なら両方起きない。
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from api.config import DATABASE_URL

    async def _main():
        eng = create_async_engine(DATABASE_URL, echo=False, pool_size=1, max_overflow=0)
        maker = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        try:
            async with maker() as db:
                return await coro_factory(db)
        finally:
            await eng.dispose()

    return asyncio.run(_main())


def _conn():
    try:
        return psycopg2.connect(host="127.0.0.1", dbname="kura", user="kura",
                                password="kura", connect_timeout=5)
    except Exception:  # noqa: BLE001
        pytest.skip("kura DB not reachable — この検証の対象は実データ")


def test_non_ok_statuses_matches_the_db_check_constraint():
    """定数が CHECK からズレたら件数が狂う。DB 側を正として突き合わせる。"""
    conn = _conn()
    try:
        cur = conn.cursor()
        # `status` 列そのものに掛かる CHECK だけを取る。定義文の LIKE '%status%'
        # だと tagging_status (done/failed/skipped/pending) も拾ってしまう。
        cur.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) "
            "WHERE c.conrelid='post'::regclass AND c.contype='c' AND a.attname='status'")
        rows = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert rows, "post.status の CHECK が見つからない"
    allowed = set()
    for d in rows:
        allowed |= set(__import__("re").findall(r"'([a-z_]+)'::text", d))
    assert allowed, f"CHECK から値を読めない: {rows}"
    expected = allowed - {"ok"}
    assert set(NON_OK_STATUSES) == expected, (
        f"NON_OK_STATUSES={sorted(NON_OK_STATUSES)} が CHECK 由来の "
        f"{sorted(expected)} と一致しない — 件数が静かにズレる")


def _slow_total(tag: str) -> int:
    """従来経路と同じ絞りを SQL で直接。"""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            # The search meaning: the tag itself or a tag that directly implies it,
            # read from post_tag (the name arrays were retired in 0179).
            "WITH ids AS (SELECT tag_id FROM tag_name WHERE name = %s "
            "  UNION SELECT ti.parent_id FROM tag_implication ti "
            "  JOIN tag_name n ON n.tag_id = ti.child_id WHERE n.name = %s) "
            "SELECT count(*) FROM post p WHERE p.status='ok' "
            "AND p.id IN (SELECT pt.post_id FROM post_tag pt WHERE pt.tag_id IN (SELECT tag_id FROM ids)) "
            "AND NOT EXISTS (SELECT 1 FROM post_tag pt JOIN tag_name tn "
            "  ON tn.tag_id=pt.tag_id AND tn.name='advertisement' "
            "  WHERE pt.post_id=p.id)", (tag, tag))
        return cur.fetchone()[0]
    finally:
        conn.close()


def _big_tags(n: int = 3) -> list[str]:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT tn.name FROM tag t JOIN tag_name tn ON tn.tag_id=t.id AND tn.ord=0 "
            "WHERE t.post_count >= %s "
            "  AND NOT EXISTS (SELECT 1 FROM tag_implication ti WHERE ti.child_id=t.id) "
            "  AND tn.name <> 'advertisement' "
            "ORDER BY t.post_count DESC LIMIT %s", (FAST_TOTAL_MIN, n))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def test_fast_total_equals_slow_total_on_real_tags():
    """実タグで 速い経路 == 遅い経路。ここが一致しない修正は出してはいけない。"""
    tags = _big_tags()
    if not tags:
        pytest.skip(f"post_count >= {FAST_TOTAL_MIN} のタグが無い")

    async def run(db):
        out = []
        for t in tags:
            out.append((t, await _fast_total_single_tag(db, t, safe=False)))
        await db.rollback()
        return out

    for tag, fast in _run(run):
        assert fast is not None, f"{tag}: 高速経路が使われなかった"
        assert fast == _slow_total(tag), (
            f"{tag}: 高速={fast} / 従来={_slow_total(tag)} — 件数がズレている")


def test_fast_path_declines_where_it_cannot_be_exact():
    """適用外は必ず None (= 従来経路)。過大な件数を静かに返さない。"""
    big = (_big_tags(1) or ["1girl"])[0]

    async def run(db):
        cases = {
            "safe mode": (big, True),
            "複数トークン": (f"{big} solo", False),
            "否定": (f"-{big}", False),
            "特殊フィルタ": ("pool:1", False),
            "wildcard": ("1gir*", False),
            "advertisement": ("advertisement", False),
            "存在しないタグ": ("__no_such_tag_xyz__", False),
        }
        out = {k: await _fast_total_single_tag(db, q, safe=s)
               for k, (q, s) in cases.items()}
        await db.rollback()
        return out

    for label, got in _run(run).items():
        assert got is None, f"{label}: 高速経路が使われた (None であるべき) — got {got}"


def test_where_clause_parser_is_untouched_by_the_fast_path():
    """高速経路は解析器に触れない (回帰の巻き添えを避ける契約)。"""
    async def run(db):
        clauses, pool = await _build_where_clauses(db, "pool:12")
        await db.rollback()
        return clauses, pool

    clauses, pool = _run(run)
    assert pool == 12 and len(clauses) == 1


def test_advertisement_predicate_mirrors_the_endpoint():
    """ad 除外の判定を endpoint と同じ **部分文字列**で行う。

    endpoint は `if "advertisement" not in query.lower()` で除外を掛ける。
    fast path が完全一致で判定すると、`advertisement` を含む別タグを検索した
    ときに endpoint は除外しないのに fast path は 8 件引き、**過小**になる
    (primary-cc-primary の指摘 2026-09-05)。式が同じであることを固定する。
    """
    import inspect

    from api.routers import posts as m

    src = inspect.getsource(m._fast_total_single_tag)
    assert '"advertisement" not in query.lower()' in src, (
        "endpoint と同じ部分文字列判定になっていない — 完全一致に戻すと"
        "advertisement を含む別タグで件数が過小になる")
    # the endpoint takes its set from _listing_set (shared with get_post_index, #148)
    assert "_listing_set(" in inspect.getsource(m.list_posts)
    endpoint_src = inspect.getsource(m._listing_set)
    assert '"advertisement" not in query.lower()' in endpoint_src, (
        "endpoint 側の式が変わった — fast path 側も追随させること")
