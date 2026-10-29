"""replica の版ずれは**向きで意味が変わる**。

■ 2026-08-08 実測

`replica_schema_matches` は単純な等値比較で、**blocking=True** だった。
その結果、私が 08-06 以降に採っている「published table を触る migration は
**replica を先に**当てる」という手順を踏むたびに E0 が上がり、測定が止まった
(3 回)。

この検査が守っているのは「canonical が先に進み、replica に無い列を送って
apply worker を止める」形 (2026-08-02、および 08-06 に WAL 18 MB が滞留した
実害)。**逆向きは危険ではなく、むしろ推奨される順序**。

正しい手順を踏むほど鳴る検査は、鳴っても情報を運ばない。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tagger.vnext_runner import _schema_drift_verdict as verdict  # noqa: E402


def test_canonical_ahead_is_the_dangerous_direction():
    """**本題の片側**: replica に無い列が送られる形。ここは止める。"""
    ok, why = verdict("0142", "0143")
    assert ok is False and why == "canonical_ahead"


def test_replica_ahead_is_the_recommended_order():
    """**本題のもう片側**: published table を触るときはこう当てる。止めない。"""
    ok, why = verdict("0143", "0142")
    assert ok is True and why == "replica_ahead"


def test_equal_is_fine_and_quiet():
    assert verdict("0143", "0143") == (True, None)


def test_unorderable_revisions_are_not_assumed_safe():
    """4 桁連番でない版は順序を判断しない。**分からないものを安全と読まない。**"""
    ok, why = verdict("abc", "0143")
    assert ok is False and why == "unordered_revisions"


def test_a_missing_reading_is_not_a_match():
    """読めなかったことを「一致していた」と読まない。"""
    assert verdict(None, "0143")[0] is False
    assert verdict("0143", None)[0] is False
