"""Run a frozen candidate trial in a dedicated, memory-limited systemd unit."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time

from . import dedicated_protocol as protocol
from . import dedicated_qualification as qualification


def _require(condition, reason):
    if not condition:
        raise protocol.ProtocolError(reason)


def memory_cgroup():
    lines = Path("/proc/self/cgroup").read_text().splitlines()
    paths = [line[3:] for line in lines if line.startswith("0::/")]
    _require(len(paths) == 1 and paths[0] != "/", "worker trial cgroup required")
    return Path("/sys/fs/cgroup") / paths[0].lstrip("/")


def _oom_events(cgroup):
    return int(dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())["oom"])


def run(plan_path, inputs, images, output):
    from .credentials import token_headers
    from .execution_receipt import weakest_provider
    from .api_target import api_origin
    import os

    plan = protocol.read_verified(plan_path, "tagger-worker-runtime-plan/1")
    _require(plan["host"] == socket.gethostname(), "worker trial host mismatch")
    _require(plan["profile"] in ("full", "wd14"), "worker trial profile invalid")
    cgroup = memory_cgroup()
    limit = (cgroup / "memory.max").read_text().strip()
    _require(limit.isdigit() and int(limit) == plan["memory_limit_bytes"],
             "worker trial memory limit mismatch")
    source = Path(__file__).resolve().parent.parent
    subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=source, check=True, timeout=15)
    tree = subprocess.check_output(["git", "ls-tree", "-r", "HEAD"], cwd=source, timeout=15)
    source_sha = hashlib.sha256(tree).hexdigest()
    _require(source_sha == plan["source_sha256"], "worker trial source mismatch")
    policies = plan["policies"]
    _require(protocol.digest(policies) == plan["inventory_sha256"], "worker trial inventory mismatch")
    matching = [p for p in policies if p["tag"] == plan["tag"]]
    _require(len(matching) == 1 and
             all(matching[0][field] == plan[key] for field, key in (
                 ("content_sha256", "artifact_sha256"), ("threshold", "threshold"),
                 ("apply_mode", "apply_mode"))), "worker trial candidate policy mismatch")
    if Path(output).exists():
        raise protocol.ProtocolError("worker trial output already exists")
    start = time.monotonic()
    oom_before = _oom_events(cgroup)
    threads = plan["threads"]
    _require(type(threads) is int and 1 <= threads <= 32, "worker trial thread budget invalid")
    import torch
    from . import nodec_autotag_worker as worker
    torch.set_num_threads(threads)
    _require(torch.get_num_threads() == threads, "worker trial thread budget mismatch")
    headers = token_headers(os.environ["KURA_USER"], os.environ["KURA_TOKEN"])
    api_url = api_origin()
    if plan["profile"] == "full":
        from .infer import primary_provider
        loader = qualification.load_full_trial_inventory
    else:
        loader = qualification.load_wd14_trial_inventory

    models = loader(release_id=plan["release_id"], policies=policies,
                    api_url=api_url, headers=headers)
    if plan["profile"] == "full":
        from .execution_receipt import from_loaded
        chars = models.get("cl2_characters")
        receipt = from_loaded(chars, host=plan["host"], profile="full",
                              release_id=plan["release_id"]) if chars else None
        provider = weakest_provider(primary_provider(models), receipt)
    else:
        provider = weakest_provider(
            worker._provider_label(), worker._execution_receipt(plan["release_id"]),
        )
    _require(provider == plan["provider"], "worker trial provider mismatch")
    candidate = models["content_heads"][plan["tag"]]
    device = next(candidate.parameters()).device.type
    _require(device == plan["device"], "worker trial candidate device mismatch")
    result = qualification.run_frozen_worker_trial(
        plan["profile"], models, inputs, images, inputs_sha256=plan["inputs_sha256"],
        artifact_sha256=plan["artifact_sha256"], threshold=plan["threshold"],
        tag=plan["tag"], apply_mode=plan["apply_mode"],
    )
    _require(result["total"] == plan["total"], "worker trial example count mismatch")
    body = {key: plan[key] for key in (
        "artifact_sha256", "threshold", "host", "profile", "provider", "device",
        "source_sha256", "inventory_sha256", "inputs_sha256", "memory_limit_bytes",
        "threads", "deadline_s", "total",
    )}
    body.update(observed_at=datetime.now(timezone.utc).isoformat(), completed=result["completed"],
                parity_passed=result["parity_passed"], parity=result["parity"], elapsed_s=time.monotonic() - start,
                oom_events=_oom_events(cgroup) - oom_before,
                peak_bytes=int((cgroup / "memory.peak").read_text()),
                model_inventory=sorted({p["content_sha256"] for p in policies}))
    return protocol.write_once(output, {"schema": "tagger-worker-runtime-trial/1",
                                       "plan_sha256": plan["sha256"], "report": body})


FAILURE_SCHEMA = "tagger-worker-runtime-trial-failure/1"


def record_failure(plan_path, output, exc):
    """Leave the reason next to the absent result so the dispatcher can read it
    (otherwise only the unit's exit status and the worker's journal know why).
    Diagnostic only: it never counts as trial evidence. Never raises."""
    try:
        try:
            plan_sha256 = protocol.read_verified(plan_path, "tagger-worker-runtime-plan/1")["sha256"]
        except Exception:  # noqa: BLE001 - an unreadable plan is itself the reason
            plan_sha256 = None
        body = {"schema": FAILURE_SCHEMA, "plan_sha256": plan_sha256,
                "kind": type(exc).__name__,
                "reason": str(exc)[:300],
                "observed_at": datetime.now(timezone.utc).isoformat()}
        path = Path(output).with_name("failure.json")
        temporary = path.with_name(".failure.json.tmp")
        temporary.write_text(json.dumps(dict(body, sha256=protocol.digest(body)), sort_keys=True))
        temporary.replace(path)
    except Exception:  # noqa: BLE001
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "inputs", "images", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    # A failure.json from an earlier run of this plan must not explain this one
    # (a timeout or OOM kill writes none).
    Path(args.output).with_name("failure.json").unlink(missing_ok=True)
    try:
        run(args.plan, args.inputs, args.images, args.output)
    except Exception as exc:
        record_failure(args.plan, args.output, exc)
        raise


if __name__ == "__main__":
    main()
