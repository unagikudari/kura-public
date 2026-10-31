"""0149 の trigger を**実際の PostgreSQL** に発火させて確かめる。

`KURA_TEST_DSN` が無ければ skip (test_mega_key_pending_db.py と同じ運用)。

確かめるのは「型」ではなく「発火」: sort=updated が読む pool.last_edit_time が
pool_post の INSERT / DELETE で実際に動くこと。動かなければ per-channel の取り込み
pool (v1.173.0) は追記を受けても一覧の「更新順」で浮上しない — それがこの
trigger の存在理由なので、bump しない regression はここで落ちる。
"""
from __future__ import annotations

import os

import pytest

KURA_TEST_DSN = os.environ.get("KURA_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not KURA_TEST_DSN,
    reason="set KURA_TEST_DSN to run the live-DB pool last_edit_time trigger test")


@pytest.fixture()
def conn():
    import psycopg2
    c = psycopg2.connect(KURA_TEST_DSN)
    c.autocommit = False
    yield c
    c.rollback()          # このファイルは一切コミットしない
    c.close()


def _mk_pool(cur) -> int:
    cur.execute("SELECT id FROM pool_category ORDER BY id LIMIT 1")
    cat = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pool (category_id, version, creation_time, last_edit_time) "
        "VALUES (%s, 1, now() - interval '1 day', now() - interval '1 day') "
        "RETURNING id", (cat,))
    return cur.fetchone()[0]


def _mk_post(cur) -> int:
    # 最小の post 行。NOT NULL 列はスキーマから読める既定で埋める。
    cur.execute("""
        INSERT INTO post (version, creation_time, safety, type, "mime-type",
                          checksum, status)
        VALUES (1, now(), 'unsafe', 'image', 'image/png',
                md5(random()::text), 'ok')
        RETURNING id""")
    return cur.fetchone()[0]


def _edit_time(cur, pool_id: int):
    cur.execute("SELECT last_edit_time FROM pool WHERE id = %s", (pool_id,))
    return cur.fetchone()[0]


def test_membership_insert_and_delete_bump_last_edit_time(conn):
    with conn.cursor() as cur:
        pid = _mk_pool(cur)
        post = _mk_post(cur)
        before = _edit_time(cur, pid)

        cur.execute(
            "INSERT INTO pool_post (pool_id, post_id, ord) VALUES (%s, %s, 0)",
            (pid, post))
        after_insert = _edit_time(cur, pid)
        assert after_insert > before, "pool_post INSERT が last_edit_time を bump しない"

        # DELETE も「更新」— 同一 transaction 内の now() は一定なので、bump の
        # 事実は目減りした値との比較でなく「1 日前の初期値より新しい」で見る。
        cur.execute("UPDATE pool SET last_edit_time = now() - interval '1 day' "
                    "WHERE id = %s", (pid,))
        cur.execute("DELETE FROM pool_post WHERE pool_id = %s AND post_id = %s",
                    (pid, post))
        after_delete = _edit_time(cur, pid)
        assert after_delete > before, "pool_post DELETE が last_edit_time を bump しない"
