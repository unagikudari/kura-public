"""Immutable qualification assertions, never admission or proof of attribution.

All three closed report types bind artifact_sha256, threshold and observed_at.
Human judgments retain uncertain separately; runtime failures are observations;
an approve verdict is only its caller's claim. No I/O or authority is exercised.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import math
import re

CONTEXT_MAX_BYTES = 256 * 1024
BODY_MAX_BYTES = 512 * 1024
CONTEXT_FIELDS = {'id', 'tag', 'tag_id', 'lifecycle_event_id', 'artifact_sha256', 'metrics', 'gt'}
SHA = re.compile(r'[0-9a-f]{64}')


class QualificationEvidenceRefused(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise QualificationEvidenceRefused(reason)


def _json(value, depth=0):
    require(depth <= 32, 'json_too_deep')
    if type(value) is str:
        require('\x00' not in value and not any(0xD800 <= ord(c) <= 0xDFFF for c in value), 'json_text_invalid')
        return
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        require(math.isfinite(value), 'nonfinite_number')
        return
    if type(value) is list:
        for item in value:
            _json(item, depth + 1)
        return
    require(type(value) is dict and all(type(k) is str for k in value), 'json_type_invalid')
    for key, item in value.items():
        _json(key, depth + 1)
        _json(item, depth + 1)


def canonical(value, *, max_bytes=BODY_MAX_BYTES):
    _json(value)
    try:
        wire = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, OverflowError) as exc:
        raise QualificationEvidenceRefused('json_invalid') from exc
    require(len(wire.encode('ascii')) <= max_bytes, 'document_too_large')
    return wire


def digest(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


def _sha(value):
    require(type(value) is str and SHA.fullmatch(value) is not None, 'sha256_invalid')


def _integer(value, *, minimum=1):
    require(type(value) is int and minimum <= value <= 2**63-1, 'integer_invalid')


def _number(value, *, minimum=0):
    require(type(value) in (int, float) and minimum <= value <= 2**63-1 and math.isfinite(value), 'number_invalid')


def _text(value, *, limit=200):
    require(type(value) is str and bool(value.strip()) and len(value) <= limit
            and all(c.isprintable() for c in value), 'text_invalid')


def _time(value):
    _text(value, limit=64)
    # Require a date-time, not a date or Python's permissive alternate separator.
    require('T' in value, 'timestamp_invalid')
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'timestamp_timezone_required')
    except (ValueError, OverflowError) as exc:
        if isinstance(exc, QualificationEvidenceRefused):
            raise
        raise QualificationEvidenceRefused('timestamp_invalid') from exc


def _threshold(value):
    _number(value)
    require(0 < value < 1, 'threshold_invalid')


def context(registration):
    """Capture immutable DB fields; ignore mutable status/reason/time columns."""
    require(isinstance(registration, Mapping) and CONTEXT_FIELDS <= registration.keys(), 'registration_context_incomplete')
    value = {key: registration[key] for key in CONTEXT_FIELDS}
    _integer(value['id']); _integer(value['tag_id'])
    if value['lifecycle_event_id'] is not None:
        _integer(value['lifecycle_event_id'])
    _text(value['tag']); _sha(value['artifact_sha256'])
    require(type(value['metrics']) is dict and 'threshold' in value['metrics'], 'metrics_threshold_required')
    _threshold(value['metrics']['threshold'])
    require(value['gt'] is None or type(value['gt']) is dict, 'gt_invalid')
    # Canonical round-trip both checks all nested data and detaches caller objects.
    return json.loads(canonical(value, max_bytes=CONTEXT_MAX_BYTES))


def _shape(value, keys):
    require(type(value) is dict and set(value) == keys, 'report_shape_invalid')


def _hashes(values, *, minimum=1, maximum=1024):
    require(type(values) is list and minimum <= len(values) <= maximum, 'hash_list_invalid')
    for value in values:
        _sha(value)
    require(len(set(values)) == len(values), 'duplicate_hash')


def _report(kind, report, binding):
    common = {'artifact_sha256', 'threshold', 'observed_at'}
    fields = {
        'human_sample': {'frame_sha256', 'sample_sha256', 'policy_sha256', 'judgments'},
        'runtime': {'source_sha256', 'inventory_sha256', 'inputs_sha256', 'host', 'profile',
                    'device', 'provider', 'model_inventory', 'memory_limit_bytes', 'peak_bytes',
                    'threads', 'deadline_s', 'elapsed_s', 'completed', 'total', 'oom_events', 'parity_passed'},
        'independent_review': {'evidence_sha256s', 'reviewer', 'reviewed_at', 'verdict', 'findings'},
    }
    require(type(kind) is str and kind in fields, 'evidence_kind_invalid')
    if kind == "runtime" and isinstance(report, dict) and "parity" in report:
        common.add("parity")
    if kind == "independent_review" and isinstance(report, dict) and "answers_sha256" in report:
        common.add("answers_sha256")
        _sha(report["answers_sha256"])
    _shape(report, common | fields[kind])
    _sha(report['artifact_sha256']); _threshold(report['threshold']); _time(report['observed_at'])
    require(report['artifact_sha256'] == binding['artifact_sha256'], 'artifact_binding_mismatch')
    require(report['threshold'] == binding['metrics']['threshold'], 'threshold_binding_mismatch')
    if kind == 'human_sample':
        for key in ('frame_sha256', 'sample_sha256', 'policy_sha256'):
            _sha(report[key])
        rows = report['judgments']
        require(type(rows) is list and len(rows) <= 10000, 'judgments_invalid')
        ids = set()
        for row in rows:
            _shape(row, {'post_id', 'state', 'reviewer', 'reviewed_at', 'source_ref'})
            _integer(row['post_id']); _text(row['reviewer']); _time(row['reviewed_at']); _sha(row['source_ref'])
            require(row['state'] in ('present', 'absent', 'uncertain'), 'judgment_state_invalid')
            require(row['post_id'] not in ids, 'duplicate_judgment')
            ids.add(row['post_id'])
    elif kind == 'runtime':
        for key in ('source_sha256', 'inventory_sha256', 'inputs_sha256'):
            _sha(report[key])
        for key in ('host', 'profile', 'device', 'provider'):
            _text(report[key])
        _hashes(report['model_inventory'])
        require(binding['artifact_sha256'] in report['model_inventory'], 'candidate_missing_from_inventory')
        for key in ('memory_limit_bytes', 'threads', 'total'):
            _integer(report[key])
        for key in ('peak_bytes', 'completed', 'oom_events'):
            _integer(report[key], minimum=0)
        _number(report['deadline_s']); require(report['deadline_s'] > 0, 'deadline_invalid')
        _number(report['elapsed_s'])
        require(report['completed'] <= report['total'], 'completion_invalid')
        # This runtime configuration's comparison only: None means not measured,
        # False measured and failed, True measured and passed. Packaging parity
        # for the same artifact cannot stand in for a runtime comparison.
        require(report['parity_passed'] is None or type(report['parity_passed']) is bool,
                'parity_invalid')
        parity = report.get("parity")
        if "parity" in report and parity is None:
            require(report["parity_passed"] is not True, "parity_diagnostics_mismatch")
        if parity is not None:
            _shape(parity, {"passed", "max_absolute_error", "decision_differences", "tolerance", "n"})
            _integer(parity["n"])
            _integer(parity["decision_differences"], minimum=0)
            _number(parity["max_absolute_error"])
            _number(parity["tolerance"])
            require(parity["n"] == report["completed"] == report["total"]
                    and 0 <= parity["max_absolute_error"] <= 1
                    and 0 < parity["tolerance"] <= 0.0001
                    and parity["decision_differences"] <= parity["n"], "parity_diagnostics_invalid")
            passed = parity["max_absolute_error"] <= parity["tolerance"] and parity["decision_differences"] == 0
            require(parity["passed"] is passed and report["parity_passed"] is passed,
                    "parity_diagnostics_mismatch")
        # Exceeding resource limits or failing parity is valid negative evidence.
    else:
        _hashes(report['evidence_sha256s']); _text(report['reviewer']); _time(report['reviewed_at'])
        require(report['verdict'] in ('approve', 'revise'), 'review_verdict_invalid')
        require(type(report['findings']) is list and len(report['findings']) <= 100, 'findings_invalid')
        for finding in report['findings']:
            _text(finding, limit=2000)


def assemble(registration_context, expected_context_sha256, kind, report):
    """Validate and freeze an assertion; compare the caller's expected context."""
    require(isinstance(registration_context, Mapping) and set(registration_context) == CONTEXT_FIELDS,
            'context_shape_invalid')
    binding = context(registration_context)
    context_wire = canonical(binding, max_bytes=CONTEXT_MAX_BYTES)
    context_sha = hashlib.sha256(context_wire.encode('ascii')).hexdigest()
    _sha(expected_context_sha256)
    require(expected_context_sha256 == context_sha, 'registration_context_mismatch')
    # Check nested JSON before semantic membership/comparisons.
    canonical(report)
    _report(kind, report, binding)
    body = dict(schema='qualification-evidence/1', registration_id=binding['id'],
                registration_context_sha256=context_sha, kind=kind,
                provenance='caller_assertion', report=report)
    wire = canonical(body)
    return dict(receipt_sha256=hashlib.sha256(wire.encode('ascii')).hexdigest(),
                canonical_body=wire, body=json.loads(wire), context_sha256=context_sha,
                context_canonical=context_wire, context=binding)


def admission_status():
    """Receipt intake has no qualification, publication or hold authority."""
    return dict(admission='not_granted', publication_eligible=False, release_or_hold_changes=False)
