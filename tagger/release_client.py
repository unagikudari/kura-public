# Description: fail-closed safe pull of the active tagger release (plan:kura-tagger-registry rev2 Step 5).
# Kind: lib
# Effects: mutating (writes to a local cache dir; reads from kura-api over HTTP)
# Requires: none in the core (pull()); requests + torch only in the default production callbacks
"""release_client.py — every node's half of the release registry.

rev 1 of this plan had the tagger keep running on a stale/corrupt model if a
pull failed ("don't stop the tagger"). Both adversarial reviewers (codex#2,
gemini#2) called that out: it produces a zombie tagger that mistags forever on
a model nobody can vouch for, which is worse than not running at all. rev 2
inverts the priority: never run on an unverified model.

The core `pull()` function takes its I/O as injected callbacks
(fetch_meta_fn / fetch_data_fn / loader_fn) so the state machine below --
write-to-.partial, fsync, sha256-verify, load+selftest, atomic rename, content-
addressed skip, fallback-to-last-verified, refuse-if-none -- is fully testable
without a network or torch. `default_loader` at the bottom is the real,
network/torch-using production callback.

    write .partial -> fsync -> sha256 check -> load + selftest on a known input
        -> atomic rename to active/
    any step fails  -> fall back to the last VERIFIED release already in active/
    nothing verified ever cached -> raise ReleaseClientError (refuse to start)
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ReleaseClientError(Exception):
    """No usable (verified) release is available; caller must refuse to start."""


class PullVerifyError(ReleaseClientError):
    """A specific pull's bytes failed sha256 or load-and-selftest verification."""


@dataclass
class Verified:
    release_id: int
    model_sha256: str
    model_path: Path
    csv_path: Path
    fallback: bool = False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_meta(cache_dir: Path) -> dict | None:
    p = cache_dir / "active" / "meta.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _sleep_backoff(attempt: int, base: float, sleep_fn: Callable[[float], None],
                    rand_fn: Callable[[float, float], float]) -> None:
    """Full jitter exponential backoff (AWS-style): sleep a random amount in
    [0, min(cap, base*2^attempt)]. Jittered so N nodes retrying together don't
    re-converge on the same retry instant (plan Step 9's congestion concern)."""
    cap = min(60.0, base * (2 ** attempt))
    sleep_fn(rand_fn(0, cap))


def _atomic_write_verified(cache_dir: Path, release_id: int, model_sha256: str,
                            model_bytes: bytes, csv_bytes: bytes,
                            loader_fn: Callable[[Path, Path], tuple[bool, str]]) -> Verified:
    """Write-verify-activate. Raises PullVerifyError WITHOUT touching the
    existing active/ dir if anything fails -- a failed pull can never
    clobber a previously-good, still-serving release."""
    partial_dir = cache_dir / ".partial"
    partial_dir.mkdir(parents=True, exist_ok=True)
    model_partial = partial_dir / "wd14-fixed.pt"
    csv_partial = partial_dir / "selected_tags.csv"

    for path, data in ((model_partial, model_bytes), (csv_partial, csv_bytes)):
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    actual_sha256 = _sha256_file(model_partial)
    if actual_sha256 != model_sha256:
        raise PullVerifyError(f"sha256 mismatch: server said {model_sha256}, got {actual_sha256}")

    ok, detail = loader_fn(model_partial, csv_partial)
    if not ok:
        raise PullVerifyError(f"load+selftest failed: {detail}")

    active_dir = cache_dir / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    os.replace(model_partial, active_dir / "wd14-fixed.pt")
    os.replace(csv_partial, active_dir / "selected_tags.csv")
    (active_dir / "meta.json").write_text(json.dumps({
        "release_id": release_id, "model_sha256": model_sha256, "verified_detail": detail,
    }))
    return Verified(release_id, model_sha256, active_dir / "wd14-fixed.pt", active_dir / "selected_tags.csv")


def pull(
    fetch_meta_fn: Callable[[], dict],
    fetch_data_fn: Callable[[int, str], bytes],
    cache_dir: Path,
    loader_fn: Callable[[Path, Path], tuple[bool, str]],
    max_retries: int = 5,
    base_backoff: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    rand_fn: Callable[[float, float], float] = random.uniform,
) -> Verified:
    """fetch_meta_fn() -> {"id": int, "model_sha256": str, ...} (GET .../release/active).
    fetch_data_fn(release_id, "model"|"tags") -> bytes (GET .../release/{id}/data).
    loader_fn(model_path, csv_path) -> (ok, detail): actually load the model and
    run one inference on a known input; this is what makes verification more
    than a checksum -- a bit-perfect but architecturally-incompatible file must
    still fail here.
    """
    cached_meta = _read_meta(cache_dir)
    last_error: Exception | None = None

    for attempt in range(max_retries):
        try:
            meta = fetch_meta_fn()
        except Exception as e:  # noqa: BLE001 -- any transport failure is retryable
            last_error = e
            _sleep_backoff(attempt, base_backoff, sleep_fn, rand_fn)
            continue

        if cached_meta and cached_meta.get("model_sha256") == meta["model_sha256"]:
            # Content-addressed: already have and already verified these exact
            # bytes -- skip the download entirely (plan Step 9 congestion
            # mitigation, and the common case for N nodes polling the same
            # unchanged release).
            active_dir = cache_dir / "active"
            return Verified(meta["id"], meta["model_sha256"],
                             active_dir / "wd14-fixed.pt", active_dir / "selected_tags.csv")

        try:
            model_bytes = fetch_data_fn(meta["id"], "model")
            csv_bytes = fetch_data_fn(meta["id"], "tags")
            return _atomic_write_verified(cache_dir, meta["id"], meta["model_sha256"],
                                           model_bytes, csv_bytes, loader_fn)
        except PullVerifyError as e:
            # Bad bytes/model, not a transient transport error -- retrying the
            # same server response won't help; stop and fall back below.
            last_error = e
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            _sleep_backoff(attempt, base_backoff, sleep_fn, rand_fn)
            continue

    # fail-closed: every attempt failed, or verification rejected the bytes.
    # Fall back to a release we already proved loads+infers correctly; if none
    # was ever verified on this node, refuse to start rather than run on
    # nothing / on unverified bytes.
    if cached_meta is not None:
        active_dir = cache_dir / "active"
        if (active_dir / "wd14-fixed.pt").is_file():
            return Verified(cached_meta["release_id"], cached_meta["model_sha256"],
                             active_dir / "wd14-fixed.pt", active_dir / "selected_tags.csv",
                             fallback=True)
    raise ReleaseClientError(
        f"no usable tagger release available (last error: {last_error!r}) and no "
        "previously verified release cached on this node -- refusing to start")


# ── production callbacks (network/torch; not exercised by unit tests) ──────

def make_http_callbacks(kura_url: str, headers: dict[str, str]):
    import requests  # noqa: PLC0415

    # Accept a complete credential header, never an ambiguously raw/base64 token.
    headers = dict(headers)

    def fetch_meta_fn() -> dict:
        r = requests.get(f"{kura_url}/api/tagger/release/active", headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def fetch_data_fn(release_id: int, part: str) -> bytes:
        r = requests.get(f"{kura_url}/api/tagger/release/{release_id}/data",
                          params={"part": part}, headers=headers, timeout=120)
        r.raise_for_status()
        return r.content

    return fetch_meta_fn, fetch_data_fn


def default_loader(model_path: Path, csv_path: Path) -> tuple[bool, str]:
    """Actually load the merged SwinV2 checkpoint and run one forward pass on a
    known (zero) input, checking the output is finite and the right shape.
    A bit-perfect but architecturally-incompatible or truncated file fails here
    even though it might pass a checksum against a DIFFERENT correct copy of
    itself -- this is the check codex#2/gemini#2 required beyond sha256."""
    try:
        import torch  # noqa: PLC0415
        import timm  # noqa: PLC0415

        ckpt = torch.load(str(model_path), map_location="cpu", weights_only=False)
        model = timm.create_model(f"hf-hub:{ckpt['repo']}", pretrained=False,
                                   num_classes=ckpt["n_out"])
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        img_size = ckpt.get("img_size", 448)
        with torch.no_grad():
            out = model(torch.zeros(1, 3, img_size, img_size))
        if not torch.isfinite(out).all():
            return False, "forward pass produced non-finite values"
        if out.shape[-1] != ckpt["n_out"]:
            return False, f"output width {out.shape[-1]} != n_out {ckpt['n_out']}"
        return True, f"loaded, forward pass ok, n_out={ckpt['n_out']}"
    except Exception as e:  # noqa: BLE001 -- any load/inference failure is a verify failure
        return False, f"{type(e).__name__}: {e}"
