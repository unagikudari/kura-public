"""Read-only, offline character evaluation against explicitly reviewed pool titles.

Title agreement is weak supervision, not per-image accuracy. Missing predictions
are not empty predictions. This module never writes tags, claims, pins or GT.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path


class InvalidEvaluation(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise InvalidEvaluation(reason)


def names(value):
    require(isinstance(value, list) and all(isinstance(x, str) and x.strip() == x and x for x in value), 'invalid_names')
    require(len(value) == len(set(value)), 'duplicate_names')
    return set(value)


def reviewed_equivalences(dataset):
    """Dataset-scoped identities, never production aliases or human GT.

    Explicit non-overlapping groups avoid chained aliases, cycles and heuristic
    spelling matches. Every group carries its reviewer and evidence sources.
    """
    groups = dataset.get('name_equivalences', [])
    require(isinstance(groups, list), 'invalid_name_equivalences')
    mapping = {}
    for group in groups:
        require(isinstance(group, dict), 'invalid_equivalence_group')
        canonical = group.get('canonical')
        members = names(group.get('members'))
        require(isinstance(canonical, str) and canonical in members and len(members) >= 2,
                'invalid_equivalence_canonical')
        require(not (members & mapping.keys()), 'overlapping_equivalence_groups')
        require(isinstance(group.get('reviewed_by'), str) and group['reviewed_by'].strip(),
                'equivalence_reviewer_required')
        require(names(group.get('source_refs')), 'equivalence_sources_required')
        mapping.update({name: canonical for name in members})
    return mapping


def evaluate(dataset, predictions, *, threshold):
    require(type(threshold) in (int, float) and math.isfinite(threshold) and 0 <= threshold <= 1, 'invalid_threshold')
    require(dataset.get('schema') == 'kura.character-title-evaluation/1', 'dataset_schema')
    require(predictions.get('schema') == 'kura.character-predictions/1', 'prediction_schema')
    require(dataset.get('reviewed') is True, 'title_mapping_not_reviewed')
    require(isinstance(dataset.get('reviewed_by'), str) and dataset['reviewed_by'].strip(), 'reviewer_required')
    require(isinstance(predictions.get('component'), str) and predictions['component'].strip(), 'component_required')
    model_digest = predictions.get('model_sha256')
    require(isinstance(model_digest, str) and len(model_digest) == 64 and all(c in '0123456789abcdef' for c in model_digest), 'model_digest_required')
    aliases = reviewed_equivalences(dataset)
    normalize = lambda name: aliases.get(name, name)
    vocabulary = names(predictions.get('vocabulary'))
    normalized_vocabulary = {normalize(name) for name in vocabulary}
    pools = dataset.get('pools')
    require(isinstance(pools, list) and pools, 'pools_required')
    seen_pools, seen_posts, compiled = set(), set(), []
    for pool in pools:
        pid = pool.get('pool_id')
        require(type(pid) is int and pid > 0 and pid not in seen_pools, 'invalid_or_duplicate_pool')
        seen_pools.add(pid)
        for field in ('title_text', 'source_ref'):
            require(isinstance(pool.get(field), str) and pool[field].strip(), 'pool_provenance_required')
        expected = {normalize(name) for name in names(pool.get('characters'))}
        require(expected, 'empty_title_labels')
        posts = pool.get('post_ids')
        require(isinstance(posts, list) and posts and all(type(x) is int and x > 0 for x in posts), 'invalid_posts')
        require(len(posts) == len(set(posts)), 'duplicate_post_in_pool')
        require(not (set(posts) & seen_posts), 'overlapping_pools_require_adjudication')
        seen_posts.update(posts)
        compiled.append((pid, expected, posts))
    raw = predictions.get('posts')
    require(isinstance(raw, list), 'predictions_required')
    by_post = {}
    for row in raw:
        pid = row.get('post_id')
        require(type(pid) is int and pid in seen_posts and pid not in by_post, 'unknown_or_duplicate_prediction')
        state = row.get('state')
        require(state in ('completed', 'failed'), 'invalid_prediction_state')
        scores = row.get('scores')
        require(isinstance(scores, dict), 'scores_required')
        require(state != 'failed' or not scores, 'failed_with_scores')
        for tag, score in scores.items():
            require(tag in vocabulary, 'prediction_outside_vocabulary')
            require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1, 'invalid_score')
        by_post[pid] = (state, {normalize(tag) for tag, score in scores.items() if score >= threshold})
    keys = ('posts', 'completed', 'missing', 'failed', 'none', 'any_title_match', 'all_title_match', 'only_unmentioned', 'with_unmentioned')
    total = dict.fromkeys(keys, 0)
    results = []
    for pid, expected, posts in compiled:
        counts = dict.fromkeys(keys, 0)
        counts['posts'] = len(posts)
        for post in posts:
            if post not in by_post:
                counts['missing'] += 1
                continue
            state, proposed = by_post[post]
            if state == 'failed':
                counts['failed'] += 1
                continue
            counts['completed'] += 1
            counts['none'] += not proposed
            counts['any_title_match'] += bool(proposed & expected)
            counts['all_title_match'] += expected <= proposed
            counts['only_unmentioned'] += bool(proposed) and not bool(proposed & expected)
            counts['with_unmentioned'] += bool(proposed - expected)
        results.append(dict(pool_id=pid, counts=counts, out_of_vocabulary=sorted(expected - normalized_vocabulary)))
        for key in keys:
            total[key] += counts[key]
    return dict(outcome='succeeded', state='evaluated', reason='reviewed_title_agreement_only',
                next='inspect coverage and title mappings before component admission; not image-level ground truth',
                component=predictions['component'], model_sha256=model_digest, threshold=threshold,
                totals=total, pools=results, image_accuracy=None,
                name_matching='reviewed_equivalences' if aliases else 'literal',
                name_equivalences=dataset.get('name_equivalences', []),
                scope='pool_title_weak_labels', gt_writes=0)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def read_json(path):
    raw = path.read_bytes()
    return json.loads(raw, object_pairs_hook=_pairs), hashlib.sha256(raw).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--threshold', type=float, required=True)
    args = parser.parse_args(argv)
    try:
        dataset, dh = read_json(args.dataset)
        predictions, ph = read_json(args.predictions)
        result = evaluate(dataset, predictions, threshold=args.threshold)
        result['input_sha256'] = dict(dataset=dh, predictions=ph)
    except (InvalidEvaluation, ValueError, OSError, AttributeError, TypeError) as exc:
        print(json.dumps(dict(outcome='refused', state='not_evaluated',
                             reason=str(exc) if isinstance(exc, InvalidEvaluation) else type(exc).__name__,
                             next='repair or review the input artifact; no writes were made')))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
