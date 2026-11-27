"""Describe the next measurement action, never approve publication.

Recompute from explicit judgments. Point floors are not confidence guarantees.
Sensitivity and unresolved-label bounds are diagnostics, not new policy rules.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math

from .stratified_evaluation import evaluate_frames, require


def assess(frames, judgments, *, threshold, policy):
    keys = {'min_precision', 'min_recall', 'min_val_pos_support', 'min_pred_pos_support'}
    require(isinstance(policy, dict) and set(policy) == keys, 'action_policy_incomplete')
    require(all(type(v) in (int, float) and math.isfinite(v) for v in policy.values()),
            'action_policy_invalid')
    for key in ('min_precision', 'min_recall'):
        require(0 < policy[key] <= 1, 'action_policy_invalid')
    policy = dict(policy)
    for key in ('min_val_pos_support', 'min_pred_pos_support'):
        require(policy[key] > 0 and int(policy[key]) == policy[key], 'action_policy_invalid')
        policy[key] = int(policy[key])
    require(isinstance(judgments, dict) and all(v in ('present','absent','uncertain',None)
            for v in judgments.values()), 'judgment_value_invalid')
    # Retain explicit uncertainty in the bound input; project only for metrics.
    definitive = {i: None if v == 'uncertain' else v for i,v in judgments.items()}
    measurement = evaluate_frames(
        frames, definitive, threshold=threshold,
        minimum_positive_support=policy['min_val_pos_support'],
        minimum_prediction_support=policy['min_pred_pos_support'])
    policy.update({k: float(policy[k]) for k in ('min_precision', 'min_recall')})
    # Canonical input binding includes absent keys as explicit unresolved states.
    canonical = dict(schema='human-evaluation-input/2', threshold=float(threshold), policy=policy, frames=sorted([
        dict(scores=sorted((i, float(v)) for i, v in f['scores'].items()),
             sample={k: sorted(v) for k, v in sorted(f['sample'].items())}) for f in frames], key=lambda f: f['scores'][0][0]),
        judgments=sorted((i, judgments.get(i)) for f in frames for ids in f['sample'].values() for i in ids))
    sha = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()
    counts = {k: Fraction(0) for k in ('tp', 'fp', 'tn', 'fn')}
    missing_weight = dict(predicted_present=Fraction(0), predicted_absent=Fraction(0))
    largest_tp = largest_tn = Fraction(0)
    prediction_capacity = 0
    for report in measurement['frames']:
        for name, stratum in report['strata'].items():
            n = stratum['requested']
            if not n:
                continue
            weight = Fraction(stratum['frame_count'], n)
            for key in counts:
                counts[key] += stratum['observed_counts'][key] * weight
            missing_weight[name] += stratum['missing'] * weight
            if stratum['observed_counts']['tp']:
                largest_tp = max(largest_tp, weight)
            if stratum['observed_counts']['tn']:
                largest_tn = max(largest_tn, weight)
            if name == 'predicted_present':
                prediction_capacity += n
    def ratio(n, d):
        return n / d if d else None
    def exposed(v):
        return float(v) if v is not None else None
    def rates(tp, fp, fn):
        return dict(precision=ratio(tp, tp+fp), recall=ratio(tp, tp+fn))
    values = rates(counts['tp'], counts['fp'], counts['fn'])
    mp, mn = missing_weight['predicted_present'], missing_weight['predicted_absent']
    lower = rates(counts['tp'], counts['fp']+mp, counts['fn']+mn)
    upper = rates(counts['tp']+mp, counts['fp'], counts['fn'])
    supported = dict(precision=measurement['observed_support']['predictions'] >= policy['min_pred_pos_support'],
                     recall=measurement['observed_support']['positive'] >= policy['min_val_pos_support'])
    checks = None
    sensitivity = None
    unreachable = []
    if prediction_capacity < policy['min_pred_pos_support']:
        unreachable.append('prediction_support')
    if measurement['observed_support']['positive'] + len(measurement['missing_ids']) < policy['min_val_pos_support']:
        unreachable.append('positive_support')
    uncertain = sorted(i for i in measurement['missing_ids'] if judgments.get(i)=='uncertain')
    unseen = sorted(i for i in measurement['missing_ids'] if judgments.get(i)!='uncertain')
    if uncertain and not unseen:
        state, reason, next_action = ('review_completed_quality_unresolved', 'explicit_uncertainty_remains',
                                     'retain_unknowns_and_review_evidence_limits_without_forcing_labels')
    elif unseen:
        state, reason, next_action = ('awaiting_human_judgments', 'requested_judgments_missing',
                                      'review_remaining_images_without_guessing_labels')
    else:
        checks = {key: (values[key] >= Fraction(str(policy['min_'+key])))
                  if values[key] is not None and supported[key] else None for key in values}
        # One observed true judgment reversed. This is NOT a confidence interval
        # or an extra admission rule; it makes fragile point estimates explicit.
        recall_reversals = []
        if largest_tp:
            recall_reversals.append(ratio(counts['tp']-largest_tp, counts['tp']-largest_tp+counts['fn']))
        if largest_tn:
            recall_reversals.append(ratio(counts['tp'], counts['tp']+counts['fn']+largest_tn))
        defined_reversals = [v for v in recall_reversals if v is not None]
        sensitivity = dict(precision=exposed(ratio(counts['tp']-largest_tp, counts['tp']+counts['fp']))
                           if largest_tp else None,
                           recall=exposed(min(defined_reversals)) if defined_reversals else None,
                           undefined_recall_possible=any(v is None for v in recall_reversals),
                           scope='worst_defined_rate_after_one_true_judgment_reversal_support_not_rechecked',
                           decisions_allowed=False)
        if any(v is False for v in checks.values()):
            state, reason, next_action = ('measured_floor_not_met', 'supported_weighted_point_floor_not_met',
                                          'audit_labels_blind_to_scores_then_prepare_a_new_candidate')
        elif measurement['blockers']:
            state, reason, next_action = ('insufficient_observed_support', 'actual_support_below_policy',
                                          'plan_independent_evidence_without_relabeling_or_reusing_fit_data')
        else:
            state, reason, next_action = ('point_floors_met_review_required',
                                          'point_estimates_are_not_quality_approval',
                                          'review_sampling_sensitivity_provenance_and_fleet_evidence')
    return dict(schema='human-evaluation-next-action/2', outcome='succeeded',
                computation='completed', admission='not_granted',
                state=state, reason=reason, next=next_action, policy=policy,
                threshold=threshold, input_sha256=sha, measurement=measurement,
                responses=dict(decisive=measurement['judged'], uncertain=len(uncertain), unseen=len(unseen)),
                point_floor_checks=checks, support_unreachable_with_this_sample=unreachable,
                unresolved_label_bounds=dict(lower={k: exposed(v) for k,v in lower.items()},
                    upper={k: exposed(v) for k,v in upper.items()},
                    scope='fixed_sample_label_completions_not_population_confidence', decisions_allowed=False,
                    undefined_completion_possible=dict(precision=counts['tp']+counts['fp']+mp == 0,
                                                       recall=counts['tp']+counts['fn'] == 0),
                    none_means='extreme_completion_rate_undefined'),
                single_judgment_sensitivity=sensitivity,
                publication_eligible=False, release_or_hold_changes=False,
                limitations=['Point floors and sensitivity are not confidence bounds or future-traffic guarantees.',
                             'Caller must bind model, threshold, input receipts and fresh explicit judgments.',
                             'Qualification review and governed publication remain separate.',
                             'Per-frame support diagnostics are local; action uses union observed support.',
                             'Explicit uncertainty is retained as unknown, with original sample denominators; review completion is not quality approval.',
                             'Correcting only model disagreements can bias evaluation; audit labels blind to scores and retain original snapshots.'])
