"""Shared access policy and use cases; adapters own transport, not data rules."""
import os
from . import service


def enabled():
    return os.environ.get('KURA_COLLECTOR_NEXT')=='1' and os.environ.get('KURA_DB_ROLE','canonical')=='canonical'


async def list_media(db,owner,after=None,limit=40,tags=None,kind=None,*,safe_mode=False):
    if safe_mode:return {'items':[],'next_cursor':None}
    return await service.list_media(db,owner,after,limit,tags,kind)


async def entries(db,owner,collection,after=0,limit=40,tags=None,kind=None,*,safe_mode=False):
    result=await service.entries(db,owner,collection,after,limit,tags,kind)
    return {'items':[],'next_cursor':None} if safe_mode else result


async def create_collection(db,owner,command):
    title=command.title.strip()
    if not title:raise ValueError('作品名を入力してください。')
    return await service.create_collection(db,owner,title,command.request_key)


async def label_history(db,owner,asset,limit=30,*,safe_mode=False):
    if safe_mode:raise service.NotFound('メディアが見つかりません。')
    from . import label_service
    return await label_service.history(db,owner,asset,limit)


async def tag_catalog(db,owner,after=None,limit=40,*,safe_mode=True):
    from . import tag_summary
    return await tag_summary.catalog(db,owner,after,limit,safe_mode=safe_mode)
