"""Server prefilter control, without HTTP or production writes.

This exercises complete_auto_tag, not the SQL trigger; a private-DB trigger
rehearsal is still required before deploying a replacement producer.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from api.services import tagging as s


@pytest.mark.parametrize('producer', ['pixai=old', 'cl_tagger_v2=new'])
def test_new_producer_cannot_reclaim_human_absent_pin(monkeypatch, producer):
    inserted = []
    audits = []
    async def execute(statement, params=None):
        sql = str(statement)
        if 'FOR NO KEY UPDATE' in sql or 'FOR UPDATE' in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: 17)
        if 'FROM post_tag_pin' in sql:
            assert params['ids'] == [41,42]
            return SimpleNamespace(fetchall=lambda: [(41,)])
        if sql.startswith('INSERT INTO post_tag '):
            compiled = statement.compile().params
            inserted.extend(v for k,v in compiled.items() if k.startswith('tag_id'))
            return SimpleNamespace(fetchall=lambda: [(42,)])
        if 'FROM tag_name' in sql:
            return SimpleNamespace(all=lambda: [(42,'other_character')])
        if 'INSERT INTO post_tag_audit' in sql:
            assert "'script'" in sql
            audits.append(params)
        return SimpleNamespace()
    db = SimpleNamespace(execute=execute, commit=AsyncMock())
    monkeypatch.setattr(s.tagging_queue, 'attempts', AsyncMock(return_value=0))
    monkeypatch.setattr(s.claim_storage, 'backend', AsyncMock(return_value='normalized'))
    replace = AsyncMock()
    monkeypatch.setattr(s.claim_storage, 'replace', replace)
    monkeypatch.setattr(s, 'get_or_create_tags', AsyncMock(return_value=[SimpleNamespace(tag_id=41),SimpleNamespace(tag_id=42)]))
    monkeypatch.setattr(s, 'expand_implications', AsyncMock())
    result = asyncio.run(s.complete_auto_tag(db,17,'ok',[
        SimpleNamespace(name='human_removed',category='character'),
        SimpleNamespace(name='other_character',category='character')],None,source=producer))
    assert result == ('done',1,0,1)
    assert inserted == [42]
    assert replace.call_args.args[-1] == [42]
    assert audits[0]['added'] == ['other_character']
    assert audits[0]['actor_id'] == producer
