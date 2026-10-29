"""Authenticated experimental adapter. Core is independent of legacy models.

Enabled explicitly on canonical only; unassessed images are hidden in safe mode.
"""
import asyncio
import os
from pathlib import Path
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from PIL import UnidentifiedImageError, Image

from api.auth import get_current_user
from api.errors import KuraError
from api.db.session import async_session, get_db
from .commands import CollectionInput, ImportEntryInput, ImportPlanInput, LabelTarget, LabelCommand
from . import application
from . import service
from . import label_service
from . import import_service
from . import restore as collection_restore
from . import exchange_upload
from . import export as collection_export
from .adapters import legacy
from .storage import MediaInputError, StorageIntegrityError, content_path, store_media


def enabled():
    if not application.enabled():
        raise HTTPException(503, "新ライブラリはこのノードで有効になっていません")


def root() -> Path:
    # Outside DATA_DIR: nginx/StaticFiles must not bypass per-owner authentication.
    return Path(os.environ.get("KURA_COLLECTOR_NEXT_ROOT", str(Path.home() / ".local/share/kura-collector-next")))


def safe(request: Request) -> bool:
    return request.cookies.get("kura_safe") == "1" or request.query_params.get("safe") == "1"


router = APIRouter(prefix="/api/collector-next", tags=["collector-next"], dependencies=[Depends(enabled)])
_slots = asyncio.Semaphore(2)
_export_slots = asyncio.Semaphore(1)
_restore_slots = asyncio.Semaphore(1)


@router.get("/tags")
async def tag_catalog(request: Request, after: int | None = Query(None,gt=0),
                      limit: int = Query(40,ge=1,le=100), user=Depends(get_current_user), db=Depends(get_db)):
    return await application.tag_catalog(db,user.user_id,after,limit,safe_mode=safe(request))


@router.get("/status")
async def status(request: Request, user=Depends(get_current_user)):
    return {"phase": "originals-collections-labels", "safe_mode": safe(request),
            "max_file_mib": 64, "max_import_files":10000, "import_resume":"reselect-and-verify", "thumbnail_format":"image/webp", "formats": ["JPEG", "PNG", "WebP", "GIF", "WebM"]}


@router.post("/exchange/restore")
async def restore_exchange(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    await db.rollback()  # authentication must not retain a connection while uploading
    if request.headers.get('content-type','').split(';')[0].strip()!='application/zip':
        raise KuraError(415,'exchange_zip_required',fix='Kuraから書き出した作品ZIPを選んでください。')
    if _restore_slots.locked():
        raise KuraError(429,'exchange_restore_busy',fix='復元中です。完了を待って一覧を確認してください。')
    archive=None
    async with _restore_slots:
        try:
            # Reject occupied targets before receiving the body. The restore
            # service rechecks under locks immediately before metadata writes.
            async with async_session.begin() as check_db:
                await collection_restore.check_destination(check_db)
            archive=await exchange_upload.receive(request,root())
            task=asyncio.create_task(exchange_upload.execute(async_session,root(),archive,user.user_id))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Decoders cannot be interrupted. Finish or roll back before
                # closing their input or releasing the concurrency slot.
                try:await task
                finally:raise
        except exchange_upload.UploadLimit as exc:
            raise KuraError(413,'exchange_upload_too_large',fix='画面からの復元はZIP全体で512MiBまでです。') from exc
        except collection_restore.NamespaceNotEmpty as exc:
            raise KuraError(409,'exchange_target_not_empty',fix='復元には空の新ライブラリが必要です。既存データへの追加・上書きには対応していません。一覧も確認してください。') from exc
        except collection_restore.NamespaceBusy as exc:
            raise KuraError(409,'exchange_target_busy',fix='ほかの処理が実行中です。完了後に一覧を確認して再試行してください。') from exc
        except collection_restore.SchemaMismatch as exc:
            raise KuraError(409,'exchange_schema_mismatch',fix='ライブラリの版を確認してください。') from exc
        except collection_restore.RestoreError as exc:
            raise KuraError(422,'exchange_archive_invalid',fix='ZIPの形式・原本・タグ情報を検証できませんでした。書き出し元のZIPを確認してください。') from exc
        except (OSError,StorageIntegrityError) as exc:
            raise KuraError(503,'exchange_storage_unavailable',fix='保存先の空き容量と原本の状態を確認してください。') from exc
        finally:
            if archive is not None:archive.close()


@router.get("/collections")
async def collections(user=Depends(get_current_user), db=Depends(get_db)):
    return await service.collections(db, user.user_id)


@router.post("/collections", status_code=201)
async def create_collection(body: CollectionInput, user=Depends(get_current_user), db=Depends(get_db)):
    try:
        result = await application.create_collection(db,user.user_id,body)
    except service.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    await db.commit()
    return result


def import_error(exc):
    status = 404 if isinstance(exc,service.NotFound) else 409 if isinstance(exc,service.Conflict) else 422
    return KuraError(status,{404:"import_not_found",409:"import_conflict",422:"import_plan_invalid"}[status],fix=str(exc))


@router.post("/imports",status_code=201)
async def plan_import(body: ImportPlanInput,user=Depends(get_current_user),db=Depends(get_db)):
    try:
        value=await import_service.plan(db,user.user_id,body.request_key,body.collection,
            [item.model_dump() for item in body.items])
        await db.commit()
        return value
    except ValueError as exc:
        await db.rollback()
        raise import_error(exc) from exc


@router.get("/imports")
async def recent_imports(after: UUID | None = None,user=Depends(get_current_user),db=Depends(get_db)):
    try:
        return await import_service.recent(db,user.user_id,after)
    except service.NotFound as exc:
        raise import_error(exc) from exc


@router.get("/imports/{key}")
async def import_status(key: UUID,user=Depends(get_current_user),db=Depends(get_db)):
    try:
        return await import_service.operation(db,user.user_id,key)
    except service.NotFound as exc:
        raise import_error(exc) from exc


@router.get("/imports/{key}/items")
async def import_items(key: UUID,after: int=Query(0,ge=0),limit: int=Query(200,ge=1,le=200),
                       user=Depends(get_current_user),db=Depends(get_db)):
    try:
        return await import_service.items(db,user.user_id,key,after,limit)
    except service.NotFound as exc:
        raise import_error(exc) from exc


@router.get("/media")
async def media(request: Request, after: int | None = Query(None, gt=0),
                tags: list[str] = Query(default=[]), kind: Literal["image", "video"] | None = Query(None),
                limit: int = Query(40, ge=1, le=100), user=Depends(get_current_user), db=Depends(get_db)):
    try:
        return await application.list_media(db, user.user_id, after, limit, tags, kind,safe_mode=safe(request))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/collections/{collection}/entries")
async def entries(collection: int, request: Request, after: int = Query(0, ge=0),
                  tags: list[str] = Query(default=[]), kind: Literal["image", "video"] | None = Query(None),
                  limit: int = Query(40, ge=1, le=100), user=Depends(get_current_user), db=Depends(get_db)):
    try:
        return await application.entries(db,user.user_id,collection,after,limit,tags,kind,safe_mode=safe(request))
    except service.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/collections/{collection}/export")
async def export_collection(collection: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    if safe(request):
        raise KuraError(404, "collection_not_found", fix="セーフ設定と作品の選択を確認してください。")
    if _export_slots.locked():
        raise KuraError(429, "export_busy", fix="別の書き出しのダウンロード完了後に再試行してください。")
    await _export_slots.acquire()
    handed_off = False
    try:
        owner = user.user_id
        await db.rollback()  # snapshot owns its short read-only transaction
        try:
            manifest = await collection_export.snapshot(db, owner, collection)
        finally:
            await db.rollback()  # no DB transaction while hashing/copying originals
        task = asyncio.create_task(asyncio.to_thread(collection_export.build_archive, root(), manifest))
        try:
            archive, size = await asyncio.shield(task)
        except asyncio.CancelledError:
            # to_thread cannot be interrupted: retain the slot until it closes its result.
            def discarded(done):
                try:
                    if not done.cancelled() and done.exception() is None:
                        done.result()[0].close()
                finally:
                    _export_slots.release()
            task.add_done_callback(discarded)
            handed_off = True
            raise
        closed = False
        async def close():
            nonlocal closed
            if not closed:
                closed = True
                archive.close()
                _export_slots.release()
        async def body():
            try:
                while chunk := await asyncio.to_thread(archive.read, collection_export.CHUNK):
                    yield chunk
            finally:
                await close()
        from starlette.background import BackgroundTask
        response = StreamingResponse(body(), media_type="application/zip", background=BackgroundTask(close),
            headers={"Content-Length": str(size), "Cache-Control": "private, no-store",
                     "X-Content-Type-Options": "nosniff",
                     "Content-Disposition": f'attachment; filename="kura-{manifest["export_id"]}.zip"'})
        handed_off = True
        return response
    except service.NotFound as exc:
        raise KuraError(404, "collection_not_found", fix="自分の作品を選び直してください。") from exc
    except collection_export.ExportLimit as exc:
        raise KuraError(422, "export_budget_exceeded", context={"reason": str(exc)}, fix="作品を小さくするか保存先の空き容量を確認してください。") from exc
    except (OSError, StorageIntegrityError) as exc:
        raise KuraError(503, "export_storage_unavailable", fix="空き容量と原本の状態を確認して再試行してください。") from exc
    finally:
        if not handed_off:
            _export_slots.release()


@router.get("/legacy/pools/{pool_id}/entries")
async def legacy_entries(pool_id: int, request: Request, after_order: int | None = Query(None),
                         after_post: int | None = Query(None, gt=0), limit: int = Query(40, ge=1, le=100),
                         user=Depends(get_current_user), db=Depends(get_db)):
    if (after_order is None) != (after_post is None):
        raise KuraError(422, "invalid_cursor", fix="一覧を最初から開き直してください。")
    try:
        return await legacy.entries(db, pool_id, safe(request),
                                    (after_order, after_post) if after_order is not None else None, limit)
    except service.NotFound as exc:
        raise KuraError(404, "collection_not_found", fix="既存作品の番号を確認してください。") from exc


@router.get("/legacy/pools/{pool_id}/media/{post}/{kind}")
async def legacy_content(pool_id: int, post: int, kind: Literal["thumbnail", "preview"], request: Request,
                         user=Depends(get_current_user), db=Depends(get_db)):
    try:
        row = await legacy.media(db, pool_id, post, safe(request), kind)
    except service.NotFound as exc:
        raise KuraError(404, "media_not_found", fix="表示条件と作品を確認してください。") from exc
    data = row["payload"]
    await db.rollback()  # serving bytes/files must not retain a DB transaction
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if data is not None:
        return Response(bytes(data), media_type="image/webp", headers=headers)
    from api.services import files
    names = files.names("thumbnail" if kind == "thumbnail" else "content", post, row["media_type"])
    path = next((Path(name) for name in names if Path(name).is_file()), None)
    if path is None:
        raise KuraError(404, "media_unavailable", fix="従来画面から保存先の状態を確認してください。")
    return FileResponse(path, media_type="image/jpeg" if kind == "thumbnail" else row["media_type"], headers=headers)


@router.post("/upload")
async def upload(file: UploadFile, request_key: UUID = Form(...), collection: int | None = Form(None),
                 relative_path: str | None = Form(None), operation_key: UUID | None = Form(None),
                 user=Depends(get_current_user), db=Depends(get_db)):
    owner = user.user_id
    await db.rollback()  # release authentication transaction before disk/CPU work
    name = (file.filename or "image").replace("\\", "/").rsplit("/", 1)[-1][:255]
    if _slots.locked():
        raise HTTPException(429, "取り込み中です。少し待って同じ受付を再試行してください")
    planned = False
    async def reject_input(code,message):
        if operation_key is not None and planned:
            async with async_session.begin() as errors_db:
                await import_service.reject(errors_db,owner,operation_key,request_key,code,message)
    try:
        relative_path = service.validate_relative_path(relative_path, name)
        if operation_key is not None:
            item=await import_service.planned_item(db,owner,operation_key,request_key)
            import_service.validate_upload(item,name,relative_path,collection)
            await db.rollback()
            planned=True
        async with _slots:
            storage_task=asyncio.create_task(asyncio.to_thread(store_media,file.file,root(),
                **({'expected_digest':item['sha256'],'expected_size':item['byte_size']} if planned else {})))
            try:
                stored=await asyncio.shield(storage_task)
            except asyncio.CancelledError:
                # Keep upload file and CPU slot alive until its thread releases both.
                try:
                    await storage_task
                finally:
                    raise
            async with async_session() as write_db:
                async with write_db.begin():
                    if operation_key is None:
                        result = await service.register(write_db, owner, request_key, stored, name, collection, relative_path)
                    else:
                        result = await import_service.register(write_db,owner,operation_key,request_key,stored,name,collection,relative_path)
        return {**result, "sha256": stored.digest.hex(), "bytes": stored.size}
    except service.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except service.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except service.InvalidPath as exc:
        raise HTTPException(422, {"code": "invalid_relative_path", "message": str(exc), "retryable": False}) from exc
    except MediaInputError as exc:
        await reject_input(exc.code,str(exc))
        raise HTTPException(422, {"code": exc.code, "message": str(exc), "retryable": False}) from exc
    except StorageIntegrityError as exc:
        raise HTTPException(503, {"code": "storage_integrity", "message": str(exc), "retryable": True}) from exc
    except (ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        await reject_input("invalid_image","画像を検証できませんでした。ファイルの形式と内容を確認してください。")
        raise HTTPException(422, {"code": "invalid_image", "message": "画像を検証できませんでした。ファイルの形式と内容を確認してください。", "retryable": False}) from exc
    except OSError as exc:
        raise HTTPException(503, {"code": "storage_unavailable", "message": "画像を読み出せないか保存先に書き込めません。画像と空き容量を確認してください。", "retryable": True}) from exc
    finally:
        await file.close()


@router.post("/labels/change")
async def change_labels(body: LabelCommand, user=Depends(get_current_user), db=Depends(get_db)):
    try:
        result = await label_service.apply(db, user.user_id, body.request_key,
            [(target.asset_id, target.revision) for target in body.targets], body.add, body.remove)
        await db.commit()
        return result
    except service.NotFound as exc:
        await db.rollback()
        raise HTTPException(404, {"message": str(exc), "retryable": False}) from exc
    except service.Conflict as exc:
        await db.rollback()
        raise HTTPException(409, {"message": str(exc), "retryable": False}) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(422, {"message": str(exc), "retryable": False}) from exc


@router.get("/label-operations/{key}")
async def label_operation(key: UUID, user=Depends(get_current_user), db=Depends(get_db)):
    try:
        return await label_service.operation(db, user.user_id, key)
    except service.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/media/{asset}/label-history")
async def label_history(asset: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    try:
        return await application.label_history(db,user.user_id,asset,safe_mode=safe(request))
    except service.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/media/{asset}/{kind}")
async def content(asset: int, kind: str, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    if kind not in ("thumbnail", "original", "playback") or safe(request):
        raise HTTPException(404, "画像が見つかりません")
    try:
        row = await service.original(db, user.user_id, asset)
    except service.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if kind == "playback" and not row["media_type"].startswith("video/"):
        raise HTTPException(404, "動画が見つかりません")
    path = content_path(root(), row["sha256"], kind == "thumbnail")
    if not path.is_file():
        raise HTTPException(409, "保存先を確認してください。原本またはプレビューを読み出せません")
    return FileResponse(path, media_type="image/webp" if kind == "thumbnail" else row["media_type"],
                        filename=row["original_name"] if kind == "original" else None,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
