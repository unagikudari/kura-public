"""秘密を repo から**取り除く** commit を、secret_gate が止めてはいけない。

2026-09-05 の gate 監査 (全履歴で測り直す方法を他 gate へ適用した回) で見つけた形。
`git diff --cached --name-only` は削除された path も返すので、素朴に見ると
「秘密が入ってしまった」という **この gate が存在する理由そのものの緊急事態**で、
その修正 commit (`git rm .env`) が BLOCK される。

実測: 全 1516 commit を実定数で掃いて誤検知は 0 件。ただしそれは **この種別が母集団に
1 件も無かったから**で (秘密は常に gitignore されていたため)、gate を直接評価すると
削除も BLOCK した。**率ゼロは「測った」ではない** — broker の changelog gate で
同じ形 (削除を報告する entry を種別ごと止める) を踏んだ直後に、同じ問いを他 gate へ
向けて見つけたもの。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "hooks" / "pre-commit"


def _load_secret_gate(deleted: set[str]):
    """hook から secret_gate を実体のまま取り出す (git は差し替える)。

    定数を写し取らない — 写すと、片方を変えた日にテストだけが緑になる。
    """
    src = HOOK.read_text(encoding="utf-8")
    ns: dict = {"os": os, "sys": sys,
                "git": lambda *a: "\n".join(sorted(deleted)) if "--diff-filter=D" in a else ""}
    exec(src[src.index("SECRET_NAMES"):src.index("def secret_gate")], ns)
    exec(src[src.index("def secret_gate"):src.index("# --- replica DDL")], ns)
    return ns["secret_gate"], ns


def test_adding_a_secret_is_blocked():
    gate, ns = _load_secret_gate(deleted=set())
    assert gate([".env"]) == 1
    assert gate(["some/dir/node_credential.json"]) == 1
    assert gate(["deploy/server.pem"]) == 1


def test_removing_a_secret_is_allowed():
    """gate が要る場面 (秘密が入った) で、その修正が通らないのは本末転倒。"""
    gate, _ = _load_secret_gate(deleted={".env"})
    assert gate([".env"]) == 0


def test_a_removal_does_not_excuse_an_addition_in_the_same_commit():
    """免除は削除された path だけ — 同じ commit で別の秘密を足すのは止める。"""
    gate, _ = _load_secret_gate(deleted={".env"})
    assert gate([".env", "new/.runner_key"]) == 1


def test_an_ordinary_file_is_never_touched_by_this_gate():
    gate, _ = _load_secret_gate(deleted=set())
    assert gate(["api/main.py", "README.md"]) == 0
