import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tagger import dedicated_protocol as protocol, worker_trial_dispatch as dispatch


@pytest.mark.parametrize(
    "trial_release_id, release_id, fallback, refused",
    [(4, 4, True, True), (4, 3, False, True), (4, 4, False, False), (None, 3, True, False)],
)
def test_wd14_refresh_checks_trial_release_before_loading(
    monkeypatch, trial_release_id, release_id, fallback, refused,
):
    from tagger import character_policy, nodec_autotag_worker as worker

    torch = SimpleNamespace(load=Mock(side_effect=AssertionError("fixture must not load a model")))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "timm", SimpleNamespace())
    monkeypatch.setattr(worker, "auth_header", lambda: ({}, None))
    monkeypatch.setattr(worker.rc, "make_http_callbacks", lambda *args: (None, None))
    monkeypatch.setattr(worker.rc, "pull", lambda *args: SimpleNamespace(
        release_id=release_id, fallback=fallback, model_path="unused",
    ))
    loaded = object()
    state = SimpleNamespace(release_id=release_id, model=loaded)

    if refused:
        with pytest.raises(character_policy.CharacterReleaseChanged, match="worker trial release unavailable"):
            worker.refresh_model(state, trial_release_id=trial_release_id)
    else:
        worker.refresh_model(state, trial_release_id=trial_release_id)

    assert state.model is loaded
    assert state.release_id == release_id
    torch.load.assert_not_called()


def test_dispatch_transfers_only_declared_inputs_and_does_not_relaunch(tmp_path, monkeypatch):
    fixture_sha = hashlib.sha256(b"fixture").hexdigest()
    inputs = protocol.write_once(tmp_path / "worker-trial-inputs.json", {
        "schema": "tagger-worker-trial-inputs/1",
        "examples": [{"post_id": i, "display_sha256": fixture_sha} for i in (1, 2)],
    })
    plan = tmp_path / "plan.json"
    protocol.write_once(plan, {
        "schema": "tagger-worker-runtime-plan/1", "host": "primary", "profile": "full",
        "inputs_sha256": inputs["sha256"], "memory_limit_bytes": 6 * 1024**3,
        "deadline_s": 240, "threads": 2,
        "source_sha256": hashlib.sha256(b"tree\n").hexdigest(),
    })
    image_dir = tmp_path / "worker-trial-images"
    image_dir.mkdir()
    for name in ("1.bin", "extra.bin"):
        (image_dir / name).write_bytes(b"fixture")
    config = tmp_path / "nodes.json"
    config.write_text(json.dumps({"nodes": {"primary": dict(
        profiles=["full"], checkout="/srv/kura", trial_checkout="/srv/kura-trial", home="/home/worker",
        venv="/srv/venv", models_dir="/srv/models",
    )}}))
    monkeypatch.setattr(dispatch.socket, "gethostname", lambda: "controller")
    ssh = Mock(side_effect=["not-found", "", "", "", "tree", "", "", "loaded",
                            "not-found", "present", "not-found", "", "another-runtime-trial.service"])
    transfer = Mock()
    monkeypatch.setattr(dispatch, "ssh", ssh)
    monkeypatch.setattr(dispatch, "command", transfer)
    from tagger import display_bytes, vnext_arms
    conn = Mock()
    monkeypatch.setattr(vnext_arms, "connect_primary", lambda: conn)
    monkeypatch.setattr(display_bytes, "load", lambda query, ids: [(i, b"fixture") for i in ids])
    assert dispatch.dispatch(tmp_path, plan, node_inventory=config)["state"] == "submitted"
    assert (image_dir / "2.bin").read_bytes() == b"fixture"
    conn.set_session.assert_called_once_with(readonly=True)
    conn.close.assert_called_once()
    launch = ssh.call_args.args[1]
    assert "/srv/kura-trial" in launch and "EnvironmentFile=/srv/kura/tagger/env" in launch
    assert "/usr/bin/flock" in launch and "--nonblock" in launch
    assert "RuntimeMaxSec=240s" in launch and "MemoryMax=6442450944" in launch
    assert all("extra.bin" not in arg for arg in transfer.call_args.args[0])
    # Losing local staging after an ambiguous launch must not start a second unit.
    (tmp_path / "worker-trial-inputs.json").unlink()
    recovered = dispatch.dispatch(tmp_path, plan, node_inventory=config)
    assert recovered == {
        "host": "primary", "unit": launch[launch.index("--unit") + 1],
        "directory": launch[launch.index("--output") + 1].removesuffix("/result.json"),
        "state": "already_submitted",
    }
    assert dispatch.dispatch(tmp_path, plan, node_inventory=config) == recovered
    assert dispatch.dispatch(tmp_path, plan, node_inventory=config)["state"] == "busy"
    assert transfer.call_count == 3


def test_collect_requires_successful_unit_and_exact_result_binding(tmp_path, monkeypatch):
    from pathlib import Path
    import pytest

    report = dict(host="primary", profile="full", artifact_sha256="a" * 64, threshold=.5,
                  device="cpu", provider="CPUExecutionProvider", source_sha256="b" * 64,
                  inventory_sha256="c" * 64, inputs_sha256="d" * 64, memory_limit_bytes=2**32,
                  threads=2, deadline_s=60, total=2)
    path = tmp_path / "plan.json"
    plan = protocol.write_once(path, {"schema": "tagger-worker-runtime-plan/1", **report})
    result_path = tmp_path / "result.json"
    result = protocol.write_once(result_path, {"schema": "tagger-worker-runtime-trial/1",
                                                "plan_sha256": plan["sha256"], "report": report})
    receipt = dict(host="primary", unit="kura-worker-trial-" + plan["sha256"][:24] + ".service",
                   directory=str(Path("/trial") / plan["sha256"]))
    finished = "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0"
    missing = "LoadState=not-found\nActiveState=inactive\nSubState=dead"
    monkeypatch.setattr(dispatch.socket, "gethostname", lambda: "controller")
    ssh = Mock(side_effect=[
        "LoadState=loaded\nActiveState=activating\nResult=success",
        "LoadState=loaded\nActiveState=failed\nResult=oom-kill", "",
        finished, json.dumps(result),
        missing, "", missing, "present", json.dumps(result),
        missing, "present", json.dumps(dict(result, plan_sha256="f" * 64)),
    ])
    monkeypatch.setattr(dispatch, "ssh", ssh)
    assert dispatch.collect(receipt, path) == {"state": "running"}
    assert dispatch.collect(receipt, path) == {"state": "failed", "result": "oom-kill"}
    assert dispatch.collect(receipt, path)["report"] == report
    assert dispatch.collect(receipt, path) == {"state": "missing"}
    assert dispatch.collect(receipt, path)["report"] == report
    with pytest.raises(protocol.ProtocolError, match="result binding mismatch"):
        dispatch.collect(receipt, path)


def test_build_plans_replaces_only_candidate_and_pins_frozen_inputs():
    import pytest

    sha = "a" * 64
    inputs = dict(sha256="b" * 64, artifact_sha256=sha,
                  snapshot_sha256="c" * 64, threshold=.5, examples=[{}, {}])
    ctx = dict(tag="candidate", artifact_sha256=sha, metrics={
        "threshold": .5, "snapshot_sha256": inputs["snapshot_sha256"],
        "production_review": {"worker_trial_inputs_sha256": inputs["sha256"]},
    })
    old = [{"tag": "other", "content_sha256": "d" * 64},
           {"tag": "candidate", "content_sha256": "e" * 64}]
    candidate = dict(tag="candidate", content_sha256=sha, threshold=.5, apply_mode="replace")
    worker = dict(host="primary", profile="full", source_sha256="f" * 64, device="cpu",
                  provider="CPUExecutionProvider", memory_limit_bytes=2**32, threads=2, deadline_s=60)
    opts = dict(release_id=14, current_policies=old, candidate_policy=candidate, worker_settings=[worker])
    plan, = dispatch.build_worker_plans(ctx, inputs, **opts)
    assert plan["policies"] == [candidate, old[0]]
    assert plan["inventory_sha256"] == protocol.digest(plan["policies"])
    assert plan["inputs_sha256"] == inputs["sha256"] and plan["total"] == 2
    with pytest.raises(protocol.ProtocolError, match="candidate binding mismatch"):
        dispatch.build_worker_plans(ctx, dict(inputs, threshold=.6), **opts)


def test_dispatch_defers_before_transfers_when_gpu_capacity_is_reserved(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    protocol.write_once(plan, {
        "schema": "tagger-worker-runtime-plan/1", "host": "nodec", "profile": "wd14"
    })
    inventory = tmp_path / "nodes.json"
    inventory.write_text(json.dumps({"nodes": {"nodec": {
        "profiles": ["wd14"], "checkout": "/srv/kura", "home": "/home/worker",
        "trial_min_free_vram_mib": 3500,
    }}}))
    worker_command = Mock(side_effect=["not-found", "", "", "2576"])
    monkeypatch.setattr(dispatch, "_worker_command", worker_command)
    result = dispatch.dispatch(tmp_path, plan, node_inventory=inventory)
    assert result["state"] == "waiting_capacity"
    assert (result["free_vram_mib"], result["required_vram_mib"]) == (2576, 3500)
    assert worker_command.call_count == 4

    data = json.loads(inventory.read_text())
    data["nodes"]["nodec"]["trial_requires_yield"] = True
    inventory.write_text(json.dumps(data))
    worker_command.reset_mock(side_effect=True)
    worker_command.side_effect = ["not-found", "", "", "3650", "loaded\nactivating"]
    assert dispatch.dispatch(tmp_path, plan, node_inventory=inventory)["state"] == "waiting_yield"
    assert worker_command.call_count == 5


def test_failed_trial_reports_the_workers_own_reason(tmp_path, monkeypatch):
    from pathlib import Path
    from tagger import worker_trial

    report = dict(host="primary", profile="wd14", artifact_sha256="a" * 64, threshold=.5,
                  device="cpu", provider="CPUExecutionProvider", source_sha256="b" * 64,
                  inventory_sha256="c" * 64, inputs_sha256="d" * 64, memory_limit_bytes=2**32,
                  threads=2, deadline_s=60, total=2)
    path = tmp_path / "plan.json"
    plan = protocol.write_once(path, {"schema": "tagger-worker-runtime-plan/1", **report})
    worker_trial.record_failure(path, tmp_path / "result.json",
                                protocol.ProtocolError("worker trial provider mismatch"))
    written = (tmp_path / "failure.json").read_text()
    doc = json.loads(written)
    assert doc["plan_sha256"] == plan["sha256"] and doc["kind"] == "ProtocolError"
    assert not (tmp_path / "result.json").exists()  # never counts as evidence

    receipt = dict(host="primary", unit="kura-worker-trial-" + plan["sha256"][:24] + ".service",
                   directory=str(Path("/trial") / plan["sha256"]))
    failed = "LoadState=loaded\nActiveState=failed\nResult=exit-code"
    monkeypatch.setattr(dispatch.socket, "gethostname", lambda: "controller")
    other = json.loads(written)
    other["plan_sha256"] = "f" * 64
    other["sha256"] = protocol.digest({k: v for k, v in other.items() if k != "sha256"})
    monkeypatch.setattr(dispatch, "ssh", Mock(side_effect=[
        failed, written,
        failed, "",
        failed, json.dumps(other),           # another plan's failure is not this one's reason
        failed, written.replace("mismatch", "match"),  # tampered: digest no longer matches
    ]))
    observed = dispatch.collect(receipt, path)
    assert observed["state"] == "failed" and observed["result"] == "exit-code"
    assert observed["failure"]["reason"] == "worker trial provider mismatch"
    for _ in range(3):
        assert dispatch.collect(receipt, path) == {"state": "failed", "result": "exit-code"}


def test_record_failure_never_raises(tmp_path):
    from tagger import worker_trial

    worker_trial.record_failure(tmp_path / "missing-plan.json", tmp_path / "no-dir" / "result.json",
                                RuntimeError("x"))
    worker_trial.record_failure(tmp_path / "missing-plan.json", tmp_path / "result.json", RuntimeError("boom"))
    assert json.loads((tmp_path / "failure.json").read_text())["plan_sha256"] is None


def test_ssh_trouble_reading_the_reason_keeps_the_failed_observation(tmp_path, monkeypatch):
    from pathlib import Path
    from subprocess import CalledProcessError

    report = dict(host="primary", profile="wd14", artifact_sha256="a" * 64, threshold=.5,
                  device="cpu", provider="CPUExecutionProvider", source_sha256="b" * 64,
                  inventory_sha256="c" * 64, inputs_sha256="d" * 64, memory_limit_bytes=2**32,
                  threads=2, deadline_s=60, total=2)
    path = tmp_path / "plan.json"
    plan = protocol.write_once(path, {"schema": "tagger-worker-runtime-plan/1", **report})
    receipt = dict(host="primary", unit="kura-worker-trial-" + plan["sha256"][:24] + ".service",
                   directory=str(Path("/trial") / plan["sha256"]))
    monkeypatch.setattr(dispatch.socket, "gethostname", lambda: "controller")
    monkeypatch.setattr(dispatch, "ssh", Mock(side_effect=[
        "LoadState=loaded\nActiveState=failed\nResult=timeout", CalledProcessError(255, "ssh"),
        "LoadState=loaded\nActiveState=failed\nResult=timeout", "x" * 5000,
        "LoadState=loaded\nActiveState=failed\nResult=timeout", "[1, 2]",
    ]))
    for _ in range(3):
        assert dispatch.collect(receipt, path) == {"state": "failed", "result": "timeout"}


def test_main_clears_an_older_reason_records_the_new_one_and_reraises(tmp_path, monkeypatch):
    import pytest
    from tagger import worker_trial

    (tmp_path / "failure.json").write_text("stale")
    monkeypatch.setattr(worker_trial, "run", Mock(side_effect=protocol.ProtocolError("worker trial source mismatch")))
    monkeypatch.setattr("sys.argv", ["worker_trial", "--plan", str(tmp_path / "plan.json"), "--inputs", "i",
                                     "--images", "m", "--output", str(tmp_path / "result.json")])
    with pytest.raises(protocol.ProtocolError, match="source mismatch"):
        worker_trial.main()
    assert json.loads((tmp_path / "failure.json").read_text())["reason"] == "worker trial source mismatch"

    monkeypatch.setattr(worker_trial, "run", Mock(side_effect=SystemExit(0)))
    with pytest.raises(SystemExit):
        worker_trial.main()  # killed/exited without an exception of its own: no stale reason left
    assert not (tmp_path / "failure.json").exists()
