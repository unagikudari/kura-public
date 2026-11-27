"""The loaded policy set is one correction contract, regardless of tag category."""
import hashlib
import json
import math
import re

from .pixai_chars import RequiredCharacterComponentError


class RequiredContentHeadError(RequiredCharacterComponentError):
    pass


def require_loaded(heads, policies=None):
    if policies is None:
        policies = heads.get('__report_policies__')
    if policies is not None:
        tags = [policy.get('tag') for policy in policies]
        if (any(not isinstance(tag, str) or not tag or tag.startswith('__') for tag in tags)
                or len(set(tags)) != len(tags)):
            raise RequiredContentHeadError('content_head_inventory_invalid')
        loaded_tags = {tag for tag in heads if not tag.startswith('__')}
        if set(tags) != loaded_tags:
            raise RequiredContentHeadError('content_head_inventory_mismatch')
    for tag, model in heads.items():
        if tag.startswith('__'):
            continue
        policy = heads.get(f'__policy__{tag}') or {}
        # Category is vocabulary taxonomy, not the model's two output lanes.
        # Existing general heads use e.g. male_genitals, female_genitals, anus.
        category = policy.get('category')
        if not isinstance(category, str) or not category.strip():
            raise RequiredContentHeadError(f'content_head_category_invalid:{tag}')
        threshold = policy.get('threshold')
        if model is None:
            raise RequiredContentHeadError(f'content_head_not_loaded:{tag}')
        if (type(threshold) not in (int, float) or not math.isfinite(threshold)
                or not 0 <= threshold <= 1):
            raise RequiredContentHeadError(f'content_head_threshold_invalid:{tag}')
        if policy.get('apply_mode') not in ('replace', 'add_only'):
            raise RequiredContentHeadError(f'content_head_apply_mode_invalid:{tag}')


def source(heads):
    policies = {tag: {'sha': heads.get(f'__sha__{tag}'),
                      'loaded': model is not None,
                      'policy': heads.get(f'__policy__{tag}')}
                for tag, model in (heads or {}).items() if not tag.startswith('__')}
    if not policies:
        return ''
    digest = hashlib.sha256(json.dumps(policies, sort_keys=True, allow_nan=False).encode()).hexdigest()[:12]
    return f'content-head-policy={digest}'


def executed_heads(heads, evaluated_tags):
    """Return identities only for heads actually evaluated on this image."""
    result = []
    for tag in sorted(evaluated_tags):
        sha = heads.get(f"__artifact_sha256__{tag}")
        policy = heads.get(f"__policy__{tag}")
        if heads.get(tag) is None or not isinstance(policy, dict):
            raise RequiredContentHeadError(f"content_head_execution_without_policy:{tag}")
        # Legacy in-process test/dev heads may have no artifact identity and
        # therefore cannot supply evidence for an artifact deployment.
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        result.append({"tag": tag, "artifact_sha256": sha,
                       "threshold": policy["threshold"], "apply_mode": policy["apply_mode"]})
    return result
