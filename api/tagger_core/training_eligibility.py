"""Read-only eligibility overlay for NEW training inputs, never a GT rewrite.

Review IDs serialize responses under the post lock. Transaction timestamps do
not serialize ordinary pin writes; those cannot resolve uncertainty by age.
Canonical-only: an unavailable review ledger refuses rather than bypasses it.
"""
import hashlib
import json
import re
from collections import Counter
from contextlib import contextmanager

MAX_REVIEWED_POSTS = 50000
SCHEMA = 'next-training-eligibility/1'

# JSON leaves the SQL adapter independent of psycopg/asyncpg row types. Stable
# observed tag ID survives renames; no tag name in historical JSON is rewritten.
SQL = """WITH RECURSIVE ancestors(id) AS (
 SELECT tag_id FROM tag_name WHERE name=%s AND ord=0
 UNION SELECT m.source_id FROM tag_merge_identity m JOIN ancestors a ON m.target_id=a.id
), latest AS (
 SELECT DISTINCT ON (e.post_id) e.* FROM tagger_human_review_event e
 WHERE e.source_context #>> '{observed,tag_id}' IN (SELECT id::text FROM ancestors)
 AND (e.source_context #>> '{queue,schema}') IS DISTINCT FROM 'human-review-set/3'
 ORDER BY e.post_id,e.id DESC LIMIT 50001)
 SELECT jsonb_build_object('post_id',e.post_id,'event_id',e.id,
 'queue_sha256',e.queue_sha256,'decision',e.decision,
 'observed_tag_id',e.source_context #>> '{observed,tag_id}',
 'observed_pin',e.source_context #> '{observed,pin}',
 'current_pin',CASE WHEN p.post_id IS NULL THEN NULL ELSE jsonb_build_object(
 'state',p.state,'audit_id',p.audit_id,'pinned_by',p.pinned_by,
 'pinned_at',to_char(p.pinned_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US')||'+00:00') END,
 'event_audit_id',e.audit_id,'pin_audit_id',p.audit_id,'pin_state',p.state,
 'audit_human',a.actor_type='human','audit_post_id',a.post_id,
 'gt_verdict',g.verdict) FROM latest e
 LEFT JOIN tag_name tn ON tn.name=%s AND tn.ord=0
 LEFT JOIN post_tag_pin p ON p.post_id=e.post_id AND p.tag_id=tn.tag_id
 LEFT JOIN post_tag_audit a ON a.id=p.audit_id
 LEFT JOIN tagger_gt_label g ON g.post_id=e.post_id AND g.tag_id=tn.tag_id
 ORDER BY e.post_id"""

@contextmanager
def read_snapshot(conn):
    """Own only a fresh canonical transaction; never commit caller work."""
    if not conn.autocommit:
        raise ValueError('training_eligibility_requires_fresh_connection')
    conn.set_session(autocommit=False, readonly=True, isolation_level='REPEATABLE READ')
    try:
        yield
    finally:
        conn.rollback()
        conn.set_session(autocommit=True)


def assess(row):
    """An uncertain response remains unresolved until a newer explicit response.

    Even a definite response requires the exact audit-backed current pin and GT
    verdict; delayed sync or a later ordinary edit must not reuse a stale label.
    """
    d = row['decision']
    if d not in ('present', 'absent', 'uncertain'):
        raise ValueError('training_eligibility_invalid_decision')
    if d == 'uncertain':
        old, current = row['observed_pin'], row['current_pin']
        identity = lambda p: None if p is None else (p.get('state'), p.get('audit_id'), p.get('pinned_by'))
        return ('uncertain' if identity(old) == identity(current) else
                'uncertain_current_pin_order_unresolved')
    if (row['event_audit_id'] is None or
        row['event_audit_id'] != row['pin_audit_id'] or
        row['pin_state'] != d or row['audit_human'] is not True or
        row['audit_post_id'] != row['post_id']):
        return 'current_pin_not_bound_to_response'
    if row['gt_verdict'] != ('positive' if d == 'present' else 'negative'):
        return 'gt_not_synced_to_response'
    return 'current_definite'


def query_spec(tag, *, queue_sha256=None, post_ids=None):
    """One parameterized query shared by synchronous and asynchronous readers."""
    sql, args = SQL, [tag]
    predicates = []
    if queue_sha256 is not None:
        if not isinstance(queue_sha256,str) or re.fullmatch('[0-9a-f]{64}',queue_sha256) is None:
            raise ValueError('training_eligibility_queue_invalid')
        predicates.append('e.queue_sha256=%s'); args.append(queue_sha256)
    if post_ids is not None:
        if (not isinstance(post_ids,list) or not 0<len(post_ids)<=500 or
            any(type(i) is not int or not 0<i<=2147483647 for i in post_ids) or
            len(set(post_ids)) != len(post_ids)):
            raise ValueError('training_eligibility_population_invalid')
        predicates.append('e.post_id=ANY(%s)'); args.append(post_ids)
    if predicates:
        sql=sql.replace('ORDER BY e.post_id,e.id DESC LIMIT',
                        'AND '+' AND '.join(predicates)+' ORDER BY e.post_id,e.id DESC LIMIT')
    args.append(tag)
    return sql, tuple(args)


def load(query, tag, *, queue_sha256=None, post_ids=None):
    isolation, read_only, snapshot = query("SELECT current_setting('transaction_isolation'),current_setting('transaction_read_only'),txid_current_snapshot()::text", ())[0]
    if isolation != 'repeatable read' or read_only != 'on':
        raise ValueError('training_eligibility_requires_readonly_snapshot')
    if len(query("SELECT tag_id FROM tag_name WHERE name=%s AND ord=0", (tag,))) != 1:
        raise ValueError('training_eligibility_current_tag_missing')
    sql, args = query_spec(tag, queue_sha256=queue_sha256, post_ids=post_ids)
    previous_timeout = query("SELECT setting FROM pg_settings WHERE name='statement_timeout'", ())[0][0]
    bounded = int(previous_timeout) == 0 or int(previous_timeout) > 10000
    if bounded:
        query("SELECT set_config('statement_timeout','10000',true)", ())
    # On SQL failure the owning snapshot rolls back. Do not obscure the original
    # refusal by issuing a restoration statement in an aborted transaction.
    rows = [r[0] for r in query(sql, args)]
    if bounded:
        query("SELECT set_config('statement_timeout',%s,true)", (previous_timeout,))
    return seal(rows, tag, snapshot)


def seal(rows, tag, snapshot):
    """Preserve v1 canonical bytes for the same rows/tag/snapshot."""
    if len(rows) > MAX_REVIEWED_POSTS:
        raise ValueError('training_eligibility_read_budget_exceeded')
    entries = []
    for row in rows:
        entries.append(dict(post_id=row['post_id'], event_id=row['event_id'],
                            queue_sha256=row['queue_sha256'], decision=row['decision'],
                            observed_tag_id=row['observed_tag_id'],
                            verdict=row['gt_verdict'], state=assess(row)))
    body = dict(schema=SCHEMA, tag=tag, entries=entries,
                counts=dict(Counter(e['state'] for e in entries)),
                historical_labels_unchanged=True, fresh_quality_granted=False,
                identity_policy='canonical_merge_identity_only', transaction_snapshot=snapshot)
    return dict(body, sha256=hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest())


def validate(overlay):
    keys = {'schema','tag','entries','counts','historical_labels_unchanged',
            'fresh_quality_granted','identity_policy','transaction_snapshot','sha256'}
    if not isinstance(overlay, dict) or set(overlay) != keys:
        raise ValueError('training_eligibility_shape')
    body = {k:v for k,v in overlay.items() if k != 'sha256'}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    if overlay['sha256'] != digest or overlay['schema'] != SCHEMA:
        raise ValueError('training_eligibility_digest')
    if (overlay['historical_labels_unchanged'] is not True or overlay['fresh_quality_granted'] is not False
        or overlay['identity_policy'] != 'canonical_merge_identity_only'
        or not isinstance(overlay['tag'],str) or not 0<len(overlay['tag'])<=500
        or not isinstance(overlay['transaction_snapshot'],str) or re.fullmatch('[0-9]+:[0-9]+:[0-9,]*',overlay['transaction_snapshot']) is None
        or not isinstance(overlay['entries'],list) or len(overlay['entries'])>MAX_REVIEWED_POSTS):
        raise ValueError('training_eligibility_shape')
    seen=set(); last=0
    for entry in overlay['entries']:
        if not isinstance(entry,dict) or set(entry) != {'post_id','event_id','queue_sha256','decision','observed_tag_id','verdict','state'}:
            raise ValueError('training_eligibility_entry_shape')
        pid=entry['post_id']; decision=entry['decision']; state=entry['state']
        if (type(pid) is not int or pid<=last or type(entry['event_id']) is not int or entry['event_id']<=0
            or entry['event_id'] in seen or not isinstance(entry['queue_sha256'],str)
            or re.fullmatch('[0-9a-f]{64}',entry['queue_sha256']) is None
            or not isinstance(entry['observed_tag_id'],str) or re.fullmatch('[1-9][0-9]*',entry['observed_tag_id']) is None
            or decision not in ('present','absent','uncertain') or entry['verdict'] not in (None,'positive','negative')):
            raise ValueError('training_eligibility_entry_shape')
        allowed = ({'uncertain','uncertain_current_pin_order_unresolved'} if decision=='uncertain' else
                   {'current_definite','current_pin_not_bound_to_response','gt_not_synced_to_response'})
        if state not in allowed or (state=='current_definite' and entry['verdict'] != ('positive' if decision=='present' else 'negative')):
            raise ValueError('training_eligibility_state_mismatch')
        seen.add(entry['event_id']);last=pid
    if (not isinstance(overlay['counts'],dict) or any(type(v) is not int for v in overlay['counts'].values())
        or overlay['counts'] != dict(Counter(e['state'] for e in overlay['entries']))):
        raise ValueError('training_eligibility_counts_mismatch')
    return overlay


def excluded(overlay):
    validate(overlay)
    return {e['post_id'] for e in overlay['entries'] if e['state'] != 'current_definite'}


def eligible_ids(overlay, labels):
    """Linear-time filter of (post_id, verdict), retaining untouched history."""
    validate(overlay)
    entries = {e['post_id']: e for e in overlay['entries']}
    return {pid for pid, verdict in labels if pid not in entries or
            (entries[pid]['state'] == 'current_definite' and entries[pid]['verdict'] == verdict)}


def readiness(overlay, queue_responses, queue_sha256, post_ids, groups):
    """Exact queue responses, separate from global current training eligibility.

    A newer response to another queue is not a fresh answer to this queue.
    Missing entries in a provided group mapping refuse. None means unmeasured;
    supplied group counts never imply independence.
    """
    if (not 0<len(post_ids)<=MAX_REVIEWED_POSTS or any(type(i) is not int or i<=0 for i in post_ids)
        or len(post_ids) != len(set(post_ids)) or (groups is not None and any(i not in groups for i in post_ids))):
        raise ValueError('training_readiness_population_invalid')
    validate(overlay)
    entries = {e['post_id']: e for e in overlay['entries']}
    validate(queue_responses)
    if (overlay["tag"] != queue_responses["tag"] or
        overlay["transaction_snapshot"] != queue_responses["transaction_snapshot"]):
        raise ValueError("training_readiness_snapshot_mismatch")
    responses = {e['post_id']: e for e in queue_responses['entries']}
    states = {}
    for pid in post_ids:
        e = responses.get(pid)
        if e is None:
            states[pid] = 'unseen'
        elif e['queue_sha256'] != queue_sha256:
            raise ValueError('training_readiness_wrong_queue')
        elif e['decision'] == 'uncertain':
            states[pid] = 'uncertain'
        elif entries.get(pid, {}).get('event_id') != e['event_id']:
            states[pid] = 'superseded_response'
        else:
            states[pid] = e['state']
    counts = dict(Counter(states.values()))
    state = ('waiting_for_responses' if counts.get('unseen') else
             'blocked_uncertain' if counts.get('uncertain') else
             'responses_bound_not_qualified' if counts.get('current_definite',0)==len(post_ids) else
             'blocked_current_evidence_mismatch')
    return dict(schema='training-support-readiness/1', queue_sha256=queue_sha256,
                outcome='observed', state=state, reason=state,
                next=('evaluate_existing_governed_readiness' if state=='responses_bound_not_qualified' else
                      'review_unanswered_images' if state=='waiting_for_responses' else
                      'await_optional_response_changes' if state=='blocked_uncertain' else
                      'await_gt_synchronization' if counts.get('gt_not_synced_to_response') else
                      'inspect_current_evidence_binding'),
                counts=counts, current_definite={v:sum(states[i]=='current_definite' and entries[i]['verdict']==v for i in post_ids) for v in ('positive','negative')},
                groups_state='not_measured' if groups is None else 'provided',
                groups=None if groups is None else {s:len({groups[i] for i in post_ids if states[i]==s}) for s in counts},
                total=len(post_ids), quality_granted=False, admission='not_granted')
