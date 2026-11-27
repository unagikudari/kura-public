import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.engine import URL
from tests.test_character_pin_private_pg import cluster
from api.services import dashboard_summary as s
from api.routers import pools
from api.errors import KuraError


async def async_value(value):
    return value


def test_summary_requires_login():
    app=FastAPI();app.add_api_route('/summary',pools.tagger_dashboard_summary)
    # Exercise the real authentication dependency; this minimal app has no
    # production exception renderer, so inspect the structured refusal itself.
    with TestClient(app) as c:
        with pytest.raises(KuraError) as refused:
            c.get('/summary')
    assert refused.value.status == 401
    assert refused.value.error == 'missing_or_invalid_authorization'


def test_snapshot_with_ordinary_role_and_no_deployment_claim(cluster,monkeypatch):
    cluster("CREATE ROLE summary_reader LOGIN;CREATE TABLE tagger_release_active(release_id int);INSERT INTO tagger_release_active VALUES(15);CREATE TABLE tag_quarantine(tag text,active boolean);INSERT INTO tag_quarantine VALUES('held_fixture',true),('inactive_fixture',false);GRANT SELECT ON tagger_release_active,tag_quarantine TO summary_reader;")
    monkeypatch.setattr(s,'read_learning',lambda db:async_value(None))
    experiment=dict(tag='smegma',host='fixture',phase='training',done=1,total=2,observed_at=1,age_seconds=1000,fresh=False,published=False)
    monkeypatch.setattr(s,'read_experiment',lambda:experiment)
    async def run():
        engine=create_async_engine(URL.create('postgresql+asyncpg',username='summary_reader',database='postgres',query={'host':cluster('SHOW unix_socket_directories').strip(),'port':'59439'}))
        try:
            async with async_sessionmaker(engine)() as db:
                d=await s.snapshot(db)
                assert d['selected_release']==15
                assert d['machine_output_holds']==['dark_labia_majora','dark_labia_minora','dark_penis','held_fixture','smegma']
                assert d['experiment']==experiment and d['learning'] is None
                assert d['deployment_verified'] is d['model_quality_verified'] is False
                for invalid in (float('nan'),float('inf'),True):
                    monkeypatch.setattr(s,'read_learning',lambda db,invalid=invalid:async_value(dict(observed_at=invalid,age_seconds=0)))
                    assert (await s.snapshot(db))['learning'] is None
                learning=dict(tag='fixture',label='評価履歴を登録済み',state='registered',fresh=False,observed_at=1,age_seconds=1000)
                monkeypatch.setattr(s,'read_learning',lambda db:async_value(learning))
                assert (await s.snapshot(db))['learning']==learning
            cluster('DELETE FROM tagger_release_active')
            async with async_sessionmaker(engine)() as db:assert (await s.snapshot(db))['selected_release'] is None
            cluster('DROP TABLE tag_quarantine')
            async with async_sessionmaker(engine)() as db:
                with pytest.raises(Exception):await s.snapshot(db)
        finally:await engine.dispose()
    asyncio.run(run())
