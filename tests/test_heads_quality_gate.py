"""Quality and no-update checks for the standalone heads cycle."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tagger.heads.quality import independent_validation, rejection, training_ids_for

FLOORS = {"min_precision": .85, "min_recall": .7, "min_val_pos_support": 20}


def metrics(tp, predicted, actual):
    return {"tp": tp, "predicted_positive": predicted, "actual_positive": actual,
            "precision": tp / predicted, "recall": tp / actual}


@pytest.mark.parametrize("candidate,baseline,existing,reason", [
    (metrics(83, 100, 100), None, False, "below_quality_floor"),
    (metrics(69, 70, 100), None, False, "below_quality_floor"),
    (metrics(9, 9, 10), None, False, "insufficient_validation_positive"),
    (metrics(90, 100, 100), None, True, "baseline_unavailable"),
    (metrics(90, 100, 100), metrics(98, 100, 100), True, "below_existing_head"),
    (metrics(90, 100, 100), metrics(91, 100, 100), True, None),
    (metrics(90, 100, 100), None, False, None),
])
def test_support_floors_and_existing_head_comparison(candidate, baseline, existing, reason):
    assert rejection(candidate, FLOORS, existing=existing, baseline=baseline) == reason


def test_only_weight_bound_training_provenance_can_filter_validation(tmp_path):
    head = tmp_path / "existing.safetensors"
    head.write_bytes(b"weights")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"chosen": [{"post_id": 2}]}))
    assert training_ids_for(head, manifest) is None
    manifest.write_text(json.dumps({
        "schema": "kura-head-training/1",
        "head_sha256": hashlib.sha256(head.read_bytes()).hexdigest(),
        "training_post_ids": [2, 5],
    }))
    known = training_ids_for(head, manifest)
    assert independent_validation([2, 3, 4, 5], known) == [3, 4]
    head.write_bytes(b"different head")
    assert training_ids_for(head, manifest) is None


@pytest.mark.parametrize("train_rc,source,next_generation", [
    (75, "trainer-head-g12", "12\n"),
    (0, "trainer-head-g13", "13\n"),
])
def test_cycle_refreshes_exclusions_and_keeps_all_published_heads(tmp_path, train_rc, source, next_generation):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / "tagger/heads/update_cycle.sh", scripts)
    (scripts / "fetch_excluded.py").write_text('from pathlib import Path\nPath("refreshed").touch()\n')
    (scripts / "train_heads.py").write_text(
        'from pathlib import Path\nassert Path("refreshed").exists()\n'
        'print(\'HEADS_RESULT {"fixture":{"state":"skipped"}}\')\n'
        f'raise SystemExit({train_rc})\n'
    )
    (scripts / "auto_apply.py").write_text(
        'import os\nfrom pathlib import Path\nassert "HEADS_ACCEPTED_SUMMARY" not in os.environ\n'
        'Path("apply-source").write_text(os.environ["APPLY_SRC"])\n'
    )
    (scripts / "record_generation.py").write_text(
        'from pathlib import Path\nPath("generations.jsonl").touch()\n'
    )
    for name in ("retire_pools.py", "cycle_signal.py"):
        (scripts / name).touch()
    (tmp_path / "generation").write_text("12\n")
    summary = tmp_path / "heads" / "summary.json"
    summary.parent.mkdir()
    summary.write_text('{"previous":{}}')
    result = subprocess.run(["bash", str(scripts / "update_cycle.sh")], check=True,
                            capture_output=True, text=True, timeout=10,
                            env={**os.environ, "KTOK": "fixture", "PY": "python3",
                                 "TAGGER_HEADS_DATA": str(tmp_path)})
    assert ("HEADS_CYCLE_SKIPPED" in result.stdout) == (train_rc == 75)
    assert 'HEADS_RESULT {"fixture":{"state":"skipped"}}' in result.stdout
    assert (tmp_path / "apply-source").read_text() == source
    assert (tmp_path / "generation").read_text() == next_generation
    assert summary.read_text() == '{"previous":{}}'
    assert (tmp_path / "generations.jsonl").exists() == (train_rc == 0)


@pytest.mark.torch
def test_existing_head_is_measured_at_the_same_threshold_as_candidate():
    import torch
    from tagger.heads.quality import measure

    x = torch.tensor([[2.], [1.], [-1.], [-2.]])
    y = torch.tensor([1, 1, 0, 0])
    old = {"w": torch.tensor([3.]), "b": torch.tensor([0.]),
           "mu": torch.tensor([0.]), "sd": torch.tensor([1.])}
    candidate = {**old, "w": torch.tensor([1.])}
    baseline, result = (measure(head, x, y, .85) for head in (old, candidate))
    assert baseline["precision"] == baseline["recall"] == 1.
    assert result["precision"] == 1. and result["recall"] == .5
    assert result["actual_positive"] == baseline["actual_positive"] == 2
