"""Carry release-bound character policy across ordinary WD14/ensemble publications.
Caller transaction owns the active row lock through publish + manifest attachment.
No global threshold lookup, model download, or independent commit.
"""
import json
from sqlalchemy import text
from . import character_policy
from .register_ensemble import ensemble_digest

ACTIVE='SELECT release_id,generation,required_worker_generation FROM tagger_release_active WHERE id=1 FOR UPDATE'
ROWS='''SELECT m.release_digest,c.role,c.ordinal,c.content_sha256,c.deriver_sha256,c.params
 FROM tagger_release_manifest m JOIN tagger_release_component c USING(release_digest)
 WHERE m.release_id=:release AND c.role='character_base' ORDER BY c.ordinal'''
WORKERS='''SELECT e.host,EXISTS(SELECT 1 FROM tagger_worker_heartbeat h WHERE h.host=e.host
 AND h.profile=e.profile AND h.release_id=:release AND h.generation>=:required_generation
 AND h.reported_at>=now()-interval '5 minutes') FROM tagger_expected_worker e ORDER BY e.host'''

class InheritanceRefused(ValueError):pass

def require(ok,reason):
    if not ok:raise InheritanceRefused(reason)

def snapshot(active,rows):
    require(active is not None,'character_predecessor_missing')
    release,generation,required_generation=active
    if not rows:return dict(release=release,generation=generation,required_generation=required_generation,components=[],digest=None)
    digests={r['release_digest'] for r in rows};require(len(digests)==1,'character_predecessor_ambiguous')
    params=rows[0]['params']
    require(isinstance(params,dict),'character_predecessor_params')
    components=[{k:r[k] for k in ('role','ordinal','content_sha256','deriver_sha256','params')} for r in rows]
    description=dict(components=components,threshold_row=dict(producer=params.get('producer'),category=params.get('category'),threshold=params.get('threshold'),evidence=params.get('threshold_evidence')))
    character_policy.decode(dict(id=release,character_component=dict(release_digest=next(iter(digests)),description=description)))
    return dict(release=release,generation=generation,required_generation=required_generation,components=components,digest=next(iter(digests)))

def workers_ready(rows):
    require(bool(rows) and all(r[1] for r in rows),'character_compatible_workers_not_ready')

def overlay(prior,new_release,model_sha):
    provenance=dict(schema='kura-character-inheritance/1',predecessor_release=prior['release'],
                    predecessor_character_manifest=prior['digest'],release_id=new_release,model_sha256=model_sha)
    return ensemble_digest(prior['components'],provenance),provenance

async def lock_predecessor(conn):
    active=(await conn.execute(text(ACTIVE))).one_or_none()
    require(active is not None,'character_predecessor_missing')
    rows=(await conn.execute(text(ROWS),{'release':active[0]})).mappings().all()
    prior=snapshot(tuple(active),rows)
    if prior['components']:
        workers_ready((await conn.execute(text(WORKERS),{'release':prior['release'],'required_generation':prior['required_generation']})).all())
    return prior

async def attach(conn,prior,new_release,model_sha):
    if not prior['components']:return
    current=(await conn.execute(text('SELECT release_id,generation,required_worker_generation FROM tagger_release_active WHERE id=1'))).one()
    require(tuple(current)==(new_release,prior['generation']+1,prior['required_generation']),'character_publish_postcondition')
    digest,provenance=overlay(prior,new_release,model_sha)
    await conn.execute(text('INSERT INTO tagger_release_manifest(release_digest,provenance,created_by,release_id) VALUES(:digest,CAST(:provenance AS jsonb),:creator,:release)'),dict(digest=digest,provenance=json.dumps(provenance),creator='character-inheritance',release=new_release))
    for row in prior['components']:
        await conn.execute(text('INSERT INTO tagger_release_component(release_digest,role,ordinal,content_sha256,deriver_sha256,params) VALUES(:digest,:role,:ordinal,:content_sha256,:deriver_sha256,CAST(:params AS jsonb))'),dict(digest=digest,**{k:v for k,v in row.items() if k!='params'},params=json.dumps(row['params'])))

# psycopg2 ensemble publisher shares validation, encoding and admission with the async path.
def lock_predecessor_sync(cur):
    cur.execute(ACTIVE);active=cur.fetchone();require(active is not None,'character_predecessor_missing')
    cur.execute(ROWS.replace(':release','%s'),(active[0],))
    names=('release_digest','role','ordinal','content_sha256','deriver_sha256','params')
    prior=snapshot(active,[dict(zip(names,r)) for r in cur.fetchall()])
    if prior['components']:
        cur.execute(WORKERS.replace(':release','%s').replace(':required_generation','%s'),(prior['release'],prior['required_generation']))
        workers_ready(cur.fetchall())
    return prior

def attach_sync(cur,prior,new_release,model_sha):
    if not prior['components']:return
    cur.execute('SELECT release_id,generation,required_worker_generation FROM tagger_release_active WHERE id=1')
    require(cur.fetchone()==(new_release,prior['generation']+1,prior['required_generation']),'character_publish_postcondition')
    digest,provenance=overlay(prior,new_release,model_sha)
    cur.execute('INSERT INTO tagger_release_manifest(release_digest,provenance,created_by,release_id) VALUES(%s,%s::jsonb,%s,%s)',(digest,json.dumps(provenance),'character-inheritance',new_release))
    for row in prior['components']:
        cur.execute('INSERT INTO tagger_release_component(release_digest,role,ordinal,content_sha256,deriver_sha256,params) VALUES(%s,%s,%s,%s,%s,%s::jsonb)',(digest,row['role'],row['ordinal'],row['content_sha256'],row['deriver_sha256'],json.dumps(row['params'])))
