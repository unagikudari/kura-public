"""Artist references are selected by stable IDs, with bounded SQL parameters."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from api.routers.tags import discover_tag_links


def call(db, ids, exclude=None):
    return discover_tag_links(platform=None, uncensored=None, verified=None,
                              exclude_category=exclude, tag_ids=ids, limit=1000, db=db)


def test_ids_and_category_are_bound_and_not_name_matched():
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))))
    assert asyncio.run(call(db, '9,2,9', 'artist')) == []
    statement, params = db.execute.call_args.args
    assert params['tag_ids'] == [2,9] and params['exclude_category'] == 'artist'
    assert 'tl.tag_id = ANY(:tag_ids)' in str(statement)
    assert 'tc.name <> :exclude_category' in str(statement)


@pytest.mark.parametrize('ids', ['', '0','-1','1,x','1;DROP TABLE tag','١','2147483648',','.join(['1']*101)])
def test_invalid_identity_filters_never_query(ids):
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        asyncio.run(call(db, ids))
    assert error.value.status_code == 422
    db.execute.assert_not_awaited()
