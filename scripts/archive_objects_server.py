#!/usr/bin/env python3
# Description: kura-archive-objects — read-only HTTP access to the original archive on the
#              node its disk is attached to, for a kura node without that disk
#              (docs/notes/kura-on-nodeb-plan.md, "Originals on another host", rev 8)
# Effects: read-only (never writes, lists or follows a symlink out of the archive)
"""Read-only access to archived originals.

Routes (all require `X-Archive-Key` = KURA_ARCHIVE_READ_KEY, and the client address in
KURA_ARCHIVE_ALLOWED_CLIENTS when that is set):

    GET      /archive-id                       the `.archive-id` of the mounted archive
    GET|HEAD /objects/sha256/<xx>/<sha>        a content-addressed object
    GET|HEAD /legacy/posts/<name>              an original in the older kura-media layout

Every answer carries `X-Archive-Id`. When the archive disk is not mounted where it is
configured (KURA_ARCHIVE_MOUNT must be a mount point and the roots must lie under it — the
test MountGuard uses), every route answers 503 instead of serving an empty directory.
Files are served only if they are regular files directly at the expected path; a symlink is
never followed. Range requests are answered by Starlette's FileResponse.

Run on the archive node, bound to its LAN address:
    uvicorn scripts.archive_objects_server:app --host "$KURA_ARCHIVE_BIND" --port 8470
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

SHA = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^\d+_[0-9a-f]{16}\.[A-Za-z0-9]{1,8}$")


def _settings() -> dict:
    return {
        "mount": Path(os.environ.get("KURA_ARCHIVE_MOUNT", "").strip() or "/nonexistent"),
        "root": Path(os.environ.get("KURA_ORIGINAL_ARCHIVE_ROOT", "").strip() or "/nonexistent"),
        "legacy": Path(os.environ.get("KURA_LEGACY_ORIGINAL_ROOT", "").strip() or "/nonexistent"),
        "key": os.environ.get("KURA_ARCHIVE_READ_KEY", "").strip(),
        "allowed": {a.strip() for a in os.environ.get("KURA_ARCHIVE_ALLOWED_CLIENTS", "").split(",") if a.strip()},
    }


def _archive_id(cfg: dict) -> str | None:
    mount, root = cfg["mount"], cfg["root"]
    try:
        if not mount.is_mount() or root == mount or not root.resolve().is_relative_to(mount.resolve()):
            return None
        if root.stat().st_dev != mount.stat().st_dev:
            return None
        return (root / ".archive-id").read_text().strip() or None
    except OSError:
        return None


def _gate(request: Request) -> tuple[dict, str] | Response:
    cfg = _settings()
    if not cfg["key"] or not secrets.compare_digest(request.headers.get("X-Archive-Key", ""), cfg["key"]):
        return PlainTextResponse("unauthorized", status_code=401)
    if cfg["allowed"] and (request.client is None or request.client.host not in cfg["allowed"]):
        return PlainTextResponse("forbidden", status_code=403)
    archive_id = _archive_id(cfg)
    if archive_id is None:
        return PlainTextResponse("archive not mounted", status_code=503)
    return cfg, archive_id


def _open_under(root: Path, parts: list[str]) -> int:
    """Open root/parts[0]/…/parts[-1] refusing a symlink at every component (codex review of
    1.269.0: a symlinked intermediate directory led outside the root). Returns the fd of a
    regular file; raises FileNotFoundError otherwise."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], flags, dir_fd=fd)
    except OSError:
        os.close(fd)
        raise FileNotFoundError("/".join(parts)) from None
    os.close(fd)
    if not stat.S_ISREG(os.fstat(leaf).st_mode):
        os.close(leaf)
        raise FileNotFoundError("/".join(parts))
    return leaf


class _ClosingFileResponse(FileResponse):
    """Closes the checked descriptor however the response ends (sent, 400/416 for a bad Range
    — Starlette skips `background` there — or the client went away)."""

    def __init__(self, fd: int, **kwargs):
        self._fd = fd
        super().__init__(f"/proc/self/fd/{fd}", stat_result=os.fstat(fd), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with contextlib.suppress(OSError):
                os.close(self._fd)


def _serve(root: Path, parts: list[str], archive_id: str) -> Response:
    try:
        fd = _open_under(root, parts)
    except FileNotFoundError:
        return PlainTextResponse("not found", status_code=404, headers={"X-Archive-Id": archive_id})
    # Serve the descriptor that was checked, not the name again: /proc/self/fd/<fd> reopens
    # that same file, so a rename or symlink swap after the check cannot change what is sent.
    try:
        return _ClosingFileResponse(fd, media_type="application/octet-stream",
                                    headers={"X-Archive-Id": archive_id})
    except BaseException:
        os.close(fd)
        raise


async def archive_id(request: Request) -> Response:
    gated = _gate(request)
    if isinstance(gated, Response):
        return gated
    _, aid = gated
    return PlainTextResponse(aid, headers={"X-Archive-Id": aid})


async def cas_object(request: Request) -> Response:
    gated = _gate(request)
    if isinstance(gated, Response):
        return gated
    cfg, aid = gated
    prefix, sha = request.path_params["prefix"], request.path_params["sha"]
    if not SHA.fullmatch(sha) or prefix != sha[:2]:
        return PlainTextResponse("bad object name", status_code=400, headers={"X-Archive-Id": aid})
    return _serve(cfg["root"], ["objects", "sha256", prefix, sha], aid)


async def legacy_original(request: Request) -> Response:
    gated = _gate(request)
    if isinstance(gated, Response):
        return gated
    cfg, aid = gated
    name = request.path_params["name"]
    if not NAME.fullmatch(name):
        return PlainTextResponse("bad name", status_code=400, headers={"X-Archive-Id": aid})
    legacy = cfg["legacy"]
    try:
        if not legacy.resolve().is_relative_to(cfg["mount"].resolve()):
            return PlainTextResponse("legacy root outside the archive mount", status_code=503)
    except OSError:
        return PlainTextResponse("legacy root unavailable", status_code=503)
    return _serve(legacy, [name], aid)


app = Starlette(routes=[
    Route("/archive-id", archive_id, methods=["GET"]),
    Route("/objects/sha256/{prefix}/{sha}", cas_object, methods=["GET", "HEAD"]),
    Route("/legacy/posts/{name}", legacy_original, methods=["GET", "HEAD"]),
])
