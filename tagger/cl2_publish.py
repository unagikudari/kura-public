"""CL character overlay publisher. Default: read-only plan. Rehearse: rollback on a private copy.
No model bytes uploaded; existing release payload and head policies are preserved.
"""
from __future__ import annotations
import argparse,hashlib,json,os,pathlib,re,sys
from . import cl2_chars
from .register_ensemble import ensemble_digest

EVIDENCE='https://github.com/unagikudari/kura/issues/149#issuecomment-6887487658'
THRESHOLD=.45
class Refused(Exception):pass

def require(value,reason):
    if not value:raise Refused(reason)

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def assets(root):
    result=[]
    for name,sha in cl2_chars.FILES.items():
        p=pathlib.Path(root)/name
        require(p.is_file() and cl2_chars.digest(p)==sha,'model_digest_mismatch')
        result.append(dict(content_sha256=sha,byte_size=p.stat().st_size))
    return result

def build_plan(cur,files,expected_release,*,lock=False):
    cur.execute('SELECT release_id,generation FROM tagger_release_active WHERE id=1'+(' FOR UPDATE' if lock else ''))
    row=cur.fetchone();require(row is not None,'active_missing')
    require(row==(expected_release,5),'active_release_or_generation_changed')
    cur.execute('SELECT version_num FROM kura_alembic_version')
    require(cur.fetchall()==[('0192',)],'schema_not_0192')
    cur.execute('SELECT commit_sha,base_model_repo,base_model_revision,patch_set_digest,model_sha256,n_stock,n_out,patched FROM tagger_release WHERE id=%s',(expected_release,))
    source=cur.fetchone();require(source is not None,'source_release_missing')
    cur.execute('SELECT release_digest FROM tagger_release_manifest WHERE release_id=%s ORDER BY release_digest',(expected_release,))
    manifests=[r[0] for r in cur.fetchall()];require(len(manifests)<=1,'source_manifest_ambiguous')
    inherited=[]
    if manifests:
        cur.execute('SELECT role,ordinal,content_sha256,deriver_sha256,params FROM tagger_release_component WHERE release_digest=%s ORDER BY role,ordinal',(manifests[0],))
        inherited=[dict(zip(('role','ordinal','content_sha256','deriver_sha256','params'),r)) for r in cur.fetchall()]
        require(not any(x['role']=='character_base' for x in inherited),'source_already_character_bound')
    cur.execute('SELECT to_jsonb(p) FROM tagger_current_head_policy p ORDER BY tag');heads=[r[0] for r in cur.fetchall()]
    desc=cl2_chars.release_description(threshold=THRESHOLD,evidence=EVIDENCE)
    require(cl2_chars.TOP_RATIO==.8,'ratio_contract_changed')
    provenance=dict(schema='kura-cl2-character-overlay/1',source_release=expected_release,
                    source_model_sha256=source[4],source_manifest=manifests[0] if manifests else None,
                    head_policy_sha256=digest(heads),character_description_sha256=digest(desc))
    components=sorted(inherited+desc['components'],key=lambda r:(r['role'],r['ordinal']))
    plan=dict(schema='kura-cl2-publish-plan/1',source_release=expected_release,generation_before=5,generation_after=6,
              source_metadata=list(source),head_policy_sha256=digest(heads),description=desc,artifacts=files,
              components=components,provenance=provenance,release_digest=ensemble_digest(components,provenance))
    cur.execute("SELECT pg_get_functiondef('tagger_governed_publish_release(text,text,text,text,text,bytea,bytea,integer,integer,jsonb,text,text)'::regprocedure)")
    plan['governed_function_sha256']=hashlib.sha256(cur.fetchone()[0].encode()).hexdigest()
    plan['plan_sha256']=digest(plan)
    return plan

def check_workers(cur,release):
    cur.execute("""SELECT e.host,e.profile,EXISTS(SELECT 1 FROM tagger_worker_heartbeat h
      WHERE h.host=e.host AND h.profile=e.profile AND h.generation=6 AND h.release_id=%s
      AND h.reported_at>=now()-interval '5 minutes')
      FROM tagger_expected_worker e ORDER BY e.host""",(release,))
    rows=cur.fetchall();require(bool(rows),'expected_workers_empty')
    require(all(ok for _,_,ok in rows),'generation6_workers_not_ready')
    return [dict(host=h,profile=p) for h,p,_ in rows]

def publish(conn,plan,files,commit_sha,*,execute=False,private_directory=None,started=lambda:None):
    """Caller owns connection; rollback-only rehearsal still consumes sequence values on its PRIVATE copy."""
    require(re.fullmatch('[0-9a-f]{40}',commit_sha) is not None,'commit_invalid')
    require(digest({k:v for k,v in plan.items() if k!='plan_sha256'})==plan.get('plan_sha256'),'plan_digest_invalid')
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='120s'")
            if not execute:
                require(private_directory is not None,'rehearsal_private_directory_required')
                cur.execute("SELECT inet_server_addr(),current_setting('data_directory')")
                addr,directory=cur.fetchone()
                require(addr is None and pathlib.Path(directory).resolve()==pathlib.Path(private_directory).resolve(),'rehearsal_not_private_endpoint')
            current=build_plan(cur,files,plan['source_release'],lock=True)
            require(current==plan,'plan_changed')
            workers=check_workers(cur,plan['source_release'])
            cur.execute('SELECT coalesce(max(id),0) FROM post_tag_audit');audit_floor=cur.fetchone()[0]
            # The global row is a compatibility mirror, not the release's evidence source.
            cur.execute("SELECT threshold,evidence FROM tagger_producer_threshold WHERE producer='cl_tagger_v2' AND category=4 FOR UPDATE")
            row=cur.fetchone();require(row is None or row==(THRESHOLD,EVIDENCE),'threshold_row_conflict')
            started()
            for a in files:
                cur.execute('INSERT INTO tagger_artifact(content_sha256,byte_size) VALUES(%s,%s) ON CONFLICT DO NOTHING',(a['content_sha256'],a['byte_size']))
                cur.execute('SELECT byte_size FROM tagger_artifact WHERE content_sha256=%s',(a['content_sha256'],))
                require(cur.fetchone()==(a['byte_size'],),'artifact_size_conflict')
            cur.execute('INSERT INTO tagger_release_manifest(release_digest,provenance,created_by) VALUES(%s,%s::jsonb,%s)',(plan['release_digest'],json.dumps(plan['provenance']),'cl2-governed-publisher'))
            for c in plan['components']:
                cur.execute('INSERT INTO tagger_release_component(release_digest,role,ordinal,content_sha256,deriver_sha256,params) VALUES(%s,%s,%s,%s,%s,%s::jsonb)',(plan['release_digest'],c['role'],c['ordinal'],c['content_sha256'],c['deriver_sha256'],json.dumps(c['params'])))
            cur.execute("INSERT INTO tagger_producer_threshold(producer,category,threshold,evidence) VALUES('cl_tagger_v2',4,%s,%s) ON CONFLICT DO NOTHING",(THRESHOLD,EVIDENCE))
            cur.execute("SELECT threshold,evidence FROM tagger_producer_threshold WHERE producer='cl_tagger_v2' AND category=4")
            require(cur.fetchone()==(THRESHOLD,EVIDENCE),'threshold_row_race')
            # Keep 392MB release bytes entirely server-side. No direct active-pointer write.
            cur.execute('''SELECT tagger_governed_publish_release(%s,r.base_model_repo,r.base_model_revision,r.patch_set_digest,
              r.model_sha256,r.model_data,r.selected_tags_csv,r.n_stock,r.n_out,r.patched,r.lockfile,%s)
              FROM tagger_release r WHERE r.id=%s''',(commit_sha,'cl2-governed-publisher',plan['source_release']))
            new=cur.fetchone()[0]
            cur.execute('UPDATE tagger_release_manifest SET release_id=%s WHERE release_digest=%s',(new,plan['release_digest']))
            cur.execute('SELECT release_id,generation FROM tagger_release_active WHERE id=1')
            require(cur.fetchone()==(new,6),'governed_generation_postcondition')
            cur.execute('SELECT to_jsonb(p) FROM tagger_current_head_policy p ORDER BY tag')
            require(digest([r[0] for r in cur.fetchall()])==plan['head_policy_sha256'],'head_policy_changed')
            result=dict(outcome='succeeded',state='published' if execute else 'rehearsed_rolled_back',reason='governed_generation_cas',next='run_readonly_canary',release_id=new,generation=6,release_digest=plan['release_digest'],plan_sha256=plan['plan_sha256'],workers=workers,audit_after=audit_floor)
        if execute:conn.commit()
        else:conn.rollback()
        return result
    except BaseException:
        conn.rollback();raise

def write_new(path,body):
    with pathlib.Path(path).open('x') as f:
        os.fchmod(f.fileno(),0o600);json.dump(body,f,indent=2);f.flush();os.fsync(f.fileno())
    fd=os.open(pathlib.Path(path).parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    mode=ap.add_mutually_exclusive_group();mode.add_argument('--execute',action='store_true');mode.add_argument('--rehearse',action='store_true')
    ap.add_argument('--database-env',required=True);ap.add_argument('--model-dir',required=True)
    ap.add_argument('--expect-release',type=int,required=True);ap.add_argument('--plan');ap.add_argument('--out',required=True)
    ap.add_argument('--private-data-directory');ap.add_argument('--publishers-frozen',action='store_true')
    a=ap.parse_args();conn=None
    try:
        require(not pathlib.Path(a.out).exists(),'receipt_exists')
        files=assets(a.model_dir)
        from dotenv import dotenv_values
        import psycopg2,subprocess
        db=dotenv_values(a.database_env).get('DATABASE_URL');require(bool(db),'database_url_missing')
        conn=psycopg2.connect(db.replace('postgresql+asyncpg:','postgresql:'),connect_timeout=10)
        if not (a.execute or a.rehearse):
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='30s'");result=build_plan(cur,files,a.expect_release)
            conn.rollback();write_new(a.out,result)
            print(json.dumps(dict(outcome='succeeded',state='planned_readonly',reason='candidate_bound',next='rehearse_on_private_copy',plan_sha256=result['plan_sha256'])));return 0
        require(bool(a.plan),'plan_required');require(a.publishers_frozen,'publishers_freeze_not_acknowledged')
        plan=json.loads(pathlib.Path(a.plan).read_text());require(plan['source_release']==a.expect_release,'expected_release_mismatch')
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=pathlib.Path(__file__).resolve().parents[1],text=True).strip()
        once=a.out+'.intent.json'
        result=publish(conn,plan,files,commit,execute=a.execute,private_directory=a.private_data_directory,
                       started=lambda:write_new(once,dict(plan_sha256=plan['plan_sha256'],execute=a.execute,commit=commit)))
        write_new(a.out,result);print(json.dumps(result));return 0
    except Exception as exc:
        # Lost commit acknowledgment is ambiguous. Inspect active/manifest; never blindly retry.
        result=dict(outcome='refused',state='stopped',reason=str(exc) if isinstance(exc,Refused) else type(exc).__name__,next='inspect_active_and_manifest_before_any_new_attempt')
        print(json.dumps(result));return 1
    finally:
        if conn is not None:conn.close()
if __name__=='__main__':raise SystemExit(main())
