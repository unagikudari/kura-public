"""テストが本番の broker に届かないことを、テストで固定する。

2026-08-08: owner の inbox に届いた E0 の測定パスが
`/tmp/pytest-of-desk/pytest-161/...` だった — **テスト実行が本番へ
escalation を送っていた**。`BROKER_URL` の既定が `http://localhost:8000`
= 本番で、runner/escalation 経路に触れるテスト 12 本のうち `broker` を
monkeypatch していたのは 1 本だけだった。

「各テストで patch を忘れない」は作法。**忘れても届かない**のが機構。
"""
import os


def test_the_broker_url_is_not_production_during_tests():
    """**本題**: 何もしていないテストから見た BROKER_URL が本番でないこと。"""
    url = os.environ.get("BROKER_URL", "")
    assert url, "conftest の autouse fixture が効いていない"
    assert "localhost:8000" not in url, "本番の broker を指したままテストが走る"
    assert "192.0.2." in url, "到達しないアドレスに倒されていない"


def test_the_module_constant_is_redirected_too():
    """import 済みモジュールが定数を束縛していても差し替わること。
    env だけ変えても、既に読み込まれた側は古い値を持つ。"""
    from tagger import vnext_runner as vr
    assert "localhost:8000" not in vr.BROKER_URL


def test_an_explicit_patch_still_wins():
    """broker とのやり取りを検証したいテストは今まで通り patch できること
    (guard が既存テストを壊さない)。"""
    from tagger import vnext_runner as vr
    seen = []
    orig = vr.broker
    try:
        vr.broker = lambda m, p, b=None: seen.append((m, p))
        vr.broker("POST", "/x")
        assert seen == [("POST", "/x")]
    finally:
        vr.broker = orig
