"""Publication policy for the standalone heads cycle."""
import hashlib
import json
import math

APPLY_THRESHOLDS = {
    "dark_labia_majora": 0.95,
    "dark_labia_minora": 0.995,
    "wrinkled_labia": 0.98,
}
NO_UPDATE = 75


def training_ids_for(head_path, manifest_path):
    """Read provenance only if it is complete and bound to the published bytes."""
    try:
        doc = json.loads(manifest_path.read_text())
        ids = doc["training_post_ids"]
        if (doc["schema"] != "kura-head-training/1" or not isinstance(ids, list)
                or not ids or any(type(pid) is not int or pid <= 0 for pid in ids)
                or doc["head_sha256"] != hashlib.sha256(head_path.read_bytes()).hexdigest()):
            return None
        return frozenset(ids)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def independent_validation(ids, training_ids):
    return [pid for pid in ids if pid not in training_ids]


def wilson_lower(success, total):
    if not total:
        return 0.0
    z = 1.96
    p = success / total
    variance = (p * (1 - p) + z * z / (4 * total)) / total
    return (p + z * z / (2 * total) - z * math.sqrt(variance)) / (1 + z * z / total)


def rejection(candidate, floors, *, existing, baseline):
    """Require absolute floors and compare 95% lower bounds with a 2 pp margin."""
    if candidate["actual_positive"] < floors["min_val_pos_support"]:
        return "insufficient_validation_positive"
    if any(not math.isfinite(candidate[key]) or candidate[key] < floors[f"min_{key}"]
           for key in ("precision", "recall")):
        return "below_quality_floor"
    if existing and baseline is None:
        return "baseline_unavailable"
    if baseline is not None:
        for denominator in ("predicted_positive", "actual_positive"):
            observed = wilson_lower(candidate["tp"], candidate[denominator])
            reference = wilson_lower(baseline["tp"], baseline[denominator])
            one_answer = wilson_lower(max(0, baseline["tp"] - 1), baseline[denominator])
            if observed < min(reference - .02, one_answer):
                return "below_existing_head"
    return None


def measure(model, features, truth, threshold):
    import torch

    weights, bias, mean, scale = (model[key] for key in ("w", "b", "mu", "sd"))
    if (weights.shape != features.shape[1:] or mean.shape != weights.shape
            or scale.shape != weights.shape or bias.shape != (1,)):
        raise ValueError("incompatible head tensors")
    if not all(torch.isfinite(value).all() for value in (weights, bias, mean, scale)) or (scale <= 0).any():
        raise ValueError("invalid head tensors")
    probabilities = torch.sigmoid(((features - mean) / scale) @ weights + bias)
    if not torch.isfinite(probabilities).all():
        raise ValueError("invalid validation predictions")
    predicted = probabilities >= threshold
    positive = truth == 1
    tp = (predicted & positive).sum().item()
    return {
        "tp": tp,
        "predicted_positive": predicted.sum().item(),
        "actual_positive": positive.sum().item(),
        "precision": tp / max(1, predicted.sum().item()),
        "recall": tp / max(1, positive.sum().item()),
        "accuracy": (predicted == positive).float().mean().item(),
    }
