"""Describe explicit human judgments of a fixed stratified evaluation sample.

This is not an admission gate. A raw mixture of oversampled predicted positives
and sampled predicted negatives is not population precision/recall. Report raw
support separately from design-weighted finite-frame estimates, and do not
estimate from a self-selected subset of the requested judgments.
"""
from __future__ import annotations

import math


class EvaluationRefused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise EvaluationRefused(reason)


def evaluate(scores, sample, judgments, *, threshold, minimum_positive_support,
             minimum_prediction_support):
    """Pure read: scores keyed by integer post ID; judgments are present/absent.

    Caller binds immutable frame/model/sample receipts and reads explicit human
    pins. Unknown judgments remain missing. No labels, models or policies are
    changed. A complete sample permits a weighted descriptive estimate, never a
    publication decision or an assertion about future traffic.
    """
    require(type(threshold) in (int, float) and math.isfinite(threshold)
            and 0 < threshold < 1, 'threshold_invalid')
    require(isinstance(scores, dict) and bool(scores), 'scores_invalid')
    require(all(type(i) is int and i > 0 for i in scores), 'score_ids_invalid')
    require(all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
                for v in scores.values()), 'scores_invalid')
    require(type(minimum_positive_support) is int and minimum_positive_support > 0
            and type(minimum_prediction_support) is int and minimum_prediction_support > 0,
            'support_policy_invalid')
    require(isinstance(sample, dict) and set(sample) == {'predicted_present', 'predicted_absent'},
            'strata_invalid')
    requested = set()
    groups = {}
    for name, predicted in (('predicted_present', True), ('predicted_absent', False)):
        ids = sample[name]
        require(isinstance(ids, list) and all(type(i) is int for i in ids)
                and len(ids) == len(set(ids)), 'sample_ids_invalid')
        members = {i for i, score in scores.items() if (score >= threshold) == predicted}
        require(set(ids) <= members and not requested.intersection(ids), 'stratum_mismatch')
        require(bool(ids) == bool(members), 'unsampled_stratum')
        requested.update(ids)
        groups[name] = (ids, len(members), predicted)
    require(isinstance(judgments, dict) and all(type(i) is int for i in judgments)
            and set(judgments) <= requested, 'judgment_ids_invalid')
    require(all(value in ('present', 'absent', None) for value in judgments.values()),
            'judgment_value_invalid')
    raw = dict(tp=0, fp=0, tn=0, fn=0)
    weighted = dict(tp=0., fp=0., tn=0., fn=0.)
    strata = {}
    missing = []
    for name, (ids, population, predicted) in groups.items():
        counts = dict(tp=0, fp=0, tn=0, fn=0)
        absent = []
        for i in ids:
            value = judgments.get(i)
            if value is None:
                absent.append(i)
                continue
            key = ('t' if predicted == (value == 'present') else 'f') + ('p' if predicted else 'n')
            counts[key] += 1
        missing.extend(absent)
        weight = population / len(ids) if ids else None
        for key in counts:
            raw[key] += counts[key]
            weighted[key] += counts[key] * (weight or 0.)
        strata[name] = dict(frame_count=population, requested=len(ids),
                            judged=len(ids)-len(absent), missing=len(absent),
                            inclusion_probability=len(ids)/population if population else None,
                            inverse_probability_weight=weight, observed_counts=counts)
    observed_positive = raw['tp'] + raw['fn']
    observed_predictions = raw['tp'] + raw['fp']
    reasons = []
    if missing:
        reasons.append('requested_judgments_missing')
    if observed_positive < minimum_positive_support:
        reasons.append('positive_support')
    if observed_predictions < minimum_prediction_support:
        reasons.append('prediction_support')
    estimate = None
    if not missing:
        precision_denominator = weighted['tp'] + weighted['fp']
        recall_denominator = weighted['tp'] + weighted['fn']
        estimate = dict(counts=weighted,
                        precision=weighted['tp']/precision_denominator if precision_denominator else None,
                        recall=weighted['tp']/recall_denominator if recall_denominator else None,
                        scope='fixed_scored_frame_only',
                        method='inverse_inclusion_weighted_counts_and_ratio_estimates',
                        uncertainty='not_estimated', independence_assumed=False)
    return dict(schema='stratified-human-evaluation/1',
                state='awaiting_judgments' if missing else 'descriptive_measurement_only',
                frame_count=len(scores), requested=len(requested), judged=sum(raw.values()),
                missing_ids=sorted(missing), observed_counts=raw, strata=strata,
                weighted_estimate=estimate,
                observed_support=dict(positive=observed_positive, predictions=observed_predictions),
                required_support=dict(positive=minimum_positive_support, predictions=minimum_prediction_support),
                blockers=reasons, labels_inferred=False, publication_eligible=False,
                limitations=['Human judgments can be wrong; absence of a pin is not negative.',
                             'Sampling must be fixed before human labels; this function cannot establish provenance.',
                             'Weighted ratios are descriptive, not unbiased ratio estimators or confidence bounds.',
                             'No group-independence, future-traffic quality or release approval is claimed.'])


def evaluate_frames(frames, judgments, *, threshold, minimum_positive_support,
                    minimum_prediction_support):
    """Combine disjoint frozen frames without merging their sampling strata.

    A single review UI may cover multiple frames, but their inclusion weights
    remain separate. Caller binds every frame to the same frozen model and
    threshold. This descriptive reader still never admits a model for publication.
    """
    require(isinstance(frames, list) and bool(frames), 'frames_invalid')
    all_ids, requested = set(), set()
    for frame in frames:
        require(isinstance(frame, dict) and set(frame) == {'scores', 'sample'}, 'frame_invalid')
        require(isinstance(frame['scores'], dict), 'scores_invalid')
        require(not all_ids.intersection(frame['scores']), 'frames_overlap')
        all_ids.update(frame['scores'])
        # Validate each frame with no judgments before extracting requested IDs.
        checked = evaluate(**frame, judgments={}, threshold=threshold,
                           minimum_positive_support=minimum_positive_support,
                           minimum_prediction_support=minimum_prediction_support)
        requested.update(checked['missing_ids'])
    require(isinstance(judgments, dict) and all(type(i) is int for i in judgments)
            and set(judgments) <= requested, 'judgment_ids_invalid')
    reports = []
    for frame in frames:
        ids = {i for values in frame['sample'].values() for i in values}
        reports.append(evaluate(**frame, judgments={i: v for i, v in judgments.items() if i in ids},
                                threshold=threshold, minimum_positive_support=minimum_positive_support,
                                minimum_prediction_support=minimum_prediction_support))
    raw = {k: sum(r['observed_counts'][k] for r in reports) for k in ('tp', 'fp', 'tn', 'fn')}
    missing = sorted(i for r in reports for i in r['missing_ids'])
    reasons = ['requested_judgments_missing'] if missing else []
    if raw['tp'] + raw['fn'] < minimum_positive_support:
        reasons.append('positive_support')
    if raw['tp'] + raw['fp'] < minimum_prediction_support:
        reasons.append('prediction_support')
    estimate = None
    if not missing:
        weighted = {k: sum(r['weighted_estimate']['counts'][k] for r in reports) for k in raw}
        pd, rd = weighted['tp'] + weighted['fp'], weighted['tp'] + weighted['fn']
        estimate = dict(counts=weighted, precision=weighted['tp']/pd if pd else None,
                        recall=weighted['tp']/rd if rd else None, scope='union_of_fixed_scored_frames_only',
                        method='sum_separately_weighted_frame_counts_then_ratio',
                        uncertainty='not_estimated', independence_assumed=False)
    return dict(schema='stratified-multiple-frame-evaluation/1',
                state='awaiting_judgments' if missing else 'descriptive_measurement_only',
                frames=reports, frame_count=len(all_ids), requested=len(requested),
                judged=sum(raw.values()), missing_ids=missing, observed_counts=raw,
                observed_support=dict(positive=raw['tp']+raw['fn'], predictions=raw['tp']+raw['fp']),
                required_support=dict(positive=minimum_positive_support, predictions=minimum_prediction_support),
                weighted_estimate=estimate, blockers=reasons, labels_inferred=False,
                publication_eligible=False, limitations=reports[0]['limitations'])
