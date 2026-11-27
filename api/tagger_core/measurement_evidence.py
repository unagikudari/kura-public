"""Original internal measurements and bound caller assertions; no lifecycle I/O.

This anchor is not a registration, training event, current GT baseline, human
review event, or admission decision. Existing registration contexts are untouched.
"""
from . import qualification_evidence as q

SOURCE_FIELDS = {'snapshot_sha256', 'training_plan_sha256', 'training_result_sha256',
                 'checkpoint_sha256', 'packaging_result_sha256'}
MEASUREMENT_FIELDS = {'artifact_sha256', 'threshold', 'source', 'feature_contract',
                      'internal_validation'}
ANCHOR_FIELDS = MEASUREMENT_FIELDS | {'schema', 'provenance', 'tag_id', 'tag',
                                     'measurement_scope', 'quality_claim'}


def _feature(value):
    q._shape(value, {'schema', 'backbone_sha256', 'preprocessing', 'preprocessing_sha256',
                     'classifier_reset', 'libraries', 'implementation_sha256'})
    q.require(value['schema'] == 'kura-feature-contract/1' and value['classifier_reset'] is True,
              'measurement_feature_contract_invalid')
    for key in ('backbone_sha256', 'preprocessing_sha256', 'implementation_sha256'):
        q._sha(value[key])
    # This is bounded metadata intake, not validation of an executable worker
    # transform. Preserve the declared contract and its canonical fingerprint;
    # actual serving validation belongs to the worker's qualification path.
    declared = value['preprocessing']
    q.require(type(declared) is dict and declared.get('schema') == 'kura-content-preprocessing/1',
              'measurement_preprocessing_invalid')
    q.require(q.digest(declared) == value['preprocessing_sha256'], 'measurement_preprocessing_mismatch')
    q._shape(value['libraries'], {'torch', 'torchvision', 'timm', 'Pillow'})
    for version in value['libraries'].values():
        q._text(version, limit=100)


def _internal(value):
    q._shape(value, {'tp', 'fp', 'tn', 'fn', 'precision', 'recall', 'auc'})
    for key in ('tp', 'fp', 'tn', 'fn'):
        q._integer(value[key], minimum=0)
    q.require(sum(value[k] for k in ('tp', 'fp', 'tn', 'fn')) > 0, 'measurement_support_invalid')
    for key in ('precision', 'recall', 'auc'):
        q._number(value[key]); q.require(value[key] <= 1, 'measurement_metric_invalid')
    # Preserve supplied metrics, but reject a mislabeled count/rate combination.
    for key, denominator in (('precision', value['tp'] + value['fp']),
                             ('recall', value['tp'] + value['fn'])):
        q.require(denominator > 0 and abs(value[key] - value['tp'] / denominator) <= 1e-12,
                  'measurement_metric_mismatch')


def original_measurement(tag_id, tag, measurement):
    """Server supplies canonical tag identity; payload supplies unverified facts."""
    q._integer(tag_id); q._text(tag)
    q.canonical(measurement, max_bytes=q.CONTEXT_MAX_BYTES)
    q._shape(measurement, MEASUREMENT_FIELDS)
    q._sha(measurement['artifact_sha256']); q._threshold(measurement['threshold'])
    q._shape(measurement['source'], SOURCE_FIELDS)
    for value in measurement['source'].values():
        q._sha(value)
    _feature(measurement['feature_contract'])
    _internal(measurement['internal_validation'])
    body = dict(measurement, schema='original-measurement/1', provenance='caller_assertion',
                tag_id=tag_id, tag=tag, measurement_scope='internal_training', quality_claim=False)
    wire = q.canonical(body, max_bytes=q.CONTEXT_MAX_BYTES)
    import json
    return dict(measurement_sha256=q.digest(body), canonical_body=wire, body=json.loads(wire))


def verify_anchor(expected_sha256, body):
    q._sha(expected_sha256); q._shape(body, ANCHOR_FIELDS)
    rebuilt = original_measurement(body['tag_id'], body['tag'], {key: body[key] for key in MEASUREMENT_FIELDS})
    q.require(rebuilt['body'] == body and rebuilt['measurement_sha256'] == expected_sha256,
              'measurement_anchor_mismatch')
    return rebuilt


def assemble(measurement_sha256, anchor, expected_measurement_sha256, kind, report):
    """Reuse report semantics, never synthesize a seven-field registration context."""
    q._sha(expected_measurement_sha256)
    q.require(measurement_sha256 == expected_measurement_sha256, 'measurement_anchor_mismatch')
    verified = verify_anchor(measurement_sha256, anchor)
    q.canonical(report)
    # Shared existing validator only needs artifact and threshold. This local
    # binding is not persisted or exposed as a fabricated registration context.
    q._report(kind, report, {'artifact_sha256': anchor['artifact_sha256'],
                            'metrics': {'threshold': anchor['threshold']}})
    body = dict(schema='measurement-qualification-evidence/1',
                measurement_sha256=verified['measurement_sha256'], kind=kind,
                provenance='caller_assertion', report=report)
    wire = q.canonical(body)
    import json
    return dict(receipt_sha256=q.digest(body), canonical_body=wire, body=json.loads(wire))
