"""#60: replica self-stamp の適格判定 — publisher-only 宣言だけが stamp できる。

**なぜこのテストが要るか** (2026-09-04, trainer-cc の申し送り ×3):
0153→0155 で publisher-only の stamp のみの依頼が 3 連続で人手往復した。
self-stamp は「全 pending revision が明示的に REPLICA_DDL=None を宣言」のとき
だけ許される — 未宣言 (分類されていない) を publisher-only と読み違えると、
DDL の要る migration を stamp だけで通し、apply crash-loop の窓を静かに開ける
(0148 型)。判定関数の境界をここで固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.replica_propagation import _UNDECLARED, all_publisher_only  # noqa: E402


def test_all_none_is_stampable():
    assert all_publisher_only([("0154", None), ("0155", None)])


def test_any_ddl_blocks_self_stamp():
    assert not all_publisher_only([("0154", None), ("0156", "ALTER TABLE x ADD c int;")])


def test_undeclared_blocks_self_stamp():
    """未宣言は「DDL 不要」ではなく「分類されていない」(P4) — stamp 不可。"""
    assert not all_publisher_only([("0154", None), ("0146", _UNDECLARED)])


def test_empty_is_not_stampable():
    """空 = 進める先が無い (DB AHEAD 含む) — stamp する対象が存在しない。"""
    assert not all_publisher_only([])
