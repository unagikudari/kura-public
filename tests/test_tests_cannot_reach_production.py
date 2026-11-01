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


def test_the_runner_has_no_broker_to_reach():
    """2026-11-01: the runner no longer talks to a broker at all (it died on
    2026-09-23; tagger/learn.py feeds the runner from the lifecycle ledger).
    The guard above stays for anything else that still names BROKER_URL; the
    runner is out of its reach by construction."""
    from tagger import vnext_runner as vr
    assert not hasattr(vr, "BROKER_URL")
    assert not hasattr(vr, "broker")


def test_the_runner_hands_registrations_over_instead_of_sending_them():
    """Only the holder of the lease registers (docs/notes/tagger-learn-local.md
    step 3d), so a test exercising the runner cannot reach the API either."""
    from tagger import vnext_runner as vr
    vr._PAYLOADS.clear()
    try:
        assert vr._register({"tag": "x"}) == {"id": None}
        assert vr._PAYLOADS == [{"tag": "x"}]
    finally:
        vr._PAYLOADS.clear()
