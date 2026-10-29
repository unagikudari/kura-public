"""関数が「そこに無い名前」を読んでいないことを静的に確かめる。

■ 2026-08-05 実測

`load_head_policies()` / `_fetch_artifact()` が `H` (認証ヘッダ) を素で参照して
いた。しかし `nodec_autotag_worker.py` の `H` は **関数引数か
`H, _ = auth_header()` の局所変数**で、モジュール変数としては存在しない。

    [kura-autotag] head-policies を取得できない (NameError) — head を適用しない

except が NameError を飲むので **「取得できない」としか出ず**、nodec/desk/nodeb の
3 ノードが `policy 0 件` = head を 1 件も当てない状態になっていた。今日の一連の
作業 (policy 由来化 / GC 修正 / 0135-0137) の効果が **タグ付けの 95% を担う
ノードに一切届いていなかった**。

呼び出さないと NameError にならないが、**名前解決は静的に分かる**。

■ 自作しない

最初これを自作の AST 走査で書いたら 37 ファイル中 34 で誤報した (内包表記の
束縛・入れ子スコープ・例外変数などを取りこぼす)。**誤報を出す検査器は本物の
欠陥まで信用されなくなる**ので、正しく実装された pyflakes を使う。
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _pyflakes(paths: list[str]) -> tuple[int, str]:
    return (lambda p: (p.returncode, p.stdout + p.stderr))(
        subprocess.run([sys.executable, "-m", "pyflakes", *paths],
                       capture_output=True, text=True))


pytestmark = pytest.mark.skipif(
    subprocess.run([sys.executable, "-c", "import pyflakes"],
                   capture_output=True).returncode != 0,
    reason="pyflakes 未導入 (検査を偽の green にしない)")


# `scripts` は対象外。既存の拡張の script が型注釈だけで
# `BrokerApiClient` を参照しており (定義はリポジトリ内に無い)、
# `from __future__ import annotations` があるので**実行時には評価されず落ちない**。
# 他人の未使用型注釈を黙って書き換えるより、対象を明示して外すほうが正直で、
# 検査を無効化するよりも狭い。tagger / api は 0 件で維持する。
@pytest.mark.parametrize("pkg", ["tagger", "api"])
def test_no_undefined_names(pkg: str):
    """**本題**: 未定義の名前を読んでいない。

    呼ばれたときにしか NameError にならず、except で飲まれると
    「動いていない」ことに気づけない (実測: 3 ノードが head 0 件で無音)。
    """
    d = REPO / pkg
    if not d.is_dir():
        pytest.skip(f"{pkg} が無い")
    _, out = _pyflakes([str(p) for p in sorted(d.rglob("*.py"))])
    bad = [l for l in out.splitlines() if "undefined name" in l.lower()]
    assert not bad, "\n".join(bad)


def test_the_checker_actually_reports_undefined_names(tmp_path):
    """通るだけの検査は無意味。**実際に踏んだ形**を検出できることを確かめる。"""
    f = tmp_path / "sample.py"
    f.write_text("import requests\nAPI='x'\n"
                 "def load_head_policies():\n"
                 "    return requests.get(API, headers=H)\n")
    _, out = _pyflakes([str(f)])
    assert "undefined name" in out.lower() and "'H'" in out
