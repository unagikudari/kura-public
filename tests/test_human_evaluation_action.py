from copy import deepcopy

import pytest

from tagger.human_evaluation_action import assess
from tagger.stratified_evaluation import EvaluationRefused


POLICY = dict(min_precision=.85, min_recall=.7, min_val_pos_support=20,
              min_pred_pos_support=20)


def inputs():
    scores = {i: .9 if i <= 30 else .1 for i in range(1, 61)}
    frame = dict(scores=scores, sample=dict(predicted_present=list(range(1, 31)),
                                          predicted_absent=list(range(31, 61))))
    return [frame], {i: 'present' if i <= 30 else 'absent' for i in scores}


def test_complete_good_sample_moves_to_review_never_publication():
    frames, labels = inputs()
    before = deepcopy((frames, labels, POLICY))
    result = assess(frames, labels, threshold=.5, policy=POLICY)
    assert result['state'] == 'point_floors_met_review_required'
    assert result['point_floor_checks'] == dict(precision=True, recall=True)
    assert not result['publication_eligible'] and not result['release_or_hold_changes']
    assert (frames, labels, POLICY) == before


def test_missing_answers_do_not_become_failure_or_pass():
    frames, labels = inputs()
    del labels[1]
    result = assess(frames, labels, threshold=.5, policy=POLICY)
    assert result['state'] == 'awaiting_human_judgments'
    assert result['measurement']['weighted_estimate'] is None
    assert result['point_floor_checks'] is None


def test_perfect_but_small_sample_cannot_claim_support():
    frame = dict(scores={1: .9, 2: .1},
                 sample=dict(predicted_present=[1], predicted_absent=[2]))
    result = assess([frame], {1: 'present', 2: 'absent'}, threshold=.5, policy=POLICY)
    assert result['state'] == 'insufficient_observed_support'
    assert result['measurement']['observed_support'] == dict(positive=1, predictions=1)


def test_weighted_missed_positive_routes_to_repair_not_raw_recall_pass():
    scores = {i: .9 if i <= 30 else .1 for i in range(1, 1031)}
    frame = dict(scores=scores, sample=dict(predicted_present=list(range(1, 31)),
                                          predicted_absent=[31, 32]))
    labels = {i: 'present' for i in range(1, 32)} | {32: 'absent'}
    result = assess([frame], labels, threshold=.5, policy=POLICY)
    assert result['measurement']['observed_counts']['fn'] == 1
    assert result['measurement']['weighted_estimate']['recall'] == pytest.approx(30 / 530)
    assert result['state'] == 'measured_floor_not_met'
    assert result['point_floor_checks'] == dict(precision=True, recall=False)
    assert not result['publication_eligible']


def test_new_human_correction_recomputes_decision_without_mutating_old_result():
    frames, labels = inputs()
    old = assess(frames, labels, threshold=.5, policy=POLICY)
    labels.update({i: 'absent' for i in range(1, 7)})
    new = assess(frames, labels, threshold=.5, policy=POLICY)
    assert old['state'] == 'point_floors_met_review_required'
    assert new['state'] == 'measured_floor_not_met'
    assert new['point_floor_checks']['precision'] is False


@pytest.mark.parametrize('key,value', [('min_precision', float('nan')),
    ('min_recall', 0), ('min_precision', True), ('min_pred_pos_support', 1.5),
    ('min_val_pos_support', -1), ('min_recall', '0.7')])
def test_invalid_policy_refused(key, value):
    frames, labels = inputs()
    with pytest.raises(EvaluationRefused):
        assess(frames, labels, threshold=.5, policy=POLICY | {key: value})


def test_missing_policy_never_uses_defaults():
    frames, labels = inputs()
    with pytest.raises(EvaluationRefused, match='action_policy_incomplete'):
        assess(frames, labels, threshold=.5, policy={})


def test_supported_failure_is_not_hidden_by_other_insufficient_support():
    frame = dict(scores={i: .9 for i in range(1, 201)},
                 sample=dict(predicted_present=list(range(1, 201)), predicted_absent=[]))
    labels = {i: 'present' if i <= 5 else 'absent' for i in range(1, 201)}
    r = assess([frame], labels, threshold=.5, policy=POLICY)
    assert r['state'] == 'measured_floor_not_met'
    assert r['point_floor_checks'] == dict(precision=False, recall=None)


def test_fragile_recall_is_exposed_not_presented_as_quality_approval():
    frame = dict(scores={i: .9 if i <= 30 else .1 for i in range(1, 10031)},
                 sample=dict(predicted_present=list(range(1, 31)), predicted_absent=[31]))
    labels = {i: 'present' if i <= 30 else 'absent' for i in range(1, 32)}
    r = assess([frame], labels, threshold=.5, policy=POLICY)
    assert r['single_judgment_sensitivity']['recall'] < .003
    assert r['reason'] == 'point_estimates_are_not_quality_approval'
    assert r['admission'] == 'not_granted' and not r['publication_eligible']


def test_binding_changes_with_threshold_or_judgment_and_normalizes_support():
    frames, labels = inputs()
    r = assess(frames, labels, threshold=.5, policy=POLICY | {'min_val_pos_support': 20.0})
    assert type(r['policy']['min_val_pos_support']) is int
    changed_threshold = assess(frames, labels, threshold=.6, policy=POLICY)
    labels[1] = 'absent'
    changed_label = assess(frames, labels, threshold=.5, policy=POLICY)
    assert len({r['input_sha256'], changed_threshold['input_sha256'], changed_label['input_sha256']}) == 3


def test_unresolved_bounds_and_unreachable_support_are_not_decisions():
    frame = dict(scores={1: .9, 2: .1}, sample=dict(predicted_present=[1], predicted_absent=[2]))
    r = assess([frame], {}, threshold=.5, policy=POLICY)
    assert r['support_unreachable_with_this_sample'] == ['prediction_support', 'positive_support']
    assert r['unresolved_label_bounds']['lower'] == dict(precision=0, recall=0)
    assert r['unresolved_label_bounds']['upper'] == dict(precision=1, recall=1)
    assert r['unresolved_label_bounds']['decisions_allowed'] is False
    assert r['measurement']['weighted_estimate'] is None
    explicit_none = assess([frame], {1: None, 2: None}, threshold=.5, policy=POLICY)
    assert r['input_sha256'] == explicit_none['input_sha256']


def test_fractional_weights_do_not_fail_exact_policy_equality():
    scores = {i: .9 if i <= 50 else .1 for i in range(1, 61)}
    frame = dict(scores=scores, sample=dict(predicted_present=list(range(1, 31)),
                                           predicted_absent=list(range(51, 61))))
    labels = {i: 'present' if i <= 24 else 'absent' for i in range(1, 31)}
    labels.update({i: 'absent' for i in range(51, 61)})
    r = assess([frame], labels, threshold=.5, policy=POLICY | {'min_precision': .8})
    assert r['point_floor_checks']['precision'] is True


def test_unknown_policy_key_refused():
    frames, labels = inputs()
    with pytest.raises(EvaluationRefused):
        assess(frames, labels, threshold=.5, policy=POLICY | {'new_floor': 1})


def test_heavy_present_stratum_reversal_can_dominate_recall_sensitivity():
    frame = dict(scores={i: .9 if i <= 2000 else .1 for i in range(1, 3001)},
                 sample=dict(predicted_present=list(range(1, 21)), predicted_absent=list(range(2001, 3001))))
    labels = {i: 'present' for i in range(1, 21)} | {i: 'present' if i <= 2500 else 'absent' for i in range(2001, 3001)}
    r = assess([frame], labels, threshold=.5, policy=POLICY)
    assert r['single_judgment_sensitivity']['recall'] == pytest.approx(1900/2400)
    assert r['single_judgment_sensitivity']['decisions_allowed'] is False


def test_already_absent_judgments_can_make_positive_support_unreachable():
    frames, _ = inputs()
    labels = {i: 'absent' for i in range(1, 46)}
    r = assess(frames, labels, threshold=.5, policy=POLICY)
    assert r['support_unreachable_with_this_sample'] == ['positive_support']
    assert r['state'] == 'awaiting_human_judgments'


def test_undefined_completion_is_not_a_clean_confidence_interval():
    frame = dict(scores={1: .9, 2: .1}, sample=dict(predicted_present=[1], predicted_absent=[2]))
    r = assess([frame], {}, threshold=.5, policy=POLICY)
    assert r['unresolved_label_bounds']['undefined_completion_possible'] == dict(precision=False, recall=True)


def test_two_frames_bound_separate_weights_and_order_invariance():
    frames = [dict(scores={1: .9, **{i: .1 for i in range(2, 12)}},
                   sample=dict(predicted_present=[1], predicted_absent=[2])),
              dict(scores={20: .9, **{i: .1 for i in range(21, 121)}},
                   sample=dict(predicted_present=[20], predicted_absent=[21]))]
    labels = {1: 'present', 2: 'present', 20: 'present', 21: 'absent'}
    policy = POLICY | {'min_val_pos_support': 1, 'min_pred_pos_support': 1}
    a = assess(frames, labels, threshold=.5, policy=policy)
    b = assess(list(reversed(frames)), labels, threshold=.5, policy=policy)
    assert a['input_sha256'] == b['input_sha256']
    assert a['measurement']['weighted_estimate']['recall'] == pytest.approx(2/12)
    del labels[21]
    r = assess(frames, labels, threshold=.5, policy=policy)
    assert r['unresolved_label_bounds']['lower']['recall'] == pytest.approx(2/112)
    assert r['unresolved_label_bounds']['upper']['recall'] == pytest.approx(2/12)
    assert r['state'] == 'awaiting_human_judgments'
