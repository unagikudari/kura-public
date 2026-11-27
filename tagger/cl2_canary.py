"""Read-only post-publication evidence. Never claims model accuracy or full GPU placement."""
import argparse,json,hashlib,pathlib
from . import cl2_chars,execution_receipt
from .cl2_publish import require,Refused,THRESHOLD,write_new

def expected_source():
    identity=json.dumps(dict(repository=cl2_chars.REPO,revision=cl2_chars.REVISION,files=cl2_chars.FILES,
      threshold=THRESHOLD,deriver_sha256=cl2_chars.DERIVER_CONTRACT,
      preprocess='rgb-bicubic384-mean0.5-std0.5-sigmoid-v1'),sort_keys=True)
    return 'cl_tagger_v2='+hashlib.sha256(identity.encode()).hexdigest()

def check(conn,published,hosts):
    require(published.get('state')=='published' and published.get('generation')==6,'not_a_published_receipt')
    require(bool(hosts) and len(set(hosts))==len(hosts),'canary_hosts_invalid')
    conn.set_session(readonly=True)
    try:
      with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='30s'")
        rid=published['release_id'];cur.execute('SELECT release_id,generation FROM tagger_release_active WHERE id=1')
        require(cur.fetchone()==(rid,6),'active_changed')
        cur.execute('SELECT release_digest FROM tagger_release_manifest WHERE release_id=%s',(rid,))
        require(cur.fetchall()==[(published['release_digest'],)],'manifest_changed')
        rows=[]
        for host in hosts:
            cur.execute('''SELECT h.profile,h.generation,h.release_id,h.execution_receipt_sha256,r.body,
              extract(epoch FROM now()-h.reported_at)::float
              FROM tagger_expected_worker e JOIN tagger_worker_heartbeat h ON h.host=e.host AND h.profile=e.profile
              LEFT JOIN tagger_execution_receipt r ON r.receipt_sha256=h.execution_receipt_sha256
              WHERE e.host=%s AND h.reported_at>=now()-interval '5 minutes' ''',(host,))
            beats=cur.fetchall();require(len(beats)==1,'canary_heartbeat_missing_or_ambiguous')
            profile,generation,release,ref,body,age=beats[0]
            require((generation,release)==(6,rid),'canary_heartbeat_old')
            execution_receipt.verify(dict(receipt_sha256=ref,body=body),host=host,profile=profile,release_id=rid)
            require(body['component_source']==expected_source(),'canary_character_source_mismatch')
            cur.execute('''SELECT a.id,a.post_id,a.execution_receipt_sha256,r.body FROM post_tag_audit a
              LEFT JOIN tagger_execution_receipt r ON r.receipt_sha256=a.execution_receipt_sha256
              WHERE a.id>%s AND a.release_id=%s AND a.host=%s AND a.source='auto_tag'
              ORDER BY a.id LIMIT 201''',(published['audit_after'],rid,host))
            audit=cur.fetchall();require(bool(audit),'canary_no_completion')
            require(len(audit)<=200,'canary_window_exceeded')
            for _,_,aref,abody in audit:
                execution_receipt.verify(dict(receipt_sha256=aref,body=abody),host=host,profile=profile,release_id=rid)
                require(abody['component_source']==expected_source(),'canary_audit_source_mismatch')
            cur.execute("SELECT count(*) FROM post_tag t JOIN post_tag_pin p USING(post_id,tag_id) WHERE p.state='absent' AND t.post_id=ANY(%s)",([r[1] for r in audit],))
            require(cur.fetchone()==(0,),'canary_negative_pin_conflict')
            rows.append(dict(host=host,profile=profile,age_seconds=age,providers=body['providers'],completions=len(audit),negative_pin_conflicts=0))
        return dict(outcome='succeeded',state='canary_observed',reason='bound_heartbeat_and_completions',next='check_unit_memory_deadline_and_owner_visual_results',release_id=rid,hosts=rows,accuracy_qualified=False,gpu_operator_placement_proven=False)
    finally:conn.rollback()

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--database-env',required=True);ap.add_argument('--published',required=True);ap.add_argument('--host',action='append',required=True);ap.add_argument('--out',required=True);a=ap.parse_args();c=None
    try:
        from dotenv import dotenv_values
        import psycopg2
        c=psycopg2.connect(dotenv_values(a.database_env)['DATABASE_URL'].replace('postgresql+asyncpg:','postgresql:'),connect_timeout=10)
        result=check(c,json.loads(pathlib.Path(a.published).read_text()),a.host);write_new(a.out,result);print(json.dumps(result));return 0
    except Exception as exc:
        print(json.dumps(dict(outcome='refused',state='stopped',reason=str(exc) if isinstance(exc,Refused) else type(exc).__name__,next='inspect_without_republish')));return 1
    finally:
        if c is not None:c.close()
if __name__=='__main__':raise SystemExit(main())
