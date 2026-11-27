"""Private PostgreSQL: canonical requests keep existing human answer identity."""
import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from alembic.migration import MigrationContext
from alembic.operations import Operations

sys.path.insert(0,str(Path(__file__).parent))
from test_human_review_live import pg,url,queue,setup,USER
from api.services import review_set_registry as registry
from api.services import human_review
from api.routers import human_review as routes
from api.errors import KuraError

M=importlib.import_module("api.alembic.versions.0203_review_set_registry")


def document():
    q=queue()
    q.pop("queue_sha256")
    raw=json.dumps(q,sort_keys=True).encode()
    ref=hashlib.sha256(raw).hexdigest()
    return ref,{ref+".json":raw}


async def migrate(engine, *revisions):
    from sqlalchemy.ext.asyncio import create_async_engine
    privileged=create_async_engine(engine.url.set(username="postgres"))
    try:
        async with privileged.begin() as conn:
            await conn.execute(text("SET SESSION AUTHORIZATION kura"))
            def upgrade(sync):
                for migration in (M, *revisions):
                    migration.op = Operations(MigrationContext.configure(sync))
                    migration.upgrade()
            await conn.run_sync(upgrade)
    finally:await privileged.dispose()


def test_import_is_atomic_idempotent_and_answers_need_no_files(url,tmp_path,monkeypatch):
    monkeypatch.setenv("KURA_REVIEW_QUEUE_DIR",str(tmp_path/"absent"))
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            ref,blobs=document()
            async with ss() as db:
                result=await registry.import_sets(db,blobs)
                assert result["inserted"]==1
                await db.rollback()
                assert await registry.load(db)==[]
            async with ss() as db:
                await registry.import_sets(db,blobs);await db.commit()
            async with ss() as db:
                assert (await registry.import_sets(db,blobs))["inserted"]==0
                await db.commit()
                q=await human_review.resolve_queue(db,ref)
                assert q["post_ids"]==[101,102]
                context=await routes.review_image(ref,101,db,USER)
                body=routes.Judgment(request_id="ad22dc1d-628b-41da-b112-d8c10a998313",
                    expected_context_sha256=context["context_sha256"],decision="uncertain")
                first=await routes.judge(ref,101,body,db,USER)
                second=await routes.judge(ref,101,body,db,USER)
                assert first["event_id"]==second["event_id"] and second["replayed"]
                page=await routes.review_set(ref,db,USER)
                assert page["remaining"]==1 and page["counts"]["uncertain"]==1
                assert page["quality_granted"] is False
                count=(await db.execute(text("SELECT count(*) FROM tagger_review_set_member"))).scalar_one()
                assert count==2
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_population_and_history_cannot_drift(url):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            ref,blobs=document()
            async with ss() as db:
                await registry.import_sets(db,blobs);await db.commit()
                for statement in (
                    "DELETE FROM tagger_review_set",
                    "UPDATE tagger_review_set_member SET post_id=999",
                    "TRUNCATE tagger_review_set_member",
                ):
                    with pytest.raises(Exception,match="permission denied"):
                        await db.execute(text(statement))
                    await db.rollback()
                with pytest.raises(Exception,match="permission denied"):
                    await db.execute(text("INSERT INTO tagger_review_set_member VALUES(:ref,2,999,NULL)"),{"ref":ref})
                    await db.commit()
                await db.rollback()
                assert len(await registry.load(db))==1
        finally:await engine.dispose()
    asyncio.run(go())


def test_invalid_batch_preserves_legacy_bridge_until_atomic_activation(url,tmp_path,monkeypatch):
    ref,blobs=document()
    for name,raw in blobs.items():(tmp_path/name).write_bytes(raw)
    monkeypatch.setenv("KURA_REVIEW_QUEUE_DIR",str(tmp_path))
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            async with ss() as db:
                assert len(await registry.load_configured(db))==1
                assert (await human_review.resolve_queue(db,ref))["queue_sha256"]==ref
                assert await registry.available(db) is False
                with pytest.raises(ValueError):
                    await registry.import_sets(db,blobs|{"f"*64+".json":b"{}"})
                await db.rollback()
                assert (await db.execute(text("SELECT count(*) FROM tagger_review_set"))).scalar_one()==0
        finally:await engine.dispose()
    asyncio.run(go())



@pytest.mark.parametrize("change",[
    {"post_ids":[]},{"post_ids":[True]},{"post_ids":[0]},{"post_ids":[101,101]},
    {"source_provenance":{}},{"media_sha256":{}},{"purpose":"publish"},
    {"extra":"field"},{"tag":None},{"frame_sha256":None},
    {"frame_sha256":int("1"*64)},{"model_sha256":int("1"*64)},
    {"media_sha256":{"101":int("1"*64),"102":"a"*64}},
])
def test_malformed_governed_registration_cannot_poison_registry(url,change):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            ref,blobs=document()
            raw=json.dumps(dict(json.loads(next(iter(blobs.values()))),**change)).encode()
            async with ss() as db:
                with pytest.raises(Exception):
                    await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":raw})
                await db.rollback()
                assert await registry.load(db)==[]
                assert await registry.available(db) is False
        finally:await engine.dispose()
    asyncio.run(go())


def test_two_imports_in_one_transaction_keep_constraints_atomic(url):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            ref,blobs=document()
            q=json.loads(next(iter(blobs.values())))
            raw=json.dumps(dict(q,frame_sha256="d"*64)).encode()
            another={hashlib.sha256(raw).hexdigest()+".json":raw}
            async with ss() as db:
                await registry.import_sets(db,blobs)
                await registry.import_sets(db,another)
                await db.commit()
                assert len(await registry.load(db))==2
                assert await registry.available(db) is True
                assert (await db.execute(text("SELECT initial_sets FROM tagger_review_registry_activation"))).scalar_one()==1
                assert (await db.execute(text("SELECT tableowner FROM pg_tables WHERE tablename='tagger_review_set'"))).scalar_one()=="kura_reconciler"
        finally:await engine.dispose()
    asyncio.run(go())

def test_sql_boundary_accepts_valid_bound_successor_and_rejects_ambiguity(url):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            ref,blobs=document();q=json.loads(next(iter(blobs.values())))
            q.update(purpose="candidate_evaluation",model_sha256="d"*64)
            old={k:v for k,v in q.items() if k not in ("media_sha256","source_provenance")}
            old["schema"]="human-review-set/1"
            raw=json.dumps(old).encode();oldref=hashlib.sha256(raw).hexdigest()
            q["source_provenance"]=dict(kind="frozen_candidate_sample",artifact_sha256="c"*64,supersedes_queue_sha256=oldref)
            newraw=json.dumps(q).encode();newref=hashlib.sha256(newraw).hexdigest()
            async with ss() as db:
                await registry.import_sets(db,{oldref+".json":raw,newref+".json":newraw});await db.commit()
                rows=await registry.load(db)
                assert next(row for row in rows if row["queue_sha256"]==oldref)["superseded_by"]==newref
                q["source_provenance"]["artifact_sha256"]="e"*64
                with pytest.raises(Exception,match="review_supersession_ambiguous"):
                    await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":json.dumps(q).encode()})
                await db.rollback()
        finally:await engine.dispose()
    asyncio.run(go())


def test_raw_exponent_id_is_refused_before_jsonb_normalization(url):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            ref,blobs=document()
            raw=next(iter(blobs.values())).replace(b'[101, 102]',b'[1.01e2, 102]')
            assert b'1.01e2' in raw
            async with ss() as db:
                with pytest.raises(Exception,match="review_population_invalid"):
                    await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":raw})
                await db.rollback()
                assert await registry.load(db)==[]
        finally:await engine.dispose()
    asyncio.run(go())


@pytest.mark.parametrize("explicit",[False,True])
def test_long_unicode_population_uses_real_link_contract(url,explicit):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            name="例"*128
            async with ss() as db:
                await db.execute(text("UPDATE tag_name SET name=:name WHERE tag_id=1 AND ord=0"),{"name":name})
                await db.commit()
                q=queue();q.pop("queue_sha256")
                q.update(tag=name,post_ids=list(range(1000000000,1000000500)))
                if explicit:
                    q["media_sha256"]={str(i):"a"*64 for i in q["post_ids"]}
                else:
                    q.pop("media_sha256");q.pop("source_provenance")
                    q.update(schema="human-review-set/1",purpose="fixed_evaluation",model_sha256="b"*64)
                raw=json.dumps(q,ensure_ascii=False).encode()
                if not explicit:
                    with pytest.raises(Exception,match="review_link_too_long"):
                        await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":raw})
                    await db.rollback()
                    assert await registry.load(db)==[]
                else:
                    await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":raw})
                    await db.commit()
                    rows=await registry.load(db)
                    assert rows[0]["browse_url"]=="/tagger-review/"+hashlib.sha256(raw).hexdigest()
        finally:await engine.dispose()
    asyncio.run(go())


@pytest.mark.parametrize("suffix",["\u200b","\u00a0","\ue000","\u00ad"])
def test_unprintable_canonical_tag_cannot_poison_reader(url,suffix):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            async with ss() as db:
                name="fixture"+suffix
                await db.execute(text("UPDATE tag_name SET name=:n WHERE tag_id=1 AND ord=0"),{"n":name})
                await db.commit()
                ref,blobs=document()
                raw=json.dumps(dict(json.loads(next(iter(blobs.values()))),tag=name)).encode()
                with pytest.raises(Exception,match="review_identity_invalid"):
                    await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":raw})
                await db.rollback()
                assert await registry.load(db)==[]
        finally:await engine.dispose()
    asyncio.run(go())


def test_shared_tag_predicate_at_every_pinned_range_boundary(url):
    from api.services.review_tag_contract_v1 import RANGES,valid
    points={1,0x10ffff}
    for a,b in RANGES:
        points.update((max(1,a-1),a,b-1,b))
    # PostgreSQL UTF8 refuses surrogate codepoints before any SQL function.
    points=sorted(n for n in points if n<=0x10ffff and not 0xd800<=n<=0xdfff)
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine)
            async with ss() as db:
                values=[chr(n) for n in points]
                rows=(await db.execute(text("SELECT v,tagger_review_tag_valid(v) FROM unnest(CAST(:v AS text[])) v"),{"v":values})).all()
                assert all(result is valid(v) for v,result in rows)
        finally:await engine.dispose()
    asyncio.run(go())


def test_evaluation_answers_track_canonical_revisions(url):
    from tagger.canonical_evaluation import (evaluate_queue, freeze_scored_frame,
                                             request_candidate_review)

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, importlib.import_module("api.alembic.versions.0204_direct_candidate_review"))
            frame = freeze_scored_frame("c" * 64, {101: .9, 102: .1},
                                        threshold=.5, per_stratum=1, seed=42)
            assert frame == freeze_scored_frame("c" * 64, {102: .1, 101: .9},
                                                threshold=.5, per_stratum=1, seed=42)
            policy = dict(min_precision=.9, min_recall=.5, min_val_pos_support=1,
                          min_pred_pos_support=1)
            legacy_ref, legacy_blobs = document()
            async with sessions() as db:
                assert await registry.evaluation_answers(db, legacy_ref) is None
                await registry.import_sets(db, legacy_blobs)
                await db.commit()
                inputs = dict(tag="fixture", frame=frame,
                              display_hashes={int(i): sha for i, sha in queue()["media_sha256"].items()},
                              measurement_sha256="d" * 64, snapshot_sha256="e" * 64)
                ref = await request_candidate_review(db, **inputs)
                assert ref == await request_candidate_review(db, **inputs)
                await db.commit()
                initial = await registry.evaluation_answers(db, ref)
                assert [a["decision"] for a in initial["answers"]] == [None, None]
                assert initial["model_sha256"] == "c" * 64
                with pytest.raises(ValueError, match="evaluation_answers_unresolved"):
                    await registry.require_current_evaluation_answers(db, ref, initial["sha256"])
                await db.rollback()
                for tail, decision in [(1, "uncertain"), (2, "absent")]:
                    context = await routes.review_image(ref, 101, db, USER)
                    body = routes.Judgment(
                        request_id=f"ad22dc1d-628b-41da-b112-d8c10a99831{tail}",
                        expected_context_sha256=context["context_sha256"], decision=decision,
                    )
                    event = await routes.judge(ref, 101, body, db, USER)
                    updated = await registry.evaluation_answers(db, ref)
                    assert updated["sha256"] != initial["sha256"]
                    with pytest.raises(ValueError, match="evaluation_answers_stale"):
                        await registry.require_current_evaluation_answers(db, ref, initial["sha256"])
                    await db.rollback()
                    assert updated == await registry.evaluation_answers(db, ref)
                    assert updated["answers"][0]["decision"] == decision
                    assert updated["answers"][0]["event_id"] == event["event_id"]
                    assert updated["answers"][1]["decision"] is None
                    assert (await db.execute(text(
                        "SELECT state FROM post_tag_pin WHERE post_id=101 AND tag_id=1"
                    ))).scalar_one() == "present"
                    assert (await db.execute(text(
                        "SELECT audit_id FROM tagger_human_review_event WHERE id=:id"
                    ), {"id": event["event_id"]})).scalar_one() is None
                    receipt = await evaluate_queue(db, ref, frame, policy=policy)
                    assert receipt["answers_sha256"] == updated["sha256"]
                    assert receipt["report"]["responses"] == {
                        "decisive": int(decision == "absent"),
                        "uncertain": int(decision == "uncertain"), "unseen": 1,
                    }
                    assert receipt["report"]["publication_eligible"] is False
                    initial = updated
                context = await routes.review_image(ref, 102, db, USER)
                await routes.judge(ref, 102, routes.Judgment(
                    request_id="ad22dc1d-628b-41da-b112-d8c10a998313",
                    expected_context_sha256=context["context_sha256"], decision="absent",
                ), db, USER)
                complete = await registry.evaluation_answers(db, ref)
                assert await registry.require_current_evaluation_answers(db, ref, complete["sha256"]) == complete
                async with sessions() as other:
                    key = "tagger-evaluation-tag:fixture"
                    assert not (await other.execute(text(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"
                    ), {"key": key})).scalar_one()
                await db.rollback()
                with pytest.raises(ValueError, match="scored_review_frame_binding_mismatch"):
                    await evaluate_queue(db, ref, dict(frame, threshold=.4), policy=policy)
        finally:
            await engine.dispose()
    asyncio.run(go())


@pytest.mark.parametrize("scope", [
    "catalog_sample_outside_frozen_gt_groups", "catalog_extension_outside_frozen_gt_groups",
])
def test_fenced_candidate_registration_creates_review_once(url, tmp_path, monkeypatch, scope):
    from api.routers.tagger import RegistrationRequestIn, submit_registration_request
    from api.tagger_core.transitions import acquire_lease, record_lifecycle_event
    from tagger.canonical_evaluation import candidate_review_document, freeze_scored_frame

    from tagger.canonical_evaluation import refresh_registered_evaluations, review_evidence_state
    from tagger.dedicated_protocol import write_once

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, importlib.import_module(
                "api.alembic.versions.0204_direct_candidate_review"), importlib.import_module(
                "api.alembic.versions.0205_candidate_evaluation_history"), importlib.import_module(
                "api.alembic.versions.0206_content_head_execution"), importlib.import_module(
                "api.alembic.versions.0192_tagger_execution_receipt"))
            async with sessions() as db:
                _, legacy = document()
                await registry.import_sets(db, legacy)
                await db.commit()
                lease = await acquire_lease(db, "lifecycle:fixture", lease_seconds=60)
                await record_lifecycle_event(db, tag="fixture", from_stage=None,
                                             to_stage="candidate", reason="fixture", lease=lease)
                event = await record_lifecycle_event(db, tag="fixture", from_stage="candidate",
                                                     to_stage="training", reason="fixture", lease=lease)
                frame = freeze_scored_frame("c" * 64, {101: .9, 102: .1},
                                            threshold=.5, per_stratum=1, seed=42)
                request = candidate_review_document(
                    tag="fixture", frame=frame, snapshot_sha256="e" * 64,
                    measurement_sha256="d" * 64,
                    display_hashes={int(i): sha for i, sha in queue()["media_sha256"].items()},
                )
                qualification = {
                    "schema": "dedicated-serving-qualification/1", "tag": "fixture",
                    "snapshot_sha256": "e" * 64, "measurement_sha256": "d" * 64,
                    "bundle_sha256": "c" * 64, "threshold": .5,
                    "state": "awaiting_production_precision", "parity": {"passed": True},
                    "heldout": {"tp": 215, "fp": 10, "tn": 799, "fn": 85,
                                "precision": 215 / 225, "recall": 215 / 300},
                }
                metrics = {
                    "production_review": {"request": request, "frame_sha256": request["frame_sha256"],
                                          "measurement_sha256": "d" * 64, "frame": frame},
                    "serving_qualification": dict(qualification, sha256=human_review.digest(qualification)),
                    "snapshot_sha256": "e" * 64, "threshold": .5,
                }
                from tagger.label_identity import label_identity, read_label_rows
                gt = {"positive": 1, "negative": 1,
                      "label_sha256": label_identity(await read_label_rows(db, "fixture"))}
                metrics["production_review"]["scope"] = scope
                trial_inputs = {
                    "schema": "tagger-worker-trial-inputs/1", "artifact_sha256": "c" * 64,
                    "snapshot_sha256": "e" * 64, "threshold": .5,
                    "examples": [
                        {"post_id": 101, "display_sha256": "a" * 64, "reference_score": .6},
                        {"post_id": 102, "display_sha256": "b" * 64, "reference_score": .4},
                    ],
                }
                metrics["production_review"].update(
                    worker_trial_inputs=trial_inputs,
                    worker_trial_inputs_sha256=human_review.digest(trial_inputs),
                )
                body = RegistrationRequestIn(
                    tag="fixture", source_host="offline", artifact_ref="fixture.pt",
                    artifact_sha256="c" * 64, lifecycle_event_id=event,
                    lease_scope=lease.scope, fencing_token=lease.fencing_token, metrics=metrics, gt=gt,
                )
                with pytest.raises(KuraError):
                    await submit_registration_request(body.model_copy(update={"artifact_sha256": "f" * 64}),
                                                      db, USER)
                await db.rollback()
                assert len(await registry.load(db)) == 1
                with pytest.raises(KuraError) as missing:
                    await submit_registration_request(body, db, USER)
                assert missing.value.error == "production_review_artifact_unavailable"
                await db.rollback()
                assert len(await registry.load(db)) == 1
                await db.execute(text("""
                    INSERT INTO tagger_artifact(content_sha256,byte_size,kind,kind_checked_at)
                    VALUES (:sha,42,'content_head_bundle',clock_timestamp())
                """), {"sha": body.artifact_sha256})
                await db.commit()
                mismatched = {**metrics, "production_review": {
                    **metrics["production_review"], "frame": {**frame, "scores": []},
                }}
                with pytest.raises(KuraError) as invalid:
                    await submit_registration_request(body.model_copy(update={"metrics": mismatched}), db, USER)
                assert invalid.value.error == "production_review_frame_invalid"
                await db.rollback()
                first = await submit_registration_request(body, db, USER)
                retry = await submit_registration_request(body, db, USER)
                assert first.id == retry.id
                from tagger.auto_review import review_pending
                for _ in range(2):
                    assessment = await review_pending(db)
                    assert assessment["owner_decision"] == [
                        ("fixture", "awaiting_production_checker_answers")]
                    assert (await db.execute(text(
                        "SELECT status FROM tagger_registration_request WHERE id=:id"
                    ), {"id": first.id})).scalar_one() == "pending"
                assert len(await registry.load(db)) == 2
                assert (await db.execute(text(
                    "SELECT count(*) FROM tagger_registration_request"
                ))).scalar_one() == 1
                key = hashlib.sha256(f"fixture@{event}".encode()).hexdigest()[:24]
                received = tmp_path / key / "received"
                assert not received.exists()
                ref = human_review.digest(request)
                for _ in range(2):
                    assert await refresh_registered_evaluations(db, root=tmp_path) == {
                        "evaluated": 1, "unavailable": [],
                    }
                assert len(list((received / "evaluations").glob("*.json"))) == 1
                context = await routes.review_image(ref, 101, db, USER)
                await routes.judge(ref, 101, routes.Judgment(
                    request_id="ad22dc1d-628b-41da-b112-d8c10a998311",
                    expected_context_sha256=context["context_sha256"], decision="uncertain",
                ), db, USER)
                assert await refresh_registered_evaluations(db, root=tmp_path) == {
                    "evaluated": 1, "unavailable": [],
                }
                assert len(list((received / "evaluations").glob("*.json"))) == 2
                assert (await db.execute(text(
                    "SELECT count(DISTINCT answers_sha256) FROM tagger_candidate_evaluation"
                ))).scalar_one() == 2
                from api.tagger_core.config import load_config
                candidate_row = {"id": first.id, "metrics": dict(metrics, threshold=.5),
                                 "artifact_sha256": body.artifact_sha256}
                config = await load_config(db)
                assert await review_evidence_state(db, candidate_row, config) == (
                    "awaiting_production_checker_answers")
                for post_id in (101, 102):
                    view = await routes.review_image(ref, post_id, db, USER)
                    await routes.judge(ref, post_id, routes.Judgment(
                        request_id=f"ad22dc1d-628b-41da-b112-d8c10a998{post_id:03d}",
                        expected_context_sha256=view["context_sha256"],
                        decision="present" if post_id == 101 else "absent",
                    ), db, USER)
                assert await review_evidence_state(db, candidate_row, config) == (
                    "awaiting_current_production_evaluation")
                await refresh_registered_evaluations(db, root=tmp_path)
                assert await review_evidence_state(db, candidate_row, config) == (
                    "production_evaluation_insufficient_support")
                from tagger.canonical_evaluation import require_publication_review
                latest_answers = await registry.evaluation_answers(db, ref)
                metrics["production_review"].pop("scope")
                with pytest.raises(ValueError, match="publication_review_scope_ineligible"):
                    await require_publication_review(
                        db, candidate_row, config, answers_sha256=latest_answers["sha256"])
                metrics["production_review"]["scope"] = "catalog_sample_outside_frozen_gt_groups"
                candidate_row["metrics"]["snapshot_sha256"] = "e" * 64
                with pytest.raises(ValueError, match="production_evaluation_insufficient_support"):
                    await require_publication_review(
                        db, candidate_row, config, answers_sha256=latest_answers["sha256"])
                from tagger.candidate_publication import require_candidate_publication
                release = (await db.execute(text("""
                    SELECT tagger_governed_publish_release(
                      'fixture','fixture','fixture','fixture','fixture',
                      decode('00','hex'),decode('00','hex'),1,1,'{}'::jsonb,NULL,'fixture')
                """))).scalar_one()
                publication = dict(expected_release_id=release, expected_predecessor=None,
                                   answers_sha256=latest_answers["sha256"])
                with pytest.raises(ValueError, match="publication_release_changed"):
                    await require_candidate_publication(
                        db, first.id, **(publication | {"expected_release_id": release + 1}))
                if scope == "catalog_extension_outside_frozen_gt_groups":
                    with pytest.raises(ValueError, match="publication_review_scope_ineligible"):
                        await require_candidate_publication(db, first.id, **publication)
                    return
                with pytest.raises(ValueError, match="production_evaluation_insufficient_support"):
                    await require_candidate_publication(db, first.id, **publication)
                for revision in range(20):
                    version = dict(request, source_provenance=dict(
                        request["source_provenance"], measurement_sha256=f"{revision:064x}"))
                    latest = await registry.register_candidate_set(db, version)
                await db.commit()
                assert (await db.execute(text("SELECT count(*) FROM tagger_review_set"))).scalar_one() == 22
                current = {q["queue_sha256"] for q in await registry.load(db)}
                assert latest in current and ref not in current
                assert (await routes.review_set(ref, db, USER))["counts"]["present"] == 1
                from tagger.candidate_publication import apply_admitted_candidate
                from sqlalchemy.exc import DBAPIError
                row = {"id": first.id, "tag": "fixture", "tag_id": 1,
                       "lifecycle_event_id": event, "artifact_sha256": "c" * 64,
                       "metrics": metrics, "gt": {"positive": 215, "negative": 809}}
                policy = {"threshold": .5, "operating_point": "fixture",
                          "apply_mode": "replace", "gate_tags": []}
                with pytest.raises(DBAPIError):
                    await apply_admitted_candidate(db, row, lease=lease,
                                                   policy=policy | {"apply_mode": "invalid",
                                                                    "measured_recall": .7, "measured_fpr": .01})
                assert (await db.execute(text(
                    "SELECT count(*) FROM tagger_patch"
                ))).scalar_one() == 0
                assert (await db.execute(text(
                    "SELECT status FROM tagger_registration_request WHERE id=:id"
                ), {"id": first.id})).scalar_one() == "pending"
                from api.services import qualification_evidence as evidence
                from api.tagger_core import config as config_module
                from api.tagger_core import qualification_evidence as contract
                from tagger import candidate_publication as publisher
                from tests.test_qualification_evidence import report

                migration = importlib.import_module("api.alembic.versions.0199_qualification_evidence")
                for statement in migration.STATEMENTS:
                    await db.execute(text(statement))
                await db.execute(text("DELETE FROM tagger_expected_worker"))
                await db.execute(text("""
                    INSERT INTO tagger_expected_worker(host,description,profile)
                    VALUES ('primary','fixture','full')
                """))
                test_config = config | {"min_val_pos_support": 1, "min_pred_pos_support": 1}

                async def pinned_config(_):
                    return test_config

                monkeypatch.setattr(config_module, "load_config", pinned_config)
                monkeypatch.setattr(publisher, "load_config", pinned_config)
                await refresh_registered_evaluations(db, root=tmp_path)
                context = await evidence.registration_context(db, first.id)
                bind = {"artifact_sha256": "c" * 64, "threshold": .5}
                runtime = report("runtime") | bind | {"host": "primary", "model_inventory": ["c" * 64], "total": 2, "completed": 2}
                # Verify discovery and collection using the actual registry and
                # runtime evidence schema. Remote transport is injected here.
                from api.routers import tagger as tagger_routes
                from tagger import worker_trial_dispatch as trials
                inventory = []
                read_policy_rows = tagger_routes.read_head_policy_rows

                async def current_rows(session):
                    return [*await read_policy_rows(session), *inventory]

                await db.commit()
                monkeypatch.setattr(tagger_routes, "read_head_policy_rows", current_rows)
                knobs = ("host", "profile", "source_sha256", "device", "provider",
                         "memory_limit_bytes", "threads", "deadline_s")
                settings = {"submitter_id": 42, "candidate_policies": {
                    "fixture": {"apply_mode": "replace", "operating_point": "fixture", "gate_tags": []},
                }, "workers": [{field: runtime[field] for field in knobs}]}
                assert not (received / "worker-trial-inputs.json").exists()
                declaration = await trials.generate_trial_declaration(
                    sessions, settings, state_root=tmp_path)
                assert len(declaration["trials"]) == 1
                path = declaration["trials"][0]["plan_path"]
                assert trials.protocol.read_verified(path)["total"] == 2
                runtime["inventory_sha256"] = trials.protocol.read_verified(path)["inventory_sha256"]
                runtime["inputs_sha256"] = metrics["production_review"]["worker_trial_inputs_sha256"]
                def unreachable(*args, **kwargs):
                    raise TimeoutError("worker unreachable")

                monkeypatch.setattr(trials, "dispatch", unreachable)
                assert await trials.run_trial_plan(sessions, declaration, state_root=tmp_path) == [{
                    "registration_id": first.id, "host": "primary",
                    "state": "unavailable", "error": "TimeoutError",
                }]
                launches = []
                monkeypatch.setattr(trials, "dispatch", lambda *a, **kw: (
                    launches.append(1) or {"state": "submitted"}))
                monkeypatch.setattr(trials, "collect", lambda *a: {"state": "running"})
                assert (await trials.run_trial_plan(sessions, declaration, state_root=tmp_path))[0]["state"] == "running"
                monkeypatch.setattr(trials, "collect", lambda *a: {"state": "missing"})
                assert (await trials.run_trial_plan(sessions, declaration, state_root=tmp_path))[0]["state"] == "missing"
                assert len(launches) == 1
                monkeypatch.setattr(trials, "collect", lambda *a: {"state": "collected", "report": runtime})
                collected = await trials.run_trial_plan(sessions, declaration, state_root=tmp_path)
                assert collected[0]["state"] == "collected" and len(launches) == 2
                observed = await evidence.attach(db, first.id, expected_context_sha256=contract.digest(context),
                                                 kind="runtime", report=runtime, user_id=42)
                reviewed = report("independent_review") | bind | {
                    "evidence_sha256s": [observed["receipt_sha256"]],
                    "answers_sha256": publication["answers_sha256"]}
                self_review = await evidence.attach(
                    db, first.id, expected_context_sha256=contract.digest(context),
                    kind="independent_review", report=reviewed, user_id=42)
                approval = await evidence.attach(
                    db, first.id, expected_context_sha256=contract.digest(context),
                    kind="independent_review", report=reviewed | {"reviewer": "second reviewer"}, user_id=43)
                fields = ("source_sha256", "inventory_sha256", "inputs_sha256", "device", "provider",
                          "memory_limit_bytes", "threads", "deadline_s", "total")
                options = dict(publication, lease=lease, policy=policy, trusted_reviewer_ids={42, 43},
                               runtime_plan={("primary", "full"): {field: runtime[field] for field in fields}})
                with pytest.raises(ValueError, match="publication_domain_undeclared"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **options, approval_sha256=approval["receipt_sha256"])
                # Without a curated declaration the tag is reported, never given gate_tags=[].
                assert await trials.derive_candidate_policies(db) == ({}, ["fixture"])
                await db.execute(text("""
                    INSERT INTO tagger_tag_rule
                        (producer, tag_id, tag, threshold, remove_below, may_add, may_remove, condition, evidence)
                    VALUES ('wd14', 1, 'fixture', .5, .4, true, false, 'always', 'fixture review')
                """))
                await db.commit()
                assert await trials.derive_candidate_policies(db) == ({"fixture": {
                    "apply_mode": "replace", "operating_point": "dedicated@0.5", "gate_tags": [],
                }}, [])
                assert await trials.derive_candidate_policies(db, exclude={"fixture"}) == ({}, [])
                derived_declaration = await trials.generate_trial_declaration(
                    sessions, settings | {"candidate_policies": {}, "derive_policies": True},
                    state_root=tmp_path / "derived")
                assert derived_declaration["candidate_policies"] == {"fixture": {
                    "apply_mode": "replace", "operating_point": "dedicated@0.5", "gate_tags": [],
                }}
                assert (len(derived_declaration["trials"]), derived_declaration["deferred_by_cap"],
                        derived_declaration["undeclared_domain"]) == (1, [], [])
                this_tree = trials.deployed_source(mainline="HEAD")
                monkeypatch.setattr(trials, "deployed_source", lambda: this_tree)
                deployed = await trials.generate_trial_declaration(
                    sessions, settings | {"source": "deployed"}, state_root=tmp_path / "deployed")
                assert trials.protocol.read_verified(deployed["trials"][0]["plan_path"])[
                    "source_sha256"] == this_tree[1]
                with pytest.raises(ValueError, match="publication_domain_changed"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **(options | {"policy": policy | {"gate_tags": ["other"]}}),
                        approval_sha256=approval["receipt_sha256"])
                inventory.append({"tag": "other", "content_sha256": "a" * 64,
                                  "kind": "content_head_bundle", "category": "general",
                                  "patch_version": 1, "threshold": .5, "apply_mode": "replace",
                                  "gate_tags": [], "operating_point": "fixture"})
                from api.services import policy_artifacts

                async def available_artifacts(_store, hashes):
                    return dict.fromkeys(hashes, True)

                monkeypatch.setattr(policy_artifacts, "policy_artifact_availability", available_artifacts)
                with pytest.raises(ValueError, match="publication_runtime_inventory_changed"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **options, approval_sha256=approval["receipt_sha256"])
                inventory.clear()
                with pytest.raises(ValueError, match="publication_runtime_self_review"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **(options | {"trusted_reviewer_ids": {42}}),
                        approval_sha256=self_review["receipt_sha256"])
                await db.commit()
                await evidence.attach(
                    db, first.id, expected_context_sha256=contract.digest(context),
                    kind="independent_review",
                    report=reviewed | {"reviewer": "second reviewer", "verdict": "revise",
                                       "findings": ["Revisit current runtime evidence."]},
                    user_id=43)
                await db.commit()
                with pytest.raises(ValueError, match="publication_independent_review_required"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **options, approval_sha256=approval["receipt_sha256"])
                assert (await db.execute(text("SELECT count(*) FROM tagger_patch"))).scalar_one() == 0
                await db.rollback()
                stale = await evidence.attach(
                    db, first.id, expected_context_sha256=contract.digest(context),
                    kind="independent_review",
                    report=reviewed | {"reviewer": "second reviewer", "answers_sha256": "0" * 64},
                    user_id=43)
                await db.commit()
                with pytest.raises(ValueError, match="publication_independent_review_mismatch"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **options, approval_sha256=stale["receipt_sha256"])
                await db.rollback()
                approval = await evidence.attach(
                    db, first.id, expected_context_sha256=contract.digest(context),
                    kind="independent_review",
                    report=reviewed | {"reviewer": "second reviewer", "findings": ["Rechecked."]},
                    user_id=43)
                await db.commit()
                with pytest.raises(ValueError, match="publication_measurement_mismatch"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **(options | {"policy": policy | {"measured_fpr": .01}}),
                        approval_sha256=approval["receipt_sha256"])
                patch = await publisher.publish_reviewed_candidate(
                    db, first.id, **options, approval_sha256=approval["receipt_sha256"])
                assert await publisher.publish_reviewed_candidate(
                    db, first.id, **options, approval_sha256=approval["receipt_sha256"]) == patch
                with pytest.raises(ValueError, match="publication_existing_result_mismatch"):
                    await publisher.publish_reviewed_candidate(
                        db, first.id, **(options | {"policy": policy | {"threshold": .6}}),
                        approval_sha256=approval["receipt_sha256"])
                assert (await db.execute(text("SELECT count(*) FROM tagger_patch"))).scalar_one() == 1
                assert (await db.execute(text(
                    "SELECT promoted_patch_id FROM tagger_registration_request WHERE id=:id"
                ), {"id": first.id})).scalar_one() == patch
                assert (await db.execute(text(
                    "SELECT content_sha256 FROM tagger_current_head_policy WHERE tag='fixture'"
                ))).scalar_one() == "c" * 64
                await db.rollback()
                assert (await db.execute(text("SELECT count(*) FROM tagger_patch"))).scalar_one() == 0
                from api.tagger_core.transitions import release_lease
                await release_lease(db, lease)
                entry = dict(publication, registration_id=first.id, policy=policy,
                             approval_sha256=approval["receipt_sha256"],
                             workers=[{"host": "primary", "profile": "full",
                                       "runtime": options["runtime_plan"][("primary", "full")]}])
                scheduled = {"schema": "tagger-publication-plan/1", "trusted_reviewer_ids": [43],
                             "candidates": [entry]}
                # The automatic verifier (a dedicated account, never the trial
                # submitter) attests once per evidence set from machine checks only.
                verifier = {"trusted_reviewer_ids": [44], "verifier_user_id": 44,
                            "policies": {"fixture": policy}}
                for wrong in ({"verifier_user_id": 42, "trusted_reviewer_ids": [42]},
                              {"trusted_reviewer_ids": [43]}):
                    assert await publisher.attest_qualified_candidates(
                        sessions, verifier | wrong, trial_declaration=declaration) == [
                        {"state": "verifier_misconfigured"}]
                # A trusted reviewer's latest "revise" is never overridden.
                await evidence.attach(
                    db, first.id, expected_context_sha256=contract.digest(context),
                    kind="independent_review",
                    report=reviewed | {"reviewer": "careful human", "verdict": "revise",
                                       "findings": ["Look again."]},
                    user_id=45)
                await db.commit()
                assert await publisher.attest_qualified_candidates(
                    sessions, verifier | {"trusted_reviewer_ids": [44, 45]},
                    trial_declaration=declaration) == [
                    {"registration_id": first.id, "state": "held",
                     "reason": "trusted_reviewer_revise"}]
                # A tag without a serving policy is refused.
                assert (await publisher.attest_qualified_candidates(
                    sessions, verifier | {"policies": {}},
                    trial_declaration=declaration | {"candidate_policies": {}}))[0]["reason"] == (
                    "no_candidate_policy")
                # Contention while writing the receipt stays with this registration.
                from sqlalchemy.exc import OperationalError
                intake = evidence.attach

                async def busy(*args, **kwargs):
                    raise OperationalError("lock_timeout", {}, Exception("canceling statement"))

                monkeypatch.setattr(evidence, "attach", busy)
                failed = await publisher.attest_qualified_candidates(
                    sessions, verifier, trial_declaration=declaration)
                assert [(r["state"], r["error"]) for r in failed] == [("error", "OperationalError")]
                monkeypatch.setattr(evidence, "attach", intake)
                attested = await publisher.attest_qualified_candidates(
                    sessions, verifier, trial_declaration=declaration)
                assert [r["state"] for r in attested] == ["attested"]
                assert await publisher.attest_qualified_candidates(
                    sessions, verifier, trial_declaration=declaration) == [
                    {"registration_id": first.id, "state": "already_attested"}]
                automatic = await publisher.generate_publication_plan(
                    sessions, verifier, trial_declaration=declaration)
                assert automatic == scheduled | {"trusted_reviewer_ids": [44], "candidates": [
                    entry | {"approval_sha256": attested[0]["receipt_sha256"]}]}
                generated = await publisher.generate_publication_plan(
                    sessions, {"trusted_reviewer_ids": [43], "policies": {"fixture": policy}},
                    trial_declaration=declaration)
                assert generated == scheduled
                assert declaration["candidate_policies"] == settings["candidate_policies"]
                assert await publisher.generate_publication_plan(
                    sessions, {"trusted_reviewer_ids": [43]}, trial_declaration=declaration) == scheduled
                completed =await publisher.run_publication_plan(sessions, scheduled)
                assert completed[0]["state"] == "published"
                assert await publisher.run_publication_plan(sessions, scheduled) == completed
                assert await publisher.attest_qualified_candidates(
                    sessions, verifier, trial_declaration=declaration) == [
                    {"registration_id": first.id, "state": "not_current"}]
                published_metrics = (await db.execute(text(
                    "SELECT measured_recall, measured_fpr FROM tagger_current_head_policy WHERE tag='fixture'"
                ))).one()
                assert tuple(published_metrics) == (1.0, 0.0)
                assert (await db.execute(text("SELECT count(*) FROM tagger_patch"))).scalar_one() == 1
                from api.schemas.tagging import ExecutedContentHead
                from api.services.execution_receipts import published_candidate_coverage, remember
                from api.services.tagging import complete_auto_tag
                from tagger.execution_receipt import make
                from tests.test_execution_receipt import body as worker_body

                before = await published_candidate_coverage(db)
                assert before["fixture"]["complete"] is False
                packet = make(worker_body() | {"host": "primary", "profile": "full",
                                                "release_id": release})
                receipt = await remember(db, packet, host="primary", profile="full", release_id=release)
                await db.execute(text("""
                    SELECT tagger_upsert_heartbeat(
                        'fixture-worker','primary','full',6,:release,'cpu',NULL,:receipt)
                """), {"receipt": receipt, "release": release})
                assert (await published_candidate_coverage(db))["fixture"]["complete"] is False
                completion = dict( status="ok", tags=[], error=None, source="fixture",
                    host="primary", release_id=release, worker_id="fixture-worker",
                    execution_receipt_sha256=receipt,
                    executed_content_heads=[ExecutedContentHead(
                        tag="fixture", artifact_sha256="c" * 64, threshold=.5, apply_mode="replace")],
                )
                await complete_auto_tag(db, post_id=101, **completion)
                async with sessions() as observer:
                    assert (await published_candidate_coverage(observer))["fixture"] == {
                        "complete": True,
                        "workers": [{"host": "primary", "profile": "full", "confirmed": True}],
                    }
                    from api.services import lifecycle_health
                    import html as _html
                    before = (await observer.execute(text("SHOW statement_timeout"))).scalar_one()
                    report = await lifecycle_health.snapshot(observer)
                    assert (await observer.execute(text("SHOW statement_timeout"))).scalar_one() == before
                    assert report["pending"] == {"candidates": [], "hidden": 0}
                    assert report["published_heads"] == [{"tag": "fixture", "complete": True, "workers": [
                        {"host": "primary", "profile": "full", "confirmed": True, "reason": None,
                         "expected_provider": None, "provider": "cpu", "release_id": release,
                         "reported_at": report["published_heads"][0]["workers"][0]["reported_at"]}]}]
                    assert report["last_publication"]["last_30d"] == 1
                    page = lifecycle_health.render(report, lambda x: _html.escape(str(x)))
                    assert "すべての公開 head が全ワーカーで実行を確認済み" in page
                plan = {"schema": "tagger-publication-plan/1", "trusted_reviewer_ids": [43],
                        "candidates": [{"registration_id": first.id + 1000}]}
                assert await publisher.run_publication_plan(sessions, plan) == [
                    {"registration_id": first.id + 1000, "state": "missing"}]
                with pytest.raises(ValueError, match="publication_plan_invalid"):
                    await publisher.run_publication_plan(sessions, plan | {"trusted_reviewer_ids": []})
                await db.execute(text("""
                    SELECT tagger_governed_upsert_patch(
                      'fixture',2,'approved',:sha,'identical republish',NULL,'{}'::jsonb,
                      '{}'::jsonb,'content_head',NULL,NULL,NULL,current_date,'fixture',NULL)
                """), {"sha": "c" * 64})
                await db.execute(text("""
                    SELECT tagger_governed_upsert_head_policy(
                      'fixture',2,:sha,'fixture',.5,1,0,'replace',ARRAY[]::text[],:registration)
                """), {"sha": "c" * 64, "registration": first.id})
                await db.commit()
                assert (await published_candidate_coverage(db))["fixture"]["complete"] is False
                await complete_auto_tag(db, post_id=102, **completion)
                assert (await published_candidate_coverage(db))["fixture"]["complete"] is True
                assert (await db.execute(text(
                    "SELECT count(DISTINCT policy_epoch) FROM tagger_content_head_execution"
                ))).scalar_one() == 2
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_content_head_coverage_requires_current_completed_inference(url):
    from datetime import datetime, timezone
    from api.services.execution_receipts import content_head_coverage

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, *(importlib.import_module("api.alembic.versions." + name)
                for name in ("0192_tagger_execution_receipt", "0204_direct_candidate_review",
                             "0205_candidate_evaluation_history", "0206_content_head_execution")))
            async with sessions() as db:
                args = dict(release_id=14, tag="fixture", artifact_sha256="a" * 64,
                            threshold=.5, apply_mode="replace",
                            published_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
                await db.execute(text("DELETE FROM tagger_expected_worker"))
                assert not (await content_head_coverage(db, **args))["complete"]
                await db.execute(text("""
                    INSERT INTO tagger_expected_worker(host,description,profile,opportunistic)
                    VALUES ('primary','required','full',false), ('nodeb','optional','wd14',true)
                """))
                from tests.test_execution_receipt import body
                from tagger.execution_receipt import make
                from api.services.execution_receipts import remember
                release = (await db.execute(text("""
                    SELECT tagger_governed_publish_release(
                      'fixture','fixture','fixture','fixture','fixture',
                      decode('00','hex'),decode('00','hex'),1,1,'{}'::jsonb,NULL,'fixture')
                """))).scalar_one()
                args["release_id"] = release
                packet = make(body() | {"host": "primary", "profile": "full", "release_id": release})
                receipt = await remember(db, packet, host="primary", profile="full", release_id=release)
                await db.execute(text("""
                    SELECT tagger_upsert_heartbeat(
                        'fixture-worker','primary','full',6,:release,'cpu',NULL,:receipt)
                """), {"receipt": receipt, "release": release})
                assert not (await content_head_coverage(db, **args))["complete"]
                from api.schemas.tagging import ExecutedContentHead
                from api.services.tagging import complete_auto_tag
                completion = dict(
                    post_id=101, status="ok", tags=[], error=None, source="fixture",
                    host="primary", release_id=release, worker_id="fixture-worker",
                    execution_receipt_sha256=receipt,
                    executed_content_heads=[ExecutedContentHead(
                        tag="fixture", artifact_sha256=args["artifact_sha256"],
                        threshold=.5, apply_mode="replace")],
                    commit=False,
                )
                savepoint = await db.begin_nested()
                assert (await complete_auto_tag(db, **completion))[0] == "done"
                assert (await content_head_coverage(db, **args))["complete"]
                await savepoint.rollback()
                assert not (await content_head_coverage(db, **args))["complete"]
                assert (await complete_auto_tag(db, **completion))[0] == "done"
                assert await content_head_coverage(db, **args) == {
                    "complete": True,
                    "workers": [{"host": "primary", "profile": "full", "confirmed": True}],
                }
                for change in ({"release_id": 15}, {"threshold": .6}, {"apply_mode": "add_only"},
                               {"artifact_sha256": "c" * 64},
                               {"published_at": datetime(2100, 1, 1, tzinfo=timezone.utc)}):
                    assert not (await content_head_coverage(db, **(args | change)))["complete"]
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_vlm_publication_follows_registry_activation(url, tmp_path, monkeypatch):
    from api.db import session
    from scripts.vlm_tag_proposals import plan_or_publish
    from tagger.review_set_import import run

    async def go():
        engine, sessions = await setup(url)
        monkeypatch.setattr(session, "async_session", sessions)
        directory = tmp_path / "queues"
        args = SimpleNamespace(queue_dir=directory, receipt_dir=tmp_path / "receipts",
                               model_sha256="a" * 64, publish=True)
        rows = [{"post": 101, "tag": queue()["tag"], "action": "add"}]
        try:
            await migrate(engine)
            await plan_or_publish(args, b"before activation", rows)
            legacy, = directory.iterdir()
            async with sessions() as db:
                assert not await registry.available(db)
            assert (await run(directory, True))["inserted"] == 1
            await plan_or_publish(args, b"after activation", rows)
            await plan_or_publish(args, b"after activation", rows)
            assert list(directory.iterdir()) == [legacy]
            async with sessions() as db:
                assert len(await registry.load_configured(db)) == 2
                assert len(await registry.blobs(db)) == 2
            assert len(list(args.receipt_dir.iterdir())) == 2
        finally:
            await engine.dispose()

    asyncio.run(go())


@pytest.mark.parametrize("purpose",["fixed_evaluation","boundary_recheck"])
def test_sql_boundary_accepts_a_frozen_successor_for_any_request(url,purpose):
    async def go():
        engine,ss=await setup(url)
        try:
            await migrate(engine,*(importlib.import_module("api.alembic.versions."+name)
                                   for name in ("0204_direct_candidate_review","0207_review_set_any_purpose")))
            ref,blobs=document();q=json.loads(next(iter(blobs.values())))
            q.update(purpose=purpose,model_sha256="d"*64)
            old={k:v for k,v in q.items() if k not in ("media_sha256","source_provenance")}
            old["schema"]="human-review-set/1"
            raw=json.dumps(old).encode();oldref=hashlib.sha256(raw).hexdigest()
            q["source_provenance"]=dict(kind="frozen_review_set",artifact_sha256="c"*64,supersedes_queue_sha256=oldref)
            newraw=json.dumps(q).encode();newref=hashlib.sha256(newraw).hexdigest()
            async with ss() as db:
                await registry.import_sets(db,{oldref+".json":raw,newref+".json":newraw});await db.commit()
                rows=await registry.load(db)
                assert next(row for row in rows if row["queue_sha256"]==oldref)["superseded_by"]==newref
                # a second request whose successor claims another purpose's kind is refused
                other=dict(old,frame_sha256="f"*64);oraw=json.dumps(other).encode();oref=hashlib.sha256(oraw).hexdigest()
                await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":oraw})
                q.update(frame_sha256="f"*64,source_provenance=dict(kind="frozen_candidate_sample",artifact_sha256="e"*64,supersedes_queue_sha256=oref))
                with pytest.raises(Exception,match="review_provenance_invalid"):
                    await db.execute(text("SELECT tagger_register_review_set(:raw)"),{"raw":json.dumps(q).encode()})
                await db.rollback()
        finally:await engine.dispose()
    asyncio.run(go())


def test_last_answer_triggers_refill_after_commit(url, monkeypatch):
    from uuid import uuid4
    from api.db import session
    from tagger import review_refill

    monkeypatch.setenv("KURA_REVIEW_REFILL_ON_COMPLETION", "1")

    async def go():
        engine, sessions = await setup(url)
        calls = []
        try:
            await migrate(engine)
            monkeypatch.setattr(session, "async_session", sessions)
            ref, incoming = document()
            next_document = queue()
            next_document.pop("queue_sha256")
            next_document["frame_sha256"] = "f" * 64
            raw = json.dumps(next_document, sort_keys=True).encode()
            next_ref = hashlib.sha256(raw).hexdigest()
            async with sessions() as db:
                await registry.import_sets(db, incoming)
                await db.commit()

                async def prepare(refill_db):
                    assert refill_db is not db
                    async with sessions() as observer:
                        count = (await observer.execute(text(
                            "SELECT count(*) FROM tagger_human_review_event WHERE queue_sha256=:ref"
                        ), {"ref": ref})).scalar_one()
                    calls.append(count)
                    return {next_ref + ".json": raw}, [{"reference": next_ref}]

                monkeypatch.setattr(review_refill, "prepare", prepare)
                for post in (101, 102):
                    q = await human_review.resolve_queue(db, ref)
                    ctx = await human_review.context(db, q, post)
                    body = routes.Judgment(request_id=uuid4(),
                        expected_context_sha256=ctx["context_sha256"], decision="uncertain")
                    await routes.judge(ref, post, body, db, USER)
                    assert calls == ([2] if post == 102 else [])
                await routes.judge(ref, 102, body, db, USER)
                assert calls == [2]
                assert {q["queue_sha256"] for q in await registry.load(db)} == {ref, next_ref}
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_completed_explicit_set_frees_capacity_without_deleting_history(url):
    from uuid import uuid4

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, *(importlib.import_module(name) for name in (
                "api.alembic.versions.0204_direct_candidate_review",
                "api.alembic.versions.0207_review_set_any_purpose",
                "api.alembic.versions.0208_active_review_capacity",
            )))
            raw = []
            for i in range(21):
                row = queue()
                row.pop("queue_sha256")
                row["frame_sha256"] = f"{i:064x}"
                raw.append(json.dumps(row, sort_keys=True).encode())
            def incoming(items):
                return {hashlib.sha256(item).hexdigest()+".json": item for item in items}
            async with sessions() as db:
                await registry.import_sets(db, incoming(raw[:20]))
                await db.commit()
                ref = hashlib.sha256(raw[0]).hexdigest()
                q = await human_review.resolve_queue(db, ref)
                for post in q["post_ids"]:
                    ctx = await human_review.context(db, q, post)
                    await routes.judge(ref, post, routes.Judgment(
                        request_id=uuid4(), expected_context_sha256=ctx["context_sha256"],
                        decision="uncertain"), db, USER)
                assert (await registry.import_sets(db, incoming(raw[20:])))["inserted"] == 1
                await db.commit()
                assert len(await registry.load(db)) == 21
                assert (await human_review.resolve_queue(db, ref))["post_ids"] == q["post_ids"]
        finally:
            await engine.dispose()

    asyncio.run(go())

def test_third_reviewer_resolves_disagreement_without_changing_labels(url, monkeypatch):
    from uuid import uuid4

    from api.services import review_adjudication as adjudication
    from test_human_review_live import snapshot
    from tagger import review_refill

    monkeypatch.setattr(review_refill, "TARGET_TAGS", 1)

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, importlib.import_module(
                "api.alembic.versions.0204_direct_candidate_review"))
            async with sessions() as db:
                await registry.import_sets(db, document()[1])
                media = queue()["media_sha256"]["101"]
                request = {
                    "schema": "human-review-set/3", "tag": "fixture",
                    "purpose": "candidate_evaluation", "frame_sha256": "b" * 64,
                    "model_sha256": "c" * 64, "post_ids": [101, 102],
                    "media_sha256": queue()["media_sha256"], "source_provenance": {
                        "kind": "dedicated_candidate", "measurement_sha256": "d" * 64,
                        "snapshot_sha256": "e" * 64,
                    },
                }
                ref = await registry.register_candidate_set(db, request)
                original = dict(request, queue_sha256=ref)
                before = await snapshot(db)

                async def vote(q, actor, decision):
                    observed = await human_review.context(db, q, 101)
                    return await human_review.submit(db, q, 101, {
                        "request_id": str(uuid4()), "expected_context_sha256": observed["context_sha256"],
                        "decision": decision,
                    }, SimpleNamespace(user_id=actor, name=f"reviewer-{actor}"))

                await vote(original, 42, "present")
                await vote(original, 43, "absent")
                assert len(await adjudication.disagreements(db)) == 1
                assert (await registry.evaluation_answers(db, ref))["answers"][0]["decision"] is None

                third = adjudication.request("fixture", 101, media)
                raw = json.dumps(third, sort_keys=True, separators=(",", ":")).encode()
                third_ref = hashlib.sha256(raw).hexdigest()
                original_media = (await db.execute(text("SELECT data FROM post WHERE id=101"))).scalar_one()
                await db.execute(text("UPDATE post SET data=:data WHERE id=101"), {"data": b"changed rendition"})
                assert await review_refill.prepare(db) == ({}, [])
                await db.execute(text("UPDATE post SET data=:data WHERE id=101"), {"data": original_media})
                incoming, suggestions = await review_refill.prepare(db)
                assert incoming == {third_ref + ".json": raw}
                assert suggestions[0]["reference"] == third_ref
                await registry.import_sets(db, incoming)
                third["queue_sha256"] = third_ref
                await db.execute(text("UPDATE post SET data=:data WHERE id=101"), {"data": b"changed rendition"})
                assert (await routes.review_set(third_ref, db, USER))["remaining"] == 0
                await db.execute(text("UPDATE post SET data=:data WHERE id=101"), {"data": original_media})
                with pytest.raises(KuraError, match="human_review_independent_reviewer_required"):
                    await vote(third, 42, "present")
                await vote(third, 44, "uncertain")
                page = await routes.review_set(third_ref, db, USER)
                assert page["adjudication"] and page["remaining"] == 1
                assert page["counts"]["uncertain"] == 1
                assert (await registry.evaluation_answers(db, ref))["answers"][0]["decision"] is None
                await vote(original, 43, "present")
                assert (await routes.review_set(third_ref, db, USER))["remaining"] == 0
                with pytest.raises(KuraError, match="human_review_disagreement_resolved"):
                    await vote(third, 45, "present")
                await vote(original, 43, "absent")
                assert (await routes.review_set(third_ref, db, USER))["remaining"] == 1
                receipt = await vote(third, 45, "present")
                assert (await routes.review_set(third_ref, db, USER))["remaining"] == 0
                with pytest.raises(KuraError, match="human_review_independent_reviewer_required"):
                    await vote(original, 45, "absent")
                answer = (await registry.evaluation_answers(db, ref))["answers"][0]
                assert answer["decision"] == "present"
                assert answer["adjudication_event_id"] == receipt["event_id"]
                assert await snapshot(db) == before
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_stale_adjudications_do_not_consume_ordinary_review_capacity(url):
    from api.services.review_adjudication import request

    def blobs(documents):
        raw = (json.dumps(doc, sort_keys=True, separators=(",", ":")).encode() for doc in documents)
        return {hashlib.sha256(item).hexdigest() + ".json": item for item in raw}

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, *(importlib.import_module(
                f"api.alembic.versions.{name}"
            ) for name in (
                "0204_direct_candidate_review", "0207_review_set_any_purpose",
                "0208_active_review_capacity", "0209_adjudication_capacity",
            )))
            adjudications = [request("fixture", 101, f"{i:064x}") for i in range(21)]
            ordinary = queue()
            ordinary.pop("queue_sha256")
            async with sessions() as db:
                assert (await registry.import_sets(db, blobs(adjudications[:20])))["inserted"] == 20
                assert (await registry.import_sets(db, blobs([ordinary])))["inserted"] == 1
                ordinary_batches = [dict(ordinary, frame_sha256=f"{i:064x}") for i in range(1, 20)]
                assert (await registry.import_sets(db, blobs(ordinary_batches)))["inserted"] == 19
                assert (await registry.import_sets(db, blobs(adjudications[20:])))["inserted"] == 1
                unicode_request = request("日本語", 101, "a" * 64)
                assert (await db.execute(text("SELECT public.tagger_is_adjudication(CAST(:doc AS jsonb))"),
                                         {"doc": json.dumps(unicode_request)})).scalar_one()
                await db.commit()
                assert len(await registry.load(db)) == 41
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_refill_searches_past_recent_images_with_existing_judgments(url):
    from tagger.review_refill import EXAMPLES

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine)
            async with sessions() as db:
                await db.execute(text("""
                    INSERT INTO post(id,creation_time,safety,type,checksum,"mime-type",
                                     version,status,data,media_location)
                    SELECT i,now(),'safe','image',i::text,'image/png',1,'ok',
                           decode('010203','hex'),'pg'
                    FROM generate_series(200,600) i
                """))
                await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
                await db.execute(text("""
                    INSERT INTO post_tag(post_id,tag_id)
                    SELECT i,1 FROM generate_series(200,600) i
                """))
                await db.execute(text("""
                    INSERT INTO post_tag_pin(post_id,tag_id,state,pinned_by)
                    SELECT i,1,'present','reviewer' FROM generate_series(201,600) i
                """))
                rows = (await db.execute(text(EXAMPLES), {"tag_id": 1, "limit": 24})).mappings().all()
                assert [row["post_id"] for row in rows] == [200]
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_refill_finds_unjudged_images_in_same_series_without_related_tags(url):
    from tagger.review_refill import EXAMPLES

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine)
            async with sessions() as db:
                await db.execute(text("""
                    INSERT INTO tag(id,category_id,creation_time,version)
                    SELECT 2,id,now(),1 FROM tag_category WHERE name='character'
                    UNION ALL
                    SELECT 3,id,now(),1 FROM tag_category WHERE name='copyright'
                """))
                await db.execute(text("""
                    INSERT INTO post(id,creation_time,safety,type,checksum,"mime-type",
                                     version,status,data,media_location)
                    VALUES (103,now(),'safe','image','fixture103','image/png',1,'ok',
                            decode('010203','hex'),'pg')
                """))
                await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
                await db.execute(text("""
                    INSERT INTO post_tag(post_id,tag_id) VALUES (101,2),(101,3),(102,3),(103,3)
                """))
                await db.execute(text("""
                    INSERT INTO post_tag_pin(post_id,tag_id,state,pinned_by)
                    VALUES (103,2,'absent','reviewer')
                """))
                rows = (await db.execute(text(EXAMPLES), {"tag_id": 2, "limit": 24})).mappings()
                assert [(row["post_id"], row["tagged"]) for row in rows] == [(102, False), (101, True)]
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_group_refill_picks_one_image_and_skips_reviewed_groups(url):
    from tagger.review_refill import GROUP_EXAMPLES

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine)
            async with sessions() as db:
                await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
                await db.execute(text("""
                    INSERT INTO tag(id,category_id,creation_time,version)
                    SELECT 2,category_id,now(),1 FROM tag WHERE id=1
                """))
                await db.execute(text("INSERT INTO post_tag(post_id,tag_id) VALUES (101,2),(102,2)"))
                params = {"tag_id": 2, "posts": [101, 102], "groups": [0, 0], "limit": 24}
                result = await db.execute(text(GROUP_EXAMPLES), params)
                assert [row["post_id"] for row in result.mappings()] == [102]
                await db.execute(text("""
                    INSERT INTO post_tag_pin(post_id,tag_id,state,pinned_by)
                    VALUES (101,2,'present','reviewer')
                """))
                result = await db.execute(text(GROUP_EXAMPLES), params)
                assert result.mappings().all() == []
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_reselection_preserves_existing_answers_and_members(url, monkeypatch):
    from uuid import uuid4
    from api.routers import pools
    from api.services import review_reselection
    from tagger import review_refill

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, *(importlib.import_module(
                f"api.alembic.versions.{name}"
            ) for name in (
                "0204_direct_candidate_review", "0207_review_set_any_purpose",
                "0208_active_review_capacity", "0209_adjudication_capacity",
                "0210_review_reselection",
            )))
            old, old_docs = document()
            row = queue()
            row.pop("queue_sha256")
            row["frame_sha256"] = "f" * 64
            raw = json.dumps(row, sort_keys=True).encode()
            new = hashlib.sha256(raw).hexdigest()

            have_replacement = False

            async def prepare(db, *, target_tags, ordinary_limit):
                assert old in await review_reselection.retired_refs(db)
                assert ordinary_limit == 1
                return ({new + ".json": raw}, [{"reference": new}]) if have_replacement else ({}, [])

            monkeypatch.setattr(review_refill, "prepare", prepare)
            async with sessions() as db:
                await registry.import_sets(db, old_docs)
                await db.commit()
                context = await routes.review_image(old, 101, db, USER)
                first_body = routes.Judgment(request_id=uuid4(),
                    expected_context_sha256=context["context_sha256"], decision="uncertain")
                first = await routes.judge(old, 101, first_body, db, USER)
                body = pools.ReselectReviews(references=[old])
                with pytest.raises(KuraError, match="review_reselection_no_replacements"):
                    await pools.reselect_reviews(body, db, USER)
                async with sessions() as other:
                    assert await review_reselection.retired_refs(other) == set()
                have_replacement = True
                result = await pools.reselect_reviews(body, db, USER)
                assert (await routes.judge(old, 101, first_body, db, USER))["event_id"] == first["event_id"]
                context = await routes.review_image(old, 102, db, USER)
                with pytest.raises(KuraError, match="human_review_queue_retired"):
                    await routes.judge(old, 102, routes.Judgment(request_id=uuid4(),
                        expected_context_sha256=context["context_sha256"], decision="present"), db, USER)
                assert result["retired"] == 1
                assert {q["queue_sha256"] for q in await registry.load(db)} == {old, new}
                assert (await db.execute(text("SELECT count(*) FROM tagger_review_set_member"))).scalar_one() == 4
                assert (await db.execute(text("SELECT count(*) FROM tagger_human_review_event"))).scalar_one() == 1
                assert {q["queue_sha256"] for q in (await pools.review_queues(db, USER))["queues"]} == {new}
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_review_skips_excluded_and_deleted_images_without_fabricating_answers(url, monkeypatch):
    from api.routers import posts
    monkeypatch.setattr(posts.files, 'delete_all_names', lambda names: None)
    monkeypatch.setattr(posts.files, "names", lambda *args: [])
    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, *(importlib.import_module(
                f"api.alembic.versions.{name}"
            ) for name in (
                "0204_direct_candidate_review", "0207_review_set_any_purpose",
                "0208_active_review_capacity", "0209_adjudication_capacity",
                "0210_review_reselection", "0211_delete_post_gt",
            )))
            ref, documents = document()
            async with sessions() as db:
                template = queue()
                template.pop("queue_sha256")
                for index in range(19):
                    raw = json.dumps(dict(template, frame_sha256=f"{index:064x}")).encode()
                    documents[hashlib.sha256(raw).hexdigest() + ".json"] = raw
                assert (await registry.import_sets(db, documents))["inserted"] == 20
                await db.execute(text("""
                    INSERT INTO tag(id,category_id,creation_time,version)
                    VALUES(2,(SELECT id FROM tag_category ORDER BY id LIMIT 1),now(),1)
                """))
                await db.execute(text("""
                    INSERT INTO tag_name(tag_name_id,tag_id,name,ord)
                    VALUES(2,2,'tagger-training-exclude',0)
                """))
                await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
                await db.execute(text("INSERT INTO post_tag(post_id,tag_id) VALUES(101,2)"))
                current = await routes.review_set(ref, db, USER)
                assert current["post_ids"] == [102]
                assert current["remaining"] == 1
                from tagger.label_identity import read_label_rows
                assert [row[0] for row in await read_label_rows(db, "fixture")] == [102]
                # Age-check rule 5: a youth tag, even under an alias name, excludes
                # the post the same way (never a teacher or review image).
                await db.execute(text("""
                    INSERT INTO tag(id,category_id,creation_time,version)
                    VALUES(3,(SELECT id FROM tag_category ORDER BY id LIMIT 1),now(),1)
                """))
                await db.execute(text("""
                    INSERT INTO tag_name(tag_name_id,tag_id,name,ord)
                    VALUES(3,3,'young_looking_alias_primary',0),(4,3,'loli',1)
                """))
                await db.execute(text("INSERT INTO post_tag(post_id,tag_id) VALUES(102,3)"))
                assert (await routes.review_set(ref, db, USER))["post_ids"] == []
                assert await read_label_rows(db, "fixture") == []
                await db.execute(text("DELETE FROM post_tag WHERE post_id=102 AND tag_id=3"))
                # A post in a learning-excluded pool (the pool page's 学習除外 switch)
                # is excluded the same way.
                await db.execute(text("""
                    INSERT INTO pool_category(id,version,name,color,"default")
                    VALUES(901,1,'fixture-pool-category','default',false) ON CONFLICT DO NOTHING
                """))
                await db.execute(text("""
                    INSERT INTO pool(id,version,category_id,creation_time) VALUES(901,1,901,now())
                """))
                await db.execute(text("INSERT INTO pool_post(pool_id,post_id,ord) VALUES(901,102,0)"))
                await db.execute(text("INSERT INTO tagger_auto_exclude_pool(pool_id,excluded) VALUES(901,true)"))
                assert (await routes.review_set(ref, db, USER))["post_ids"] == []
                assert await read_label_rows(db, "fixture") == []
                await db.execute(text("UPDATE tagger_auto_exclude_pool SET excluded=false WHERE pool_id=901"))
                assert [row[0] for row in await read_label_rows(db, "fixture")] == [102]
                assert not (await db.execute(text("""
                    SELECT has_function_privilege(
                        current_user, 'public.tagger_delete_gt_before_post_delete()', 'EXECUTE')
                """))).scalar_one()
                await posts.delete_post(102, db, USER)
                assert (await db.execute(text("SELECT post_id FROM tagger_gt_label"))).scalars().all() == [101]
                current = await routes.review_set(ref, db, USER)
                assert current["post_ids"] == []
                assert current["remaining"] == 0
                assert current["decisions"] == {}
                assert (await human_review.resolve_queue(db, ref))["post_ids"] == [101,102]
                raw = json.dumps(dict(template, frame_sha256="9" * 64)).encode()
                assert (await registry.import_sets(db, {hashlib.sha256(raw).hexdigest() + ".json": raw}))["inserted"] == 1
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_base_comparison_sets_never_hide_a_publication_review(url):
    from tagger import base_comparison as bc

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, *(importlib.import_module(
                f"api.alembic.versions.{name}"
            ) for name in (
                "0204_direct_candidate_review", "0207_review_set_any_purpose",
                "0208_active_review_capacity", "0209_adjudication_capacity",
                "0210_review_reselection", "0211_delete_post_gt", "0212_base_comparison_review",
                "0213_review_origin",
            )))
            ref, documents = document()
            async with sessions() as db:
                assert (await registry.import_sets(db, documents))["inserted"] == 1
                media = {i: hashlib.sha256(b"\1\2\3").hexdigest() for i in (101, 102)}
                findings = {
                    "schema": "human-review-set/2", "tag": "fixture",
                    "purpose": "historical_label_recheck", "frame_sha256": "8" * 64,
                    "model_sha256": None, "post_ids": [101, 102],
                    "media_sha256": {str(i): h for i, h in media.items()},
                    "source_provenance": {"kind": "historical_human_training",
                                          "artifact_sha256": "9" * 64, "origin": "lora_findings"},
                }
                raw_findings = json.dumps(findings).encode()
                findings_ref = hashlib.sha256(raw_findings).hexdigest()
                assert (await registry.import_sets(db, {findings_ref + ".json": raw_findings}))["inserted"] == 1
                publication = await registry.register_candidate_set(db, {
                    "schema": "human-review-set/3", "tag": "fixture", "purpose": "candidate_evaluation",
                    "frame_sha256": "1" * 64, "model_sha256": "d" * 64, "post_ids": [101, 102],
                    "media_sha256": {str(i): h for i, h in media.items()},
                    "source_provenance": {"kind": "dedicated_candidate",
                                          "snapshot_sha256": "2" * 64, "measurement_sha256": "3" * 64}})
                frame = bc.joint_frame(
                    "fixture", {"model_sha256": "a" * 64, "threshold": .5, "scores": {101: .9, 102: .1}},
                    {"model_sha256": "b" * 64, "threshold": .5, "scores": {101: .9, 102: .8}}, seed=1)
                comparison = await bc.request_comparison_review(
                    db, frame, display_hashes=media, snapshot_sha256="4" * 64)
                await db.commit()
                listed = {q["queue_sha256"]: q["purpose"] for q in await registry.load(db)}
                assert listed[publication] == "candidate_evaluation"
                assert listed[comparison] == "base_comparison"
                for post_id, decision in ((101, "present"), (102, "absent")):
                    view = await routes.review_image(comparison, post_id, db, USER)
                    await routes.judge(comparison, post_id, routes.Judgment(
                        request_id=f"5f0e8b8e-0000-4000-8000-00000000{post_id:04d}",
                        expected_context_sha256=view["context_sha256"], decision=decision), db, USER)
                answers = await registry.evaluation_answers(db, comparison)
                assert [(a["post_id"], a["decision"]) for a in answers["answers"]] == [
                    (101, "present"), (102, "absent")]
                result = bc.compare(frame, {a["post_id"]: a["decision"] for a in answers["answers"]})
                assert result["complete"]
                # The purpose exists only for direct requests.
                raw = json.dumps(dict(json.loads(documents[ref + ".json"]), purpose="base_comparison",
                                      frame_sha256="6" * 64)).encode()
                with pytest.raises(Exception):
                    await registry.import_sets(db, {hashlib.sha256(raw).hexdigest() + ".json": raw})
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_lora_findings_and_lifecycle_have_independent_capacity(url):
    from sqlalchemy.ext.asyncio import create_async_engine

    async def go():
        engine, sessions = await setup(url)
        privileged = create_async_engine(engine.url.set(username="postgres"))
        try:
            await migrate(engine, *(importlib.import_module(
                f"api.alembic.versions.{name}"
            ) for name in (
                "0204_direct_candidate_review", "0207_review_set_any_purpose",
                "0208_active_review_capacity", "0209_adjudication_capacity",
                "0210_review_reselection", "0211_delete_post_gt", "0212_base_comparison_review",
                "0213_review_origin", "0214_review_lane_capacity",
            )))
            routine = queue()
            routine.pop("queue_sha256")
            finding = dict(routine, schema="human-review-set/2",
                           purpose="historical_label_recheck", model_sha256=None,
                           post_ids=[101], media_sha256={"101": "a" * 64},
                           source_provenance={"kind": "historical_human_training",
                                              "artifact_sha256": "b" * 64})

            def batch(template, ids):
                raw = (json.dumps(dict(template, frame_sha256=f"{i:064x}")).encode()
                       for i in ids)
                return {hashlib.sha256(doc).hexdigest() + ".json": doc for doc in raw}

            async with sessions() as db:
                legacy = batch(finding, [42])
                assert (await registry.import_sets(db, legacy))["inserted"] == 1
                await db.commit()
            async with privileged.begin() as db:
                await db.execute(text("SET LOCAL ROLE kura_reconciler"))
                await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0))"))
                await db.execute(text(
                    "INSERT INTO tagger_review_lora_origin(queue_sha256) VALUES (:ref)"
                ), {"ref": next(iter(legacy)).removesuffix(".json")})
            async with sessions() as db:
                marked = dict(finding, source_provenance={
                    **finding["source_provenance"], "origin": "lora_findings"
                })
                assert (await registry.import_sets(db, batch(marked, range(19))))["inserted"] == 19
                assert (await registry.import_sets(db, batch(routine, range(20))))["inserted"] == 20
                await db.commit()
                for template in (marked, routine):
                    with pytest.raises(Exception, match="review_registry_capacity"):
                        await registry.import_sets(db, batch(template, [55]))
                    await db.rollback()
        finally:
            await privileged.dispose()
            await engine.dispose()

    asyncio.run(go())


def test_confirmed_usable_uncensored_pair_blocks_new_non_censor_queue(url):
    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine, importlib.import_module("api.alembic.versions.0215_pool_visibility"))
            template = queue()
            template.pop("queue_sha256")
            raw = json.dumps(template).encode()
            documents = {hashlib.sha256(raw).hexdigest() + ".json": raw}
            async with sessions() as db:
                await db.execute(text("""
                    INSERT INTO tag(id,category_id,creation_time,version)
                    SELECT 2,category_id,now(),1 FROM tag WHERE id=1
                """))
                await db.execute(text("INSERT INTO tag_name(tag_name_id,tag_id,name,ord) VALUES(2,2,'censored',0)"))
                await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
                await db.execute(text("INSERT INTO post_tag(post_id,tag_id) VALUES(101,2)"))
                await db.execute(text("""INSERT INTO censor_pair(censored_post_id,uncensored_post_id,confirmed)
                                         VALUES(101,102,false)"""))
                await db.commit()

                await db.execute(text("UPDATE censor_pair SET confirmed=true WHERE censored_post_id=101"))
                current = dict(template, queue_sha256="1"*64)
                assert await human_review.reviewable_post_ids(db, current) == [102]
                with pytest.raises(Exception, match="review_prefer_uncensored_variant"):
                    await registry.import_sets(db, documents)
                censor = dict(template, tag="censored")
                allowed = json.dumps(censor).encode()
                assert (await registry.import_sets(db, {hashlib.sha256(allowed).hexdigest()+".json":allowed}))["inserted"]==1
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_review_catalog_backfills_then_tracks_latest_answer_transactionally(url):
    from api.services.dashboard_review_catalog import first_pending
    from sqlalchemy.ext.asyncio import create_async_engine

    async def go():
        engine, sessions = await setup(url)
        privileged = create_async_engine(engine.url.set(username="postgres"))
        revisions = (
            "0204_direct_candidate_review", "0207_review_set_any_purpose",
            "0208_active_review_capacity", "0209_adjudication_capacity", "0210_review_reselection",
            "0211_delete_post_gt", "0212_base_comparison_review", "0213_review_origin",
            "0214_review_lane_capacity",
        )
        try:
            await migrate(engine, *(importlib.import_module("api.alembic.versions." + n) for n in revisions))
            ref, blobs = document()
            async with sessions() as db:
                await registry.import_sets(db, blobs)
                await db.commit()
                q = await human_review.resolve_queue(db, ref)
                image = await human_review.context(db, q, 101)
                first = await human_review.submit(db, q, 101, dict(
                    request_id="6e077423-e39b-4393-a52a-0b3cc301df97",
                    expected_context_sha256=image["context_sha256"], decision="uncertain"), USER)
                await db.commit()
            migration = importlib.import_module("api.alembic.versions.0216_review_catalog")
            async with privileged.begin() as conn:
                await conn.execute(text("SET SESSION AUTHORIZATION kura"))
                def upgrade(sync):
                    migration.op = Operations(MigrationContext.configure(sync))
                    migration.upgrade()
                await conn.run_sync(upgrade)
            async with sessions() as db:
                assert await human_review.resolve_queue(db, ref) == q
                [row] = await first_pending(db)
                assert (row["total"], row["answered"], row["pending_total"]) == (2, 1, 1)
                assert await db.scalar(text("SELECT event_id FROM tagger_review_member_answer")) == first["event_id"]
                assert bytes(await db.scalar(text("SELECT document FROM tagger_review_set"))) == blobs[ref+".json"]
                image = await human_review.context(db, q, 101)
                updated = await human_review.submit(db, q, 101, dict(
                    request_id="5c3ca260-82ba-49aa-962c-48f0e93535ad",
                    expected_context_sha256=image["context_sha256"], decision="uncertain"), USER)
                assert await db.scalar(text("SELECT event_id FROM tagger_review_member_answer")) == updated["event_id"]
                await db.rollback()
                assert await db.scalar(text("SELECT event_id FROM tagger_review_member_answer")) == first["event_id"]
        finally:
            await privileged.dispose()
            await engine.dispose()
    asyncio.run(go())
