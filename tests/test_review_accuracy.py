from tagger.review_accuracy import build_report


def test_before_after_uses_paired_rows_and_keeps_findings_out_of_independent_metrics():
    releases = {
        side: dict(release_sha256=char * 64, base_sha256='a' * 64, patch_sha256=char * 64)
        for side, char in [('before', 'b'), ('after', 'c')]
    }
    manifest = dict(releases=releases, sets={})
    reviews = {}
    for tag, ref, origin, schema in (
        ('scat', 'd' * 64, 'lora_findings', 'human-review-set/2'),
        ('peeing', 'e' * 64, 'lifecycle', 'human-review-set/3'),
    ):
        manifest['sets'][ref] = dict(tag=tag, frame_sha256='f' * 64,
            thresholds=dict(before=.5, after=.5),
            scores={str(pid): dict(display_sha256='1' * 64, before=a, after=b)
                    for pid, a, b in ((1, .9, .1), (2, .1, .8), (3, .8, None), (4, .1, .8))})
        reviews[ref] = dict(tag=tag, schema=schema, origin=origin, purpose='fixture',
                            frame_sha256='f' * 64, answers=[
            dict(post_id=pid, decision=decision, display_sha256='1' * 64)
            for pid, decision in ((1, 'absent'), (2, 'present'), (3, 'present'), (4, 'uncertain'))
        ])
    report = build_report(manifest, reviews)
    findings, blind = report['sets']
    assert findings['before']['false_tag_rate'] == findings['before']['missed_tag_rate'] == 1
    assert findings['after']['false_tag_rate'] == findings['after']['missed_tag_rate'] == 0
    assert findings['coverage'] == dict(total=4, compared=2, unanswered=0, uncertain=1, missing_scores=1)
    assert findings['change'] == dict(corrected=2, regressed=0)
    assert blind['training_overlap'] == 'unverified'
    assert all(not row['eligible_for_improvement_claim'] for row in report['sets'])
    assert not any(row['matched'] for row in report['confusion_pairs'])


def test_score_manifest_checks_picture_identity_and_counts_missing_predictions():
    import pytest
    from tagger.review_accuracy_manifest import assemble

    review = dict(tag='fixture', frame_sha256='a' * 64,
                  answers=[dict(post_id=1, display_sha256='b' * 64)])
    before = dict(sha256='1' * 64, release={}, sets={'ref': dict(
        tag='fixture', frame_sha256='a' * 64, threshold=.4,
        predictions={'1': dict(display_sha256='b' * 64, score=.6)})})
    after = dict(sha256='2' * 64, release={}, sets={'ref': dict(
        tag='fixture', frame_sha256='a' * 64, threshold=.4, predictions={})})
    result = assemble(before, after, {'ref': review})
    assert result['sets']['ref']['scores']['1'] == dict(display_sha256='b' * 64, before=.6, after=None)
    assert result['side_evidence_sha256']['before'] == before['sha256']
    after['sets']['ref']['predictions']['1'] = dict(display_sha256='c' * 64, score=.6)
    with pytest.raises(ValueError, match='score image mismatch'):
        assemble(before, after, {'ref': review})


def test_blind_manifest_excludes_a_bound_unanswered_hidden_member():
    ref, display = 'a' * 64, 'b' * 64
    identity = dict(tag='fixture', frame_sha256='c' * 64)
    evidence = {'1': dict(reason='hidden_pool_only', display_sha256=display)}
    manifest = dict(
        releases={side: dict(release_sha256=ref, base_sha256=ref, patch_sha256=ref)
                  for side in ('before', 'after')},
        sets={ref: dict(**identity, excluded=evidence, thresholds=dict(before=.5, after=.5),
                        scores={'1': dict(display_sha256=display, before=.1, after=.9)})})
    review = dict(**identity, schema='human-review-set/3', origin='lifecycle', purpose='base_comparison',
                  answers=[dict(post_id=1, display_sha256=display, decision=None)])
    [result] = build_report(manifest, {ref: review})['sets']
    assert result['coverage']['total'] == result['coverage']['unanswered'] == 0
    assert result['excluded'] == evidence
    assert not result['eligible_for_improvement_claim']
