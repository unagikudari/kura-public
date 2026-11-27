import copy
import json
import pytest
from tagger.character_eval import InvalidEvaluation, evaluate, main, read_json


def inputs():
    return ({'schema': 'kura.character-title-evaluation/1', 'reviewed': True,
             'reviewed_by': 'fixture-reviewer', 'pools': [
                 {'pool_id': 1, 'title_text': 'Alice and Bob', 'source_ref': 'fixture:title:1',
                  'characters': ['alice', 'bob'], 'post_ids': [1, 2, 3, 4, 5]}]},
            {'schema': 'kura.character-predictions/1', 'component': 'fixture', 'model_sha256': 'a' * 64,
             'vocabulary': ['alice', 'carol'], 'posts': [
                 {'post_id': 1, 'state': 'completed', 'scores': {'alice': .8, 'carol': .9}},
                 {'post_id': 2, 'state': 'completed', 'scores': {}},
                 {'post_id': 3, 'state': 'failed', 'scores': {}},
                 {'post_id': 4, 'state': 'completed', 'scores': {'carol': .8}}]})


def test_partial_match_extra_name_empty_failure_and_missing_are_distinct():
    d, p = inputs()
    result = evaluate(d, p, threshold=.8)
    assert result['totals'] == dict(posts=5, completed=3, missing=1, failed=1, none=1,
                                   any_title_match=1, all_title_match=0, only_unmentioned=1, with_unmentioned=2)
    assert result['pools'][0]['out_of_vocabulary'] == ['bob']
    assert result['image_accuracy'] is None and result['gt_writes'] == 0
    assert 'false_positive' not in result


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), -.1, 1.1, '0.8'])
def test_bad_threshold(value):
    with pytest.raises(InvalidEvaluation, match='invalid_threshold'):
        evaluate(*inputs(), threshold=value)


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), -.1, 1.1, '0.8'])
def test_bad_scores(value):
    d, p = inputs(); p['posts'][0]['scores']['alice'] = value
    with pytest.raises(InvalidEvaluation, match='invalid_score'):
        evaluate(d, p, threshold=.8)


def test_unreviewed_title_mapping_cannot_become_truth():
    d, p = inputs(); d['reviewed'] = False
    with pytest.raises(InvalidEvaluation, match='not_reviewed'):
        evaluate(d, p, threshold=.8)


def test_duplicate_and_overlapping_pool_samples_refuse():
    d, p = inputs(); second = copy.deepcopy(d['pools'][0]); second['pool_id'] = 2; d['pools'].append(second)
    with pytest.raises(InvalidEvaluation, match='overlapping_pools'):
        evaluate(d, p, threshold=.8)
    d, p = inputs(); d['pools'][0]['post_ids'].append(1)
    with pytest.raises(InvalidEvaluation, match='duplicate_post'):
        evaluate(d, p, threshold=.8)


def test_duplicate_prediction_refuses_instead_of_last_write_wins():
    d, p = inputs(); p['posts'].append(copy.deepcopy(p['posts'][0]))
    with pytest.raises(InvalidEvaluation, match='duplicate_prediction'):
        evaluate(d, p, threshold=.8)


def test_out_of_vocabulary_prediction_is_not_valid_empty_result():
    d, p = inputs(); p['posts'][0]['scores']['bob'] = .9
    with pytest.raises(InvalidEvaluation, match='outside_vocabulary'):
        evaluate(d, p, threshold=.8)


def test_duplicate_json_keys_refuse(tmp_path):
    f = tmp_path / 'data.json'; f.write_text('{"reviewed":false,"reviewed":true}')
    with pytest.raises(InvalidEvaluation, match='duplicate_json_key'):
        read_json(f)


def test_cli_is_read_only_and_binds_input_bytes(tmp_path, capsys):
    d, p = inputs(); df = tmp_path / 'dataset.json'; pf = tmp_path / 'predictions.json'
    df.write_text(json.dumps(d)); pf.write_text(json.dumps(p)); before = {x.name: x.read_bytes() for x in tmp_path.iterdir()}
    assert main(['--dataset', str(df), '--predictions', str(pf), '--threshold', '.8']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['input_sha256']['dataset'] == read_json(df)[1]
    assert {x.name: x.read_bytes() for x in tmp_path.iterdir()} == before


def test_two_character_title_does_not_claim_both_present_in_image():
    d, p = inputs(); p['vocabulary'].append('bob'); p['posts'][0]['scores']['bob'] = .8
    result = evaluate(d, p, threshold=.8)
    assert result['totals']['all_title_match'] == 1
    assert result['scope'] == 'pool_title_weak_labels' and result['image_accuracy'] is None


def equivalence():
    return dict(canonical='alice', members=['alice','alice_(series)'],
                reviewed_by='fixture-reviewer', source_refs=['fixture:identity'])


def test_reviewed_alias_matches_without_becoming_an_extra_or_two_characters():
    d,p=inputs();d['name_equivalences']=[equivalence()]
    d['pools'][0]['characters']=['alice_(series)','bob']
    p['vocabulary'].append('alice_(series)')
    p['posts'][0]['scores']={'alice':.8,'alice_(series)':.9}
    r=evaluate(d,p,threshold=.8)
    assert r['totals']['any_title_match']==1
    assert r['totals']['all_title_match']==0  # Bob is still absent.
    assert r['totals']['with_unmentioned']==1  # only the Carol-only post
    assert r['pools'][0]['out_of_vocabulary']==['bob']
    assert r['name_equivalences']==d['name_equivalences']
    assert r['name_matching']=='reviewed_equivalences'
    assert r['image_accuracy'] is None and r['gt_writes']==0


@pytest.mark.parametrize('change', [
    lambda g:g.update(reviewed_by=''),
    lambda g:g.update(source_refs=[]),
    lambda g:g.update(members=['alice','alice']),
    lambda g:g.update(canonical='not-a-member'),
])
def test_unproven_or_invalid_alias_refuses(change):
    d,p=inputs();g=equivalence();change(g);d['name_equivalences']=[g]
    with pytest.raises(InvalidEvaluation):evaluate(d,p,threshold=.8)


def test_alias_chains_and_overlapping_identities_refuse():
    d,p=inputs();g=equivalence();h=equivalence()
    h.update(canonical='alice_(series)',members=['alice_(series)','carol'])
    d['name_equivalences']=[g,h]
    with pytest.raises(InvalidEvaluation,match='overlapping'):evaluate(d,p,threshold=.8)


def test_alias_cannot_smuggle_a_prediction_outside_raw_vocabulary():
    d,p=inputs();d['name_equivalences']=[equivalence()]
    p['posts'][0]['scores']={'alice_(series)':.9}
    with pytest.raises(InvalidEvaluation,match='outside_vocabulary'):evaluate(d,p,threshold=.8)


def test_spelling_similarity_alone_does_not_match():
    d,p=inputs();p['vocabulary']=['alice_(series)','carol']
    p['posts'][0]['scores']={'alice_(series)':.9}
    r=evaluate(d,p,threshold=.8)
    assert r['totals']['any_title_match']==0
    assert r['name_matching']=='literal'
