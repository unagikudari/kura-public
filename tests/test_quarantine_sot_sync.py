"""tag_quarantine (DB) と QUARANTINE_TAGS (コード定数) の一致を強制する。

**なぜこのテストが要るか** (issue #55 census §5-6 / migration 0068):
0068 は「tag_quarantine を SoT にする」と宣言したが cut-over されず、`active` を
読むコードが無い dead state になっていた。完全 cut-over は worker 側への配信機構
が要るため保留 (api/tagger_core/config.py の裁定コメント参照) — それまでの間、**片方だけ
編集された分裂 SoT** が最悪の状態 (例: DB で active=false にしたのに infer は
定数を見て出力を捨て続け、reconciler だけ学習を dispatch する = 構造的に inert
な学習)。このテストは分裂を CI で落とし、両方を同 commit で変えることを強制する。

quarantine を触る手順: api/tagger_core/config.py の QUARANTINE_TAGS と DB の
tag_quarantine.active を**同時に**変更する (解除 = 定数から削除 + active=false)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

psycopg2 = pytest.importorskip("psycopg2")

from api.tagger_core.config import QUARANTINE_TAGS  # noqa: E402


def test_db_active_quarantine_matches_the_code_constant():
    try:
        conn = psycopg2.connect(host="127.0.0.1", dbname="kura", user="kura",
                                password="kura", connect_timeout=5)
    except Exception:  # noqa: BLE001
        pytest.skip("kura DB not reachable — sync gate runs where the schema lives")
    try:
        cur = conn.cursor()
        cur.execute("SELECT tag FROM tag_quarantine WHERE active")
        db_active = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    assert db_active == set(QUARANTINE_TAGS), (
        f"tag_quarantine (DB, active={sorted(db_active)}) と QUARANTINE_TAGS "
        f"(code, {sorted(QUARANTINE_TAGS)}) が分裂している — 両方を同 commit で"
        f"揃えること (api/tagger_core/config.py の裁定コメント参照)")
