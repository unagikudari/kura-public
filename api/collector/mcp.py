"""Internal authenticated collector MCP; UI and tools share application services.

No host paths, remote fetching, raw SQL, byte payloads, restore or delete tools.
The existing operational MCP remains a separate read-only server.
"""
from contextlib import asynccontextmanager
import json
import logging
from functools import wraps
import os
from typing import Annotated,Literal,Any
from urllib.parse import urlsplit
from uuid import UUID
from mcp.server.fastmcp import FastMCP,Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations, CallToolResult, TextContent
from pydantic import Field,ValidationError
from starlette.responses import JSONResponse

from api.db.session import async_session
from . import application,service,label_service,import_service
from .commands import CollectionInput,LabelCommand,ImportPlanInput

log=logging.getLogger(__name__)


def security():
    hosts=['127.0.0.1','localhost','[::1]','127.0.0.1:*','localhost:*','[::1]:*']
    origins=['http://127.0.0.1','http://localhost','http://[::1]','http://127.0.0.1:*','http://localhost:*','http://[::1]:*']
    for origin in os.environ.get('KURA_COLLECTOR_MCP_ORIGINS','').split(','):
        if not origin.strip():continue
        parsed=urlsplit(origin.strip())
        if (parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('','/') or parsed.query or parsed.fragment):
            raise ValueError('KURA_COLLECTOR_MCP_ORIGINS must contain HTTP(S) origins only')
        try:parsed.port
        except ValueError:raise ValueError('KURA_COLLECTOR_MCP_ORIGINS has an invalid port')
        hosts.extend([parsed.netloc,parsed.hostname]);origins.append(f'{parsed.scheme}://{parsed.netloc}')
    return TransportSecuritySettings(allowed_hosts=hosts,allowed_origins=origins)


READ=ToolAnnotations(readOnlyHint=True,openWorldHint=False)
CREATE=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False)
EDIT=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=False)
Positive=Annotated[int,Field(gt=0)]
PageSize=Annotated[int,Field(ge=1,le=100)]
Tags=Annotated[list[str],Field(max_length=16)]


class ToolFailure(Exception):
    def __init__(self,code,fix):
        self.body={'error':code,'fix':fix}
        super().__init__(code)


def failure(code,fix):return ToolFailure(code,fix)


def protected(fn):
    @wraps(fn)
    async def execute(*args,**kwargs):
        try:return await fn(*args,**kwargs)
        except ToolFailure as exc:
            return CallToolResult(isError=True,structuredContent=exc.body,
                content=[TextContent(type='text',text=json.dumps(exc.body,ensure_ascii=False))])
    return execute


@asynccontextmanager
async def invocation(ctx:Context):
    if not application.enabled():raise failure('collector_unavailable','canonicalで新ライブラリを有効にしてください。')
    try:owner=ctx.request_context.request.scope['state']['kura_owner_id']
    except (AttributeError,KeyError,TypeError,ValueError):raise failure('authentication_required','利用者トークン付きのHTTP MCP接続を使ってください。')
    if type(owner) is not int or owner<=0:raise failure('authentication_required','有効な利用者で接続してください。')
    try:
        async with async_session.begin() as db:yield db,owner
    except ToolFailure:raise
    except service.NotFound as exc:raise failure('not_found',str(exc)) from exc
    except service.Conflict as exc:raise failure('conflict',str(exc)) from exc
    except ValidationError as exc:raise failure('invalid_arguments','ツールの引数形式・件数・受付キーを確認してください。') from exc
    except ValueError as exc:raise failure('invalid_arguments',str(exc)) from exc
    except Exception as exc:
        # Do not return driver errors, query parameters or filesystem paths.
        log.warning('collector MCP service failed: %s',type(exc).__name__)
        raise failure('service_unavailable','一覧・操作状況を確認してから同じ受付キーで再試行してください。') from exc


async def collector_status(ctx:Context)->dict[str,Any]:
    """提供中の範囲と上限。旧コレクション・全体移行・任意ホストパスは対象外。"""
    async with invocation(ctx):
        return {'library':'collector-next','scope':'experimental','safe_default':True,
                'limits':{'media_page':100,'label_targets':100,'label_changes':16,'import_items':10000,'request_bytes':4*1024**2},
                'file_transfer':'authenticated-http','restore':'empty-library HTTP/CLI only','durable_export_jobs':False}


async def collector_collections(ctx:Context,after:Positive|None=None,limit:PageSize=40)->dict[str,Any]:
    """自分の作品をID降順でページ取得。next_cursorを次回afterへ渡す。"""
    async with invocation(ctx) as (db,owner):return await service.collection_page(db,owner,after,limit)


async def collector_search(ctx:Context,after:Positive|None=None,limit:PageSize=40,tags:Tags=[],kind:Literal['image','video']|None=None,safe_mode:bool=True)->dict[str,Any]:
    """自分のメディアをタグAND検索。未判定はセーフ表示で非表示。safe_mode=falseで明示解除。"""
    async with invocation(ctx) as (db,owner):return await application.list_media(db,owner,after,limit,tags,kind,safe_mode=safe_mode)


async def collector_tags(ctx:Context,after:Positive|None=None,limit:PageSize=40,safe_mode:bool=True)->dict[str,Any]:
    """自分の整理タグと保存済み件数・論理容量・代表画像を取得。表示時の再集計はしない。"""
    async with invocation(ctx) as (db,owner):return await application.tag_catalog(db,owner,after,limit,safe_mode=safe_mode)


async def collector_entries(collection:Positive,ctx:Context,after:Annotated[int,Field(ge=0)]=0,limit:PageSize=40,tags:Tags=[],kind:Literal['image','video']|None=None,safe_mode:bool=True)->dict[str,Any]:
    """自分の作品のページを作品内順で取得。同じ原本の複数位置を保持。"""
    async with invocation(ctx) as (db,owner):return await application.entries(db,owner,collection,after,limit,tags,kind,safe_mode=safe_mode)


async def collector_create_collection(command:CollectionInput,ctx:Context)->dict[str,Any]:
    """作品作成。同じrequest_keyと作品名の再送は同じ作品へ収束する。"""
    async with invocation(ctx) as (db,owner):
        return await application.create_collection(db,owner,command)


async def collector_change_labels(command:LabelCommand,ctx:Context)->dict[str,Any]:
    """整理タグの付与・解除。検索で得たasset_id/revisionと固定request_keyを使う。根拠はMCP由来として保存。"""
    async with invocation(ctx) as (db,owner):
        return await label_service.apply(db,owner,command.request_key,[(t.asset_id,t.revision) for t in command.targets],command.add,command.remove,origin='mcp')


async def collector_label_operation(request_key:UUID,ctx:Context)->dict[str,Any]:
    """UIまたはMCPで実行した自分のタグ操作の確定結果を確認。"""
    async with invocation(ctx) as (db,owner):return await label_service.operation(db,owner,request_key)


async def collector_label_history(asset:Positive,ctx:Context,limit:PageSize=30,safe_mode:bool=True)->dict[str,Any]:
    """自分の原本の整理タグ根拠とproducer版を取得。セーフ表示では非表示。"""
    async with invocation(ctx) as (db,owner):
        return {'items':await application.label_history(db,owner,asset,limit,safe_mode=safe_mode)}


async def collector_plan_import(command:ImportPlanInput,ctx:Context)->dict[str,Any]:
    """原本名・相対パス・SHA256・サイズから永続受付を計画。原本は取得しない。UI履歴から同じ受付を再開できる。"""
    async with invocation(ctx) as (db,owner):
        result=await import_service.plan(db,owner,command.request_key,command.collection,[item.model_dump() for item in command.items])
        return {**result,'upload_path':'/api/collector-next/upload','transfer':'multipart; same user Authorization; operation_key plus planned request_key'}


async def collector_imports(ctx:Context,after:UUID|None=None)->dict[str,Any]:
    """UI/MCP共通の自分の取り込み履歴（20件ずつ）。"""
    async with invocation(ctx) as (db,owner):return await import_service.recent(db,owner,after)


async def collector_import_status(request_key:UUID,ctx:Context)->dict[str,Any]:
    """UI/MCP共通の取り込み進捗。待機中の原本を完了と扱わない。"""
    async with invocation(ctx) as (db,owner):return await import_service.operation(db,owner,request_key)


async def collector_import_items(request_key:UUID,ctx:Context,after:Annotated[int,Field(ge=0)]=0,limit:Annotated[int,Field(ge=1,le=200)]=100)->dict[str,Any]:
    """取り込みの保存済み・未送信・要確認項目と受付キーを上限付きで取得。"""
    async with invocation(ctx) as (db,owner):return await import_service.items(db,owner,request_key,after,limit)


class CollectorTransport:
    """Feature gate and bounded JSON transport before the SDK allocates its body."""
    MAX_BYTES=4*1024**2
    def __init__(self,app):self._app=app;self.routes=getattr(app,'routes',[])

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self._app(scope,receive,send)
        if not application.enabled():
            return await JSONResponse({'detail':{'error':'collector_unavailable','fix':'canonicalで新ライブラリを有効にしてください。'}},status_code=503)(scope,receive,send)
        if scope['method']=='POST':
            length=dict(scope.get('headers',[])).get(b'content-length')
            if length is not None:
                try:declared=int(length)
                except ValueError:declared=-1
                if declared<0 or declared>self.MAX_BYTES:
                    return await JSONResponse({'detail':{'error':'mcp_request_too_large','fix':'1回の要求を4MiB以内に分けてください。'}},status_code=413)(scope,receive,send)
            chunks=[];size=0
            while True:
                message=await receive()
                if message['type']=='http.disconnect':return
                chunk=message.get('body',b'');size+=len(chunk)
                if size>self.MAX_BYTES:
                    return await JSONResponse({'detail':{'error':'mcp_request_too_large','fix':'1回の要求を4MiB以内に分けてください。'}},status_code=413)(scope,receive,send)
                chunks.append(chunk)
                if not message.get('more_body',False):break
            body=b''.join(chunks)
            replayed=False
            async def replay():
                nonlocal replayed
                if replayed:return await receive()
                replayed=True
                return {'type':'http.request','body':body,'more_body':False}
            return await self._app(scope,replay,send)
        return await self._app(scope,receive,send)


def create_server():
    server=FastMCP('kura-collector',instructions='新ライブラリ専用。所有者は認証から決定します。メディア検索は既定でセーフ表示です。更新には安定したUUID受付キーと最新revisionを使ってください。ファイル転送は既存の認証付きHTTP経路を使います。',
        streamable_http_path='/',stateless_http=True,json_response=True,transport_security=security())
    server.add_tool(protected(collector_status),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_collections),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_search),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_tags),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_entries),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_create_collection),annotations=CREATE,structured_output=True)
    server.add_tool(protected(collector_change_labels),annotations=EDIT,structured_output=True)
    server.add_tool(protected(collector_label_operation),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_label_history),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_plan_import),annotations=CREATE,structured_output=True)
    server.add_tool(protected(collector_imports),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_import_status),annotations=READ,structured_output=True)
    server.add_tool(protected(collector_import_items),annotations=READ,structured_output=True)
    return server


mcp=create_server()
