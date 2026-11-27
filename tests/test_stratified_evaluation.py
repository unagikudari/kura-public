from copy import deepcopy
import pytest
from tagger.stratified_evaluation import evaluate, EvaluationRefused


def measure(scores, sample, labels, **kwargs):
    return evaluate(scores, sample, labels, threshold=.5,
                    minimum_positive_support=20, minimum_prediction_support=20, **kwargs)


def fixture():
    scores = {i: .8 if i < 10 else .1 for i in range(1, 2001)}
    sample = {'predicted_present': list(range(1, 10)),
              'predicted_absent': list(range(10, 160))}
    labels = {i: 'present' if i <= 10 else 'absent' for i in range(1, 160)}
    return scores, sample, labels


def test_weighted_recall_does_not_misrepresent_oversampled_predictions():
    scores, sample, labels = fixture()
    r = measure(scores, sample, labels)
    assert r['observed_counts'] == dict(tp=9, fp=0, fn=1, tn=149)
    assert r['weighted_estimate']['counts']['fn'] == pytest.approx(1991/150)
    assert r['weighted_estimate']['recall'] == pytest.approx(9/(9+1991/150))
    assert r['weighted_estimate']['recall'] < .5  # Raw 9/10 would falsely pass .7.
    assert r['observed_support'] == dict(positive=10, predictions=9)
    assert r['blockers'] == ['positive_support', 'prediction_support']
    assert r['publication_eligible'] is False


@pytest.mark.parametrize('label', [None, 'missing'])
def test_abstention_or_absence_never_becomes_negative_or_estimated(label):
    scores, sample, labels = fixture()
    if label == 'missing':
        del labels[10]
    else:
        labels[10] = None
    r = measure(scores, sample, labels)
    assert r['missing_ids'] == [10]
    assert r['weighted_estimate'] is None
    assert r['observed_counts']['fn'] == 0
    assert r['state'] == 'awaiting_judgments'


def test_zero_predictions_is_unknown_precision_not_perfect_quality():
    r = measure({1: .1, 2: .1}, {'predicted_present': [], 'predicted_absent': [1, 2]},
                {1: 'absent', 2: 'absent'})
    assert r['weighted_estimate']['precision'] is None
    assert r['weighted_estimate']['recall'] is None
    assert not r['publication_eligible']


def test_census_weights_one_and_inputs_never_mutated():
    scores = {i: .8 if i <= 30 else .1 for i in range(1, 61)}
    sample = {'predicted_present': list(range(1, 31)), 'predicted_absent': list(range(31, 61))}
    labels = {i: 'present' if i <= 30 else 'absent' for i in scores}
    before = deepcopy((scores, sample, labels))
    r = measure(scores, sample, labels)
    assert (scores, sample, labels) == before
    assert r['weighted_estimate']['precision'] == r['weighted_estimate']['recall'] == 1
    assert r['blockers'] == [] and not r['publication_eligible']


@pytest.mark.parametrize('bad', ['wrong_stratum', 'duplicate', 'outside', 'unlabelled_stratum',
                                'bad_label', 'nan', 'bool_score', 'bool_id'])
def test_malformed_inputs_refused(bad):
    scores, sample, labels = fixture()
    if bad == 'wrong_stratum': sample['predicted_present'].append(10)
    elif bad == 'duplicate': sample['predicted_present'].append(1)
    elif bad == 'outside': labels[3000] = 'present'
    elif bad == 'unlabelled_stratum': sample['predicted_absent'] = []
    elif bad == 'bad_label': labels[1] = True
    elif bad == 'nan': scores[1] = float('nan')
    elif bad == 'bool_score': scores[1] = True
    elif bad == 'bool_id': scores = {True: .8, **{i:v for i,v in scores.items() if i != 1}}
    with pytest.raises(EvaluationRefused): measure(scores, sample, labels)


def test_separate_frames_keep_different_sampling_weights():
    from tagger.stratified_evaluation import evaluate_frames
    frames = [dict(scores={1: .9, **{i: .1 for i in range(2, 12)}},
                   sample=dict(predicted_present=[1], predicted_absent=[2])),
              dict(scores={20: .9, **{i: .1 for i in range(21, 121)}},
                   sample=dict(predicted_present=[20], predicted_absent=[21]))]
    result = evaluate_frames(frames, {1: 'present', 2: 'present', 20: 'present', 21: 'absent'},
                             threshold=.5, minimum_positive_support=2, minimum_prediction_support=2)
    assert result['observed_counts'] == dict(tp=2, fp=0, fn=1, tn=1)
    assert result['weighted_estimate']['counts'] == dict(tp=2, fp=0, fn=10, tn=100)
    assert result['weighted_estimate']['recall'] == 2/12  # not raw 2/3, nor merged-stratum 2/57
    assert result['blockers'] == [] and result['publication_eligible'] is False
    result = evaluate_frames(frames, {1: 'present', 2: 'present', 20: 'present'},
                             threshold=.5, minimum_positive_support=2, minimum_prediction_support=2)
    assert result['missing_ids'] == [21] and result['weighted_estimate'] is None


def test_frames_overlap_and_unrequested_judgment_are_refused():
    from tagger.stratified_evaluation import evaluate_frames, EvaluationRefused
    import pytest
    frame = dict(scores={1: .9, 2: .1}, sample=dict(predicted_present=[1], predicted_absent=[2]))
    with pytest.raises(EvaluationRefused, match='frames_overlap'):
        evaluate_frames([frame, frame], {}, threshold=.5, minimum_positive_support=1, minimum_prediction_support=1)
    with pytest.raises(EvaluationRefused, match='judgment_ids_invalid'):
        evaluate_frames([frame], {99: 'present'}, threshold=.5, minimum_positive_support=1, minimum_prediction_support=1)


def test_single_frame_aggregation_preserves_zero_stratum():
    from tagger.stratified_evaluation import evaluate_frames
    result = evaluate_frames([dict(scores={1: .1}, sample=dict(predicted_present=[], predicted_absent=[1]))],
                             {1: 'absent'}, threshold=.5, minimum_positive_support=20, minimum_prediction_support=20)
    assert result['weighted_estimate']['counts'] == dict(tp=0, fp=0, tn=1, fn=0)
    assert result['weighted_estimate']['precision'] is None
    assert result['blockers'] == ['positive_support', 'prediction_support']
