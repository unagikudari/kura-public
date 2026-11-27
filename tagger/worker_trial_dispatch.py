"""Stage and start an isolated candidate trial using declared worker settings."""

import hashlib
import json
from pathlib import Path
import re
import socket
from subprocess import SubprocessError

from . import dedicated_protocol as protocol
from .head_policy import proposed_policy
from .remote_learning import REPO, RSYNC_SSH, command, ssh


def _worker_command(worker, args, *, timeout=60):
    if worker["host"] == socket.gethostname():
        return command(args, timeout=timeout)
    return ssh(worker, args, timeout=timeout)


def _has_result(worker, directory):
    return _worker_command(worker, ["sh", "-c", 'if [ -e "$1" ] || [ -L "$1" ]; then printf present; fi',
                        "sh", str(directory / "result.json")]) == "present"


FAILURE_SCHEMA = "tagger-worker-runtime-trial-failure/1"


def _failure(worker, directory, plan):
    """The worker's own reason for a failed trial, when it could write one."""
    try:
        raw = _worker_command(worker, ["sh", "-c", 'head -c 4097 "$1" 2>/dev/null || true',
                                       "sh", str(directory / "failure.json")])
    except (OSError, SubprocessError):
        return None  # a diagnostic read never changes the failed observation
    if not raw or len(raw.encode("utf-8")) > 4096:
        return None
    try:
        doc = json.loads(raw)
        body = {k: v for k, v in doc.items() if k != "sha256"}
    except (ValueError, AttributeError):
        return None
    if (doc.get("schema") != FAILURE_SCHEMA or doc.get("sha256") != protocol.digest(body)
            or doc.get("plan_sha256") not in (None, plan["sha256"])):
        return None
    return {"kind": str(doc.get("kind"))[:80], "reason": str(doc.get("reason"))[:300],
            "observed_at": str(doc.get("observed_at"))[:40]}


def _restore_images(manifest, directory):
    from . import display_bytes, vnext_arms
    from .dedicated_qualification import stage_worker_trial_images

    conn = vnext_arms.connect_primary()
    try:
        conn.set_session(readonly=True)
        def load(ids):
            return display_bytes.load(lambda sql, args: vnext_arms.q(conn, sql, args), ids)
        stage_worker_trial_images(load, manifest, directory)
    finally:
        conn.close()


def dispatch(trial_dir, plan_path, *, node_inventory=None):
    plan = protocol.read_verified(plan_path, "tagger-worker-runtime-plan/1")
    inventory_path = node_inventory or REPO / "tagger/deploy/nodes.json"
    inventory = json.loads(Path(inventory_path).read_text())["nodes"]
    host = plan["host"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise protocol.ProtocolError("worker trial host invalid")
    node = inventory.get(host)
    profile = plan["profile"]
    if node is None or profile not in node["profiles"]:
        raise protocol.ProtocolError("worker trial node/profile undeclared")

    service_checkout = Path(node["checkout"])
    worker = {"host": host}
    if node.get("trial_checkout") == "auto":
        checkout = _auto_trial_checkout(worker, node, service_checkout, Path(trial_dir), plan)
    else:
        checkout = Path(node.get("trial_checkout", service_checkout))
    if not checkout.is_absolute() or ".." in checkout.parts:
        raise protocol.ProtocolError("worker trial checkout path invalid")
    remote = Path(node["home"]) / ".local/state/kura-tagger/worker-trials" / plan["sha256"]
    unit = "kura-worker-trial-" + plan["sha256"][:24] + ".service"
    # A surviving unit is authoritative even if the launch SSH reply was lost.
    state = _worker_command(worker, ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"])
    if state != "not-found" or _has_result(worker, remote):
        return {"host": host, "unit": unit, "directory": str(remote), "state": "already_submitted"}
    active = _worker_command(worker, ["systemctl", "--user", "list-units", "--plain", "--no-legend",
                          "--state=activating,running", "kura-worker-trial-*.service"])
    if active:
        return {"host": host, "unit": unit, "directory": str(remote), "state": "busy"}
    min_free = node.get("trial_min_free_vram_mib")
    if min_free is not None:
        if type(min_free) is not int or min_free < 1:
            raise protocol.ProtocolError("worker trial VRAM floor invalid")
        free = _worker_command(worker, [
            "nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits",
        ], timeout=10).strip()
        if not free.isascii() or not free.isdecimal():
            raise protocol.ProtocolError("worker trial requires one observable NVIDIA GPU")
        if int(free) < min_free:
            return {"host": host, "unit": unit, "directory": str(remote),
                    "state": "waiting_capacity", "free_vram_mib": int(free),
                    "required_vram_mib": min_free}
    if node.get("trial_requires_yield", False):
        worker_state = _worker_command(worker, [
            "systemctl", "--user", "show", f"kura-tagger@{profile}.service",
            "--property=LoadState,ActiveState", "--value",
        ], timeout=10).splitlines()
        if set(worker_state) != {"loaded", "inactive"}:
            return {"host": host, "unit": unit, "directory": str(remote),
                    "state": "waiting_yield"}
    memory = plan["memory_limit_bytes"]
    deadline = plan["deadline_s"]
    quota = plan["threads"]
    if (type(memory) is not int or not 1024**3 <= memory <= 16 * 1024**3
            or type(deadline) is not int or not 1 <= deadline <= 3600
            or type(quota) is not int or not 1 <= quota <= 8):
        raise protocol.ProtocolError("worker trial resource budget invalid")
    root = Path(trial_dir)
    manifest_path = root / "worker-trial-inputs.json"
    manifest = protocol.read_verified(manifest_path, "tagger-worker-trial-inputs/1")
    if manifest["sha256"] != plan["inputs_sha256"]:
        raise protocol.ProtocolError("worker trial transfer inputs mismatch")
    records = manifest["examples"]
    ids = [row["post_id"] for row in records]
    image_dir = root / "worker-trial-images"
    if (not 2 <= len(ids) <= 64 or any(type(i) is not int or i < 1 for i in ids)
            or len(ids) != len(set(ids)) or image_dir.is_symlink()):
        raise protocol.ProtocolError("worker trial transfer population invalid")
    images = [image_dir / f"{i}.bin" for i in ids]
    if any(path.is_symlink() for path in images):
        raise protocol.ProtocolError("worker trial image symlink")
    if any(not path.is_file() for path in images):
        _restore_images(manifest, image_dir)
    _worker_command(worker, ["git", "-C", str(checkout), "diff", "--quiet", "HEAD"])
    tree = _worker_command(worker, ["git", "-C", str(checkout), "ls-tree", "-r", "HEAD"])
    if hashlib.sha256((tree + "\n").encode()).hexdigest() != plan["source_sha256"]:
        raise protocol.ProtocolError("worker trial source not installed on target")
    _worker_command(worker, ["mkdir", "-p", "-m", "700", str(remote / "images")])
    rsync = ["rsync", "-a", "--ignore-existing", "--chmod=D700,F600"]
    prefix = ""
    if host != socket.gethostname():
        rsync.extend(("-e", RSYNC_SSH))
        prefix = host + ":"
    for source, destination in ((plan_path, remote / "plan.json"),
                                (manifest_path, remote / "inputs.json")):
        command([*rsync, str(source), prefix + str(destination)], timeout=120)
    command([*rsync, *map(str, images), prefix + str(remote / "images") + "/"], timeout=120)
    args = [
        "systemd-run", "--user", "--quiet", "--remain-after-exit", "--unit", unit,
        "--working-directory", str(checkout),
        "--property", "EnvironmentFile=" + str(service_checkout / "tagger/env"),
        "--property", f"MemoryMax={memory}", "--property", f"RuntimeMaxSec={deadline}s",
        "--property", f"CPUQuota={quota * 100}%", "--property", "Nice=15",
        "--property", "IOSchedulingClass=idle",
        "--setenv", "KURA_MODELS_DIR=" + node["models_dir"],
        "--setenv", "TAGGER_PROFILE=" + profile,
        "--setenv", "HF_HUB_OFFLINE=1",
    ]
    for name, value in sorted(node.get("env", {}).items()):
        args.extend(("--setenv", f"{name}={value}"))
    args.extend([
        "/usr/bin/flock", "--nonblock", str(remote.parent / "host-trial.lock"),
        str(Path(node["venv"]) / "bin/python"), "-m", "tagger.worker_trial",
        "--plan", str(remote / "plan.json"), "--inputs", str(remote / "inputs.json"),
        "--images", str(remote / "images"), "--output", str(remote / "result.json"),
    ])
    _worker_command(worker, args, timeout=30)
    return {"host": host, "unit": unit, "directory": str(remote), "state": "submitted"}


def collect(submission, plan_path):
    """Read a probe result once its actual unit has completed successfully."""
    plan = protocol.read_verified(plan_path, "tagger-worker-runtime-plan/1")
    host = submission["host"]
    unit = submission["unit"]
    directory = Path(submission["directory"])
    if (host != plan["host"] or unit != "kura-worker-trial-" + plan["sha256"][:24] + ".service"
            or directory.name != plan["sha256"]):
        raise protocol.ProtocolError("worker trial submission binding mismatch")
    worker = {"host": host}
    raw = _worker_command(worker, ["systemctl", "--user", "show", unit,
                       "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus"])
    status = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if status.get("LoadState") == "not-found":
        # The worker atomically writes the receipt as its final action.
        # Transient units can disappear on restart while the receipt survives.
        if not _has_result(worker, directory):
            return {"state": "missing"}
    else:
        if (status.get("ActiveState") == "failed"
                or status.get("Result") not in (None, "", "success")):
            failed = {"state": "failed", "result": status.get("Result")}
            reason = _failure(worker, directory, plan)
            return dict(failed, failure=reason) if reason else failed
        if status.get("SubState") != "exited" or status.get("ExecMainStatus") != "0":
            return {"state": "running"}
    raw = _worker_command(worker, ["head", "-c", "65537", str(directory / "result.json")])
    if len(raw.encode("utf-8")) > 65536:
        raise protocol.ProtocolError("worker trial result byte budget exceeded")
    result = json.loads(raw)
    body = {k: v for k, v in result.items() if k != "sha256"}
    if (result.get("schema") != "tagger-worker-runtime-trial/1"
            or result.get("sha256") != protocol.digest(body)
            or result.get("plan_sha256") != plan["sha256"]):
        raise protocol.ProtocolError("worker trial result binding mismatch")
    report = result["report"]
    fields = ("host", "profile", "artifact_sha256", "threshold", "device", "provider",
              "source_sha256", "inventory_sha256", "inputs_sha256", "memory_limit_bytes",
              "threads", "deadline_s", "total")
    if any(report.get(key) != plan[key] for key in fields):
        raise protocol.ProtocolError("worker trial report configuration mismatch")
    return {"state": "collected", "report": report, "receipt_sha256": result["sha256"]}


async def register_result(db, registration_id, result, *, user_id, expected_context_sha256):
    """Persist an assertion through the existing qualification evidence contract."""
    from api.services.qualification_evidence import attach

    if result.get("state") != "collected":
        raise protocol.ProtocolError("worker trial is not complete")
    return await attach(db, registration_id, expected_context_sha256=expected_context_sha256,
                        kind="runtime", report=result["report"], user_id=user_id)


async def run_trial_plan(sessions, declaration, *, state_root, node_inventory=None):
    """Dispatch or collect declared trials without waiting inside the learning tick."""
    import asyncio
    from sqlalchemy import text
    from api.services.qualification_evidence import registration_context
    from api.tagger_core import qualification_evidence as evidence

    user_id = declaration.get("submitter_id")
    entries = declaration.get("trials")
    if (declaration.get("schema") != "tagger-worker-trial-declaration/1"
            or type(user_id) is not int or user_id < 1
            or not isinstance(entries, list) or len(entries) > 16):
        raise protocol.ProtocolError("worker trial declaration invalid")
    results = []
    for entry in entries:
        registration = entry["registration_id"]
        plan_path = Path(entry["plan_path"])
        plan = protocol.read_verified(plan_path, "tagger-worker-runtime-plan/1")
        async with sessions() as db:
            ctx = await registration_context(db, registration)
            metrics = ctx["metrics"]
            expected = (await db.execute(text("""
                SELECT EXISTS (SELECT FROM tagger_expected_worker
                   WHERE host=:host AND profile=:profile AND NOT opportunistic)
            """), {"host": plan["host"], "profile": plan["profile"]})).scalar_one()
            if (not expected or not ctx["lifecycle_event_id"]
                    or plan["tag"] != ctx["tag"]
                    or plan["artifact_sha256"] != ctx["artifact_sha256"]
                    or plan["threshold"] != metrics.get("threshold")
                    or plan["inputs_sha256"] != (metrics.get("production_review") or {}).get(
                        "worker_trial_inputs_sha256")):
                raise protocol.ProtocolError("worker trial registration binding mismatch")
        import hashlib
        key = hashlib.sha256(f'{ctx["tag"]}@{ctx["lifecycle_event_id"]}'.encode()).hexdigest()[:24]
        directory = Path(state_root) / key / "received"
        marker = directory / "trial-submissions" / (plan["sha256"] + ".json")
        try:
            if marker.exists():
                submitted = protocol.read_verified(marker, "tagger-worker-trial-submission/1")["submission"]
            else:
                submitted = await asyncio.to_thread(dispatch, directory, plan_path, node_inventory=node_inventory)
                if submitted["state"] in {"busy", "waiting_capacity", "waiting_yield"}:
                    results.append({"registration_id": registration, **submitted})
                    continue
                protocol.write_once(marker, {"schema": "tagger-worker-trial-submission/1",
                                             "submission": submitted})
            observation = await asyncio.to_thread(collect, submitted, plan_path)
        except (OSError, ValueError, SubprocessError) as exc:
            results.append({"registration_id": registration, "host": plan["host"],
                            "state": "unavailable", "error": type(exc).__name__})
            continue
        summary = {"registration_id": registration, "host": plan["host"],
                   "state": observation["state"]}
        if observation["state"] == "collected":
            async with sessions() as db:
                registered = await register_result(
                    db, registration, observation, user_id=user_id,
                    expected_context_sha256=evidence.digest(ctx),
                )
                await db.commit()
            summary["receipt_sha256"] = registered["receipt_sha256"]
            if "parity" in observation["report"]:
                summary["parity"] = observation["report"]["parity"]
        elif observation["state"] == "missing":
            # Forget admissions lost with the worker's systemd manager.
            # The next tick rechecks the deterministic unit before dispatch.
            marker.unlink(missing_ok=True)
        elif observation["state"] == "failed":
            summary["result"] = observation["result"]
            if observation.get("failure"):
                summary["failure"] = observation["failure"]
        results.append(summary)
    return results


def build_worker_plans(context, inputs, *, release_id, current_policies,
                       candidate_policy, worker_settings):
    """Bind each configured worker trial to one candidate and serving inventory."""
    tag = context["tag"]
    metrics = context["metrics"]
    review = metrics["production_review"]
    if (inputs["sha256"] != review["worker_trial_inputs_sha256"]
            or inputs["artifact_sha256"] != context["artifact_sha256"]
            or inputs["snapshot_sha256"] != metrics["snapshot_sha256"]
            or inputs["threshold"] != metrics["threshold"]
            or candidate_policy["tag"] != tag
            or candidate_policy["content_sha256"] != context["artifact_sha256"]
            or candidate_policy["threshold"] != metrics["threshold"]):
        raise protocol.ProtocolError("worker plan candidate binding mismatch")
    by_tag = {row["tag"]: row for row in current_policies}
    if len(by_tag) != len(current_policies):
        raise protocol.ProtocolError("worker plan duplicate current policy")
    previous = by_tag.get(tag)
    predecessor = ({key: previous.get(key) for key in (
        "content_sha256", "patch_version", "threshold", "apply_mode", "gate_tags",
    )} if previous else None)
    by_tag[tag] = candidate_policy
    policies = [by_tag[name] for name in sorted(by_tag)]
    inventory_sha = protocol.digest(policies)
    keys = {"host", "profile", "source_sha256", "device", "provider",
            "memory_limit_bytes", "threads", "deadline_s"}
    pairs = set()
    results = []
    for settings in worker_settings:
        if set(settings) != keys:
            raise protocol.ProtocolError("worker plan settings invalid")
        pair = (settings["host"], settings["profile"])
        if pair in pairs:
            raise protocol.ProtocolError("worker plan duplicate worker")
        pairs.add(pair)
        results.append({
            "schema": "tagger-worker-runtime-plan/1", **settings, "tag": tag,
            "release_id": release_id, "policies": policies, "inventory_sha256": inventory_sha,
            "expected_predecessor": predecessor,
            "inputs_sha256": inputs["sha256"], "artifact_sha256": context["artifact_sha256"],
            "threshold": metrics["threshold"], "apply_mode": candidate_policy["apply_mode"],
            "total": len(inputs["examples"]),
        })
    if not results:
        raise protocol.ProtocolError("worker plan inventory empty")
    return results


async def generate_trial_declaration(sessions, settings, *, state_root):
    """Find eligible pending candidates and write their immutable worker plans."""
    import asyncio
    from sqlalchemy import text
    from api.routers.tagger import list_head_policies
    from api.services.qualification_evidence import registration_context
    from api.tagger_core.quality_hold import held_names

    policies = settings["candidate_policies"]
    workers = settings["workers"]
    if not isinstance(policies, dict) or len(policies) > 16 or not isinstance(workers, list):
        raise protocol.ProtocolError("automatic worker trial settings invalid")
    pairs = {(row["host"], row["profile"]) for row in workers}
    async with sessions() as db:
        expected = (await db.execute(text("""
            SELECT host, profile FROM tagger_expected_worker WHERE NOT opportunistic
        """))).all()
        if not pairs or pairs != {tuple(row) for row in expected} or len(pairs) != len(workers):
            raise protocol.ProtocolError("automatic worker trial inventory mismatch")
        release = (await db.execute(text("""
            SELECT release_id FROM tagger_release_active WHERE id=1
        """))).scalar_one_or_none()
        if release is None:
            raise protocol.ProtocolError("automatic worker trial active release missing")
        held = await held_names(db)
        explicit = {tag: policy for tag, policy in policies.items() if tag not in held}
        derived, undeclared = {}, []
        if settings.get("derive_policies") is True:
            derived, undeclared = await derive_candidate_policies(db, exclude=set(policies) | held)
        policies = {**derived, **explicit}
        found = (await db.execute(text("""
            SELECT r.id, r.tag, c.name AS category,
                   (SELECT coalesce(max(version),0) + 1 FROM tagger_patch WHERE tag_id=r.tag_id) AS version
            FROM tagger_registration_request r
            JOIN tag t ON t.id=r.tag_id
            JOIN tag_category c ON c.id=t.category_id
            WHERE r.tag=ANY(:tags) AND r.status='pending'
              AND r.metrics->'production_review'->>'worker_trial_inputs_sha256' IS NOT NULL
              AND r.id = (SELECT newer.id FROM tagger_registration_request newer
                          WHERE newer.tag_id=r.tag_id ORDER BY requested_at DESC,id DESC LIMIT 1)
            ORDER BY r.id
        """), {"tags": sorted(policies)})).mappings().all()
        candidates, deferred = select_within_trial_cap(found, explicit=explicit,
                                                       worker_count=len(workers))
        declaration = {"schema": "tagger-worker-trial-declaration/1",
                       "submitter_id": settings["submitter_id"], "trials": [], "unavailable": [],
                       "candidate_policies": policies, "undeclared_domain": undeclared,
                       "deferred_by_cap": deferred}
        if not candidates:
            return declaration
        inventory = await list_head_policies(db, require_complete=True)
        current = json.loads(inventory.body)
        for row in candidates:
            context = await registration_context(db, row["id"])
            key = hashlib.sha256(
                f'{context["tag"]}@{context["lifecycle_event_id"]}'.encode()
            ).hexdigest()[:24]
            directory = Path(state_root) / key / "received"
            manifest_path = directory / "worker-trial-inputs.json"
            review = context["metrics"]["production_review"]
            try:
                if "worker_trial_inputs" in review:
                    manifest = review["worker_trial_inputs"]
                    if (manifest.get("schema") != "tagger-worker-trial-inputs/1"
                            or protocol.digest(manifest) != review["worker_trial_inputs_sha256"]):
                        raise protocol.ProtocolError("registered worker trial inputs mismatch")
                    inputs = protocol.write_once(manifest_path, manifest)
                else:
                    inputs = protocol.read_verified(manifest_path, "tagger-worker-trial-inputs/1")
                row_workers = workers
                if settings.get("source") == "deployed":
                    source = await asyncio.to_thread(pinned_trial_source, directory)
                    row_workers = [dict(w, source_sha256=source["source_sha256"]) for w in workers]
            except (OSError, ValueError, SubprocessError) as exc:
                declaration["unavailable"].append({"registration_id": row["id"],
                                                   "error": type(exc).__name__})
                continue
            candidate = proposed_policy(
                policies[row["tag"]], tag=row["tag"], category=row["category"],
                version=row["version"], artifact_sha256=context["artifact_sha256"],
                threshold=context["metrics"]["threshold"],
            )
            for plan in build_worker_plans(context, inputs, release_id=release,
                                          current_policies=current, candidate_policy=candidate,
                                          worker_settings=row_workers):
                path = directory / "trial-plans" / (protocol.digest(plan) + ".json")
                protocol.write_once(path, plan)
                declaration["trials"].append({"registration_id": row["id"], "plan_path": str(path)})
                if len(declaration["trials"]) > 16:
                    raise protocol.ProtocolError("automatic worker trial batch too large")
    return declaration


def select_within_trial_cap(found, *, explicit, worker_count, max_trials=16):
    """One trial per expected worker per candidate and at most ``max_trials`` per
    tick: operator-listed tags first, then derived ones by registration id; the
    rest are returned by tag to wait for a later tick."""
    cap = max(1, max_trials // max(1, worker_count))
    ordered = sorted(found, key=lambda row: (row["tag"] not in explicit, row["id"]))
    return ordered[:cap], [row["tag"] for row in ordered[cap:]]


PENDING_TRIAL_CANDIDATES_SQL = """
SELECT r.tag, r.metrics->>'threshold' AS threshold
FROM tagger_registration_request r
WHERE r.status='pending'
  AND r.metrics->'production_review'->>'worker_trial_inputs_sha256' IS NOT NULL
  AND r.metrics->'production_review'->>'scope' = 'catalog_sample_outside_frozen_gt_groups'
  AND NOT EXISTS (SELECT FROM tagger_candidate_disposition_current d
                  WHERE d.tag = r.tag AND d.disposition IN ('defer', 'dismiss', 'block'))
  AND r.id = (SELECT newer.id FROM tagger_registration_request newer
              WHERE newer.tag_id=r.tag_id ORDER BY requested_at DESC,id DESC LIMIT 1)
ORDER BY r.id
"""


async def derive_candidate_policies(db, *, exclude=frozenset()):
    """Serving policy for each publishable pending candidate, from curated declarations only.

    The visual domain comes from tagger_tag_rule (head_policy.domain_gate_tags);
    a tag without a representable declaration is reported, never given gate_tags=[].
    """
    from sqlalchemy import text
    from .head_policy import apply_mode_for, domain_gate_tags

    derived, undeclared = {}, []
    for row in (await db.execute(text(PENDING_TRIAL_CANDIDATES_SQL))).mappings():
        tag = row["tag"]
        if tag in exclude or tag in derived:
            continue
        try:
            threshold = float(row["threshold"])
        except (TypeError, ValueError):
            continue
        if not 0 < threshold < 1:  # also rejects nan and inf
            continue
        domain = await domain_gate_tags(db, tag)
        if domain is None:
            undeclared.append(tag)
            continue
        derived[tag] = {"apply_mode": apply_mode_for(measured=True),
                        "operating_point": f"dedicated@{threshold!r}",
                        "gate_tags": list(domain)}
    return derived, undeclared



TRIAL_SOURCE_SCHEMA = "tagger-worker-trial-source/1"
_ENSURE_CHECKOUT = r"""
set -e
service="$1"; checkout="$2"; commit="$3"
ok() {
  [ "$(git -C "$checkout" rev-parse HEAD 2>/dev/null)" = "$commit" ] &&
    git -C "$checkout" diff --quiet HEAD 2>/dev/null
}
if ! ok; then
  # Missing, partial or modified: rebuild from scratch.
  git -C "$service" worktree remove --force "$checkout" 2>/dev/null || rm -rf "$checkout"
  git -C "$service" worktree prune
  git -C "$service" cat-file -e "$commit^{commit}" 2>/dev/null || git -C "$service" fetch -q origin
  mkdir -p "$(dirname "$checkout")"
  git -C "$service" worktree add -q --detach "$checkout" "$commit"
fi
touch "$checkout"
# Drop pinned checkouts nobody has dispatched to for two weeks; recreated on demand.
for old in "$(dirname "$checkout")"/*; do
  [ "$old" = "$checkout" ] && continue
  [ -n "$(find "$old" -maxdepth 0 -mtime +14 2>/dev/null)" ] || continue
  git -C "$service" worktree remove --force "$old" 2>/dev/null || rm -rf "$old"
done
git -C "$service" worktree prune
"""

def deployed_source(repo=REPO, mainline="origin/main"):
    """The commit this canonical checkout runs, and the tree digest a trial must reproduce."""
    import subprocess
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                     text=True, timeout=15).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise protocol.ProtocolError("deployed source commit invalid")
    # Only code every node can fetch: the shared checkout may sit on another
    # lane's unpushed branch, and a pin is permanent for its candidate.
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, mainline],
                      timeout=15).returncode != 0:
        raise protocol.ProtocolError("deployed source is not on the published mainline")
    tree = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", commit], timeout=60)
    return commit, hashlib.sha256(tree).hexdigest()


def pinned_trial_source(directory, *, source=None):
    """Pin a candidate's trial source once: the deployed commit when it was first planned.

    Later deploys must not restart trials already in flight, so the pin is
    immutable; a new candidate (new lifecycle event) gets the then-deployed code.
    To re-pin one candidate deliberately (e.g. a parity fix landed after it was
    pinned), move its trial-source.json aside; its old receipts stay as evidence
    for the old plan identity and new trials run on the then-deployed code.
    """
    path = Path(directory) / "trial-source.json"
    if path.exists():
        return protocol.read_verified(path, TRIAL_SOURCE_SCHEMA)
    commit, digest = (source or deployed_source)()
    return protocol.write_once(path, {"schema": TRIAL_SOURCE_SCHEMA, "commit": commit,
                                      "source_sha256": digest})


def _auto_trial_checkout(worker, node, service_checkout, trial_dir, plan):
    """A clean detached worktree of the pinned commit, created on the node on demand.

    The caller still verifies it is clean and reproduces plan["source_sha256"].
    """
    path = trial_dir / "trial-source.json"
    if not path.exists():
        raise protocol.ProtocolError('trial_checkout "auto" needs trial settings with source "deployed"')
    source = protocol.read_verified(path, TRIAL_SOURCE_SCHEMA)
    if source["source_sha256"] != plan["source_sha256"]:
        raise protocol.ProtocolError("worker trial source pin mismatch")
    commit = source["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise protocol.ProtocolError("worker trial source commit invalid")
    checkout = Path(node["home"]) / ".local/state/kura-tagger/trial-checkouts" / commit
    _worker_command(worker, ["sh", "-c", _ENSURE_CHECKOUT, "sh", str(service_checkout),
                             str(checkout), commit], timeout=300)
    return checkout
