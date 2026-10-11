"""Thin HTTP mapping and outermost ASGI response-lifetime business gate."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager, nullcontext
from pathlib import Path
import threading
import anyio

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from .models import BackupError


def error_response(exc):
    code = exc.code
    status = (503 if code == "maintenance_busy" else
              409 if code in {"stale_epoch", "stale_revision", "invalid_preview", "prepare_conflict"} else
              413 if code == "resource_limit" else
              507 if code == "insufficient_space" else
              404 if code == "not_found" else 422)
    return JSONResponse({"detail": exc.message, "code": code}, status_code=status)


def start_registered_worker(coordinator, kind, target, args=()):
    ticket = coordinator.reserve_worker(kind, mutating=True)
    try:
        worker = threading.Thread(target=ticket.run, args=(target, args), daemon=True)
        worker.start()
        return worker
    except BaseException:
        ticket.abort_start()
        raise


def _db_stamp(paths):
    # Fixed, bounded observations of known DB/WAL files. No database connection,
    # photo scan, or constructor. A partial committed initialization is a write.
    result = []
    for db in paths:
        for suffix in ("", "-wal", "-journal"):
            path = Path(str(db) + suffix)
            try:
                stat = path.stat()
                result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except OSError:
                result.append(None)
    return result


@contextmanager
def _track_db_initialization(coordinator, paths):
    """Caller already owns business access; record only a real bounded change."""
    before = _db_stamp(paths)
    try:
        yield
    finally:
        if before != _db_stamp(paths):
            epoch, _ = coordinator.version()
            # This is an internal write signal, not a headerless user mutation.
            with coordinator.service(epoch.epoch, mutating=True):
                pass


class BackupMaintenanceMiddleware:
    def __init__(self, app, coordinator):
        self.app, self.coordinator = app, coordinator

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path == "/api/health":
            return await self.app(scope, receive, send)
        mutating = scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        headers = dict(scope.get("headers", []))
        raw_epoch = headers.get(b"x-tradewind-data-epoch")
        epoch = raw_epoch.decode("utf-8", errors="replace") if raw_epoch is not None else None
        if path.startswith("/api/backup/"):
            # Control requests do not take business leases (prepared cancellation
            # must remain reachable), but cannot bypass the data epoch guard.
            current, _ = self.coordinator.version()
            if mutating and ((epoch is None and current.require_header)
                             or (epoch is not None and epoch != current.epoch)):
                return await error_response(BackupError("stale_epoch", "数据版本已变化，请重新打开页面。"))(scope, receive, send)
            return await self.app(scope, receive, send)
        lease = self.coordinator.service(epoch, mutating=mutating)
        try:
            lease.__enter__()
        except BackupError as exc:
            return await error_response(exc)(scope, receive, send)
        try:
            state = getattr(scope.get("app"), "state", None)
            paths = getattr(state, "backup_business_db_paths", lambda: ())()
            with _track_db_initialization(self.coordinator, paths) if not mutating else nullcontext():
                await self.app(scope, receive, send)
        finally:
            lease.__exit__(None, None, None)


class OwnedFileResponse(FileResponse):
    def __init__(self, service, operation, *, rescue):
        # Acquisition must occur OUTSIDE BaseHTTP's intermediate response stream;
        # outer middleware releases this scope-owned context at the real client.
        self.service, self.operation, self.rescue = service, operation, rescue
        super().__init__("unused", media_type="application/zip", filename="tradewind-backup.zip")
        del self.headers["accept-ranges"]

    async def __call__(self, scope, receive, send):
        context = self.service.download(self.operation, rescue=self.rescue)
        try:
            path = context.__enter__()
        except BackupError as exc:
            return await error_response(exc)(scope, receive, send)
        self.path = path
        owners = scope.setdefault("backup_download_contexts", [])
        owners.append(context)
        try:
            try:
                file = path.open("rb")
            except OSError:
                return await error_response(BackupError("operation_failed", "无法读取备份材料，请检查磁盘权限。"))(scope, receive, send)
            # Explicit synchronous finally closes even in an already-cancelled
            # AnyIO scope. AsyncFile.__aexit__ can be cancelled before close.
            with file:
                self.set_stat_headers(path.stat())
                async with anyio.create_task_group() as group:
                    async def disconnected():
                        while True:
                            if (await receive())["type"] == "http.disconnect":
                                group.cancel_scope.cancel()
                                return
                    group.start_soon(disconnected)
                    await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
                    while True:
                        chunk = await anyio.to_thread.run_sync(file.read, self.chunk_size)
                        more = len(chunk) == self.chunk_size
                        await send({"type": "http.response.body", "body": chunk, "more_body": more})
                        if not more:
                            break
                    group.cancel_scope.cancel()
        finally:
            # With no ownership wrapper (standalone response), still clean up.
            if not scope.get("backup_outer_gate"):
                owners.remove(context)
                context.__exit__(None, None, None)


class _DownloadLifetime:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope["backup_outer_gate"] = True
        scope["backup_download_contexts"] = []
        try:
            await self.app(scope, receive, send)
        finally:
            for context in reversed(scope["backup_download_contexts"]):
                context.__exit__(None, None, None)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Export(_Body):
    includes_photos: StrictBool = False


class _Prepare(_Body):
    preview_id: StrictStr
    epoch: StrictStr
    request_id: StrictStr


class _CancelRestore(_Body):
    operation_id: StrictStr


def backup_router(service):
    router = APIRouter(prefix="/api/backup")

    @router.get("/state")
    def state():
        return asdict(service.state())

    @router.post("/export")
    def export(body: _Export):
        return asdict(service.start_export(body.includes_photos))

    @router.get("/operations/{id}")
    def operation(id: str):
        return asdict(service.operation(id))

    @router.post("/operations/{id}/cancel")
    def cancel(id: str):
        service.cancel(id)
        return {"ok": True}

    @router.get("/operations/{id}/download")
    def download(id: str):
        return OwnedFileResponse(service, id, rescue=False)

    @router.post("/preview")
    async def preview(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
            raise BackupError("invalid_request", "请上传 ZIP 原始文件流。")
        ticket = service.begin_upload()
        try:
            async for chunk in request.stream():
                ticket.write(chunk)
            return asdict(ticket.finish())
        finally:
            ticket.abort()

    @router.post("/restore/prepare")
    def prepare(body: _Prepare):
        return asdict(service.prepare(body.preview_id, body.epoch, body.request_id))

    @router.post("/restore/cancel")
    def cancel_restore(body: _CancelRestore):
        service.cancel_restore(body.operation_id)
        return {"ok": True}

    @router.get("/rescues/{id}/download")
    def rescue(id: str):
        return OwnedFileResponse(service, id, rescue=True)

    @router.delete("/rescues/{id}")
    def delete(id: str):
        service.delete_rescue(id)
        return {"ok": True}

    return router
