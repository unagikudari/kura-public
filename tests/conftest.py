"""テストが本番の broker へ届かないようにする。

■ なぜ (2026-08-08 実測)

owner の inbox にこの escalation が届いた:

    [vnext escalation E0] tag=deepthroat
    構造前提が崩れている: replica_schema_matches
    測定: /tmp/pytest-of-desk/pytest-161/test_gt_unchanged_remeasures_w1/
          deepthroat.measurement.json

**測定ファイルのパスが pytest の一時ディレクトリ**。つまりテスト実行が
本番の broker に escalation を送っていた。

`tagger/vnext_runner.py` の

    BROKER_URL = os.environ.get("BROKER_URL", "http://localhost:8000")

は **既定が本番**で、テスト側が `broker` を monkeypatch し忘れるとそのまま
届く。実測すると runner/escalation の経路に触れるテスト 12 本のうち
**patch していたのは 1 本だけ**だった。

「各テストで patch を忘れない」は作法であって機構ではない。**忘れても届かない**
形にする — env を到達しない値に倒しておき、broker とのやり取りを検証したい
テストは今まで通り明示的に patch する (patch する側が勝つので既存テストは
そのまま動く)。

■ なぜ KURA_API は触らないか

こちらは読み取りが主で、実際に live API を叩くことを意図した統合テストが
既にある (test_upload_visibility 等)。**意図せず本番へ書き込むのは broker への
escalation/task 生成の側**なので、そこだけを塞ぐ。範囲を広げると「落ちる
理由が変わっただけ」のテストを増やす。
"""
import os
import sys
from pathlib import Path

import pytest

# repo root を **一度だけ** import path に載せる。
#
# これまでは各テストが自分で `sys.path.insert(0, REPO)` していた (20 数ファイル)。
# full run では最初に走った 1 本のおかげで後続も `api` / `tagger` を import できるが、
# **単体実行すると入っていない側は落ちる** — 実測 (2026-09-04, primary-cc):
# `pytest tests/test_refguard_covers_every_served_surface.py` が
# `ModuleNotFoundError: No module named 'api'`。full suite では緑。
# CI が file 単位や shard 実行に変わった瞬間に偽の赤になる。
# 各ファイルの insert は冪等なので残してある (消すのは別の掃除)。
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# 到達しない前提のアドレス。RFC 5737 の TEST-NET-1 (192.0.2.0/24) は
# ドキュメント用に予約されていて経路が無い — 万一 patch を忘れても
# 本番へ届かず、接続エラーとしてテスト側で顕在化する。
_UNREACHABLE = "http://192.0.2.1:1"


@pytest.fixture(autouse=True)
def _no_live_broker(monkeypatch):
    """**忘れても本番へ届かない。** 明示的に patch するテストはそちらが勝つ。"""
    monkeypatch.setenv("BROKER_URL", _UNREACHABLE)
    # モジュールが import 済みで定数を束縛している場合も差し替える。
    for mod in ("tagger.vnext_runner", "tagger.reconciler"):
        m = __import__(mod, fromlist=["_"]) if mod in _loaded() else None
        if m is not None and hasattr(m, "BROKER_URL"):
            monkeypatch.setattr(m, "BROKER_URL", _UNREACHABLE, raising=False)


def _loaded():
    import sys
    return sys.modules


def pytest_configure(config):
    """`torch` マーカーを登録する。

    未登録だと PytestUnknownMarkWarning が出るだけで **走ってしまう** — 綴りを間違えた
    マーカーが黙って無視されるのと同じ形。scripts/run_torch_tests.sh は
    `-m torch` で選ぶので、ここの登録がゲートの前提になる。
    """
    config.addinivalue_line(
        "markers",
        "torch: torch を要するテスト。scripts/run_torch_tests.sh が torch を持つ "
        "interpreter で走らせる (通常の pytest 実行では deselect される)")


def pytest_collection_modifyitems(config, items):
    """`torch` マーカーのテストは、`-m` で明示されない限り **deselect** する。

    skip ではない。skip は緑に見えて「走らなかった」を隠す (tests/test_mega_key_pending_db.py
    の言い方)。deselect は集計に `N deselected` として残るので、走らなかったことが見える。
    走らせるのは scripts/run_torch_tests.sh (`-m torch`、torch の無い環境では **失敗**)。
    素の pytest でこの 2 本が落ちて赤くなる状態が 1.185.2〜1.187.0 の間あった — 落ちる
    テストは無視され、無視されるテストは gate にならない。
    """
    if "torch" in (config.getoption("-m") or ""):
        return
    keep, drop = [], []
    for it in items:
        (drop if it.get_closest_marker("torch") else keep).append(it)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
