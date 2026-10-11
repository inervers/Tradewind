"""Process-local operations and ownership; restore alone writes journals."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import threading
import time
import uuid

from .archive import capture_snapshot, write_archive, _identity, _remove_owned
from .maintenance import MaintenanceCoordinator
from .models import (BackupCancelled, BackupError, BackupLayout, BackupLimits,
                     BackupState, OperationView, PreparedRestore, RescueView,
                     RestoreOutcome, RestorePreview, ValidatedArchive)
from .paths import BUSINESS_FILES, check_memory_path, owned_path, require_space
from .restore import _read_recovery_state, _read_journal, cancel_prepared_restore, prepare_restore
from .validation import validate_archive


def _error(code="operation_failed"):
    return BackupError(code, {
        "operation_failed": "无法完成备份操作，请检查磁盘权限后重试。",
        "not_found": "备份操作或材料不存在。",
        "maintenance_busy": "业务操作或备份维护尚未结束，请稍后重试。",
        "invalid_preview": "恢复预览已失效，请重新上传并预览。",
        "prepare_conflict": "已有不同的恢复准备，请先取消原准备。",
        "invalid_request": "备份请求格式无效。",
        "resource_limit": "备份超过本版本允许的资源上限。",
    }[code])


def _id(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).version == 4
    except ValueError:
        return False


@dataclass
class _Operation:
    view: OperationView
    folder: Path
    identity: tuple
    cancel: threading.Event
    created: float
    owners: int = 0
    validated: ValidatedArchive | None = None
    download_path: Path | None = None
    thread: threading.Thread | None = None
    upload: UploadTicket | None = None


class BackupService:
    def __init__(self, layout: BackupLayout, *, memory_path: Path, app_version: str,
                 coordinator: MaintenanceCoordinator, limits: BackupLimits):
        self.layout, self.memory_path = layout, Path(memory_path)
        self.app_version, self.coordinator, self.limits = app_version, coordinator, limits
        self._lock = threading.RLock()
        self._operations = {}
        self._preview = None
        self._requests = {}
        self._rescue_owners = {}
        self._photo_estimate = 0
        self._report = RestoreOutcome("none", "", "", {})
        # Validate control BEFORE orphan cleanup; never repair or rewrite it here.
        _read_recovery_state(layout)
        work = owned_path(layout, ".backup-work")
        if work.exists():
            for child in work.iterdir():
                if not _id(child.name):
                    raise _error()
                child = owned_path(layout, ".backup-work/" + child.name)
                _remove_owned(child, _identity(child), tree=True)

    def _feature(self):
        check_memory_path(self.layout, self.memory_path)
        _read_recovery_state(self.layout)

    def _new(self, kind):
        operation = str(uuid.uuid4())
        owned_path(self.layout, ".backup-work").mkdir(parents=True, exist_ok=True)
        folder = owned_path(self.layout, ".backup-work/" + operation)
        folder.mkdir()
        item = _Operation(OperationView(operation, kind, "running", "queued", 0, 0, "", "", None, None),
                          folder, _identity(folder), threading.Event(), time.time())
        self._operations[operation] = item
        return item

    def _cleanup(self, item):
        if item.owners:
            raise _error("maintenance_busy")
        try:
            _remove_owned(item.folder, item.identity, tree=True)
        except OSError:
            raise _error() from None
        if self._preview == item.view.id:
            self._preview = None

    def _expire(self):
        now = time.time()
        for item in self._operations.values():
            ttl = item.view.preview.expires_at if item.view.preview else item.created + 24 * 3600
            if (item.view.status != "running" and not item.owners and now >= ttl
                    and item.view.kind != "prepare" and self.coordinator.mode != "prepared"):
                self._cleanup(item)
                item.view = replace(item.view, status="cancelled", preview=None)

    def _progress(self, item):
        def update(phase, completed, total):
            with self._lock:
                item.view = replace(item.view, phase=phase, completed=completed, total=total)
        return update

    def _launch(self, item, target, lease=None):
        def run():
            try:
                target()
                if lease is not None:
                    lease.release()
                with self._lock:
                    if item.cancel.is_set() and item.view.kind != "prepare":
                        raise BackupCancelled()
                    item.view = replace(item.view, status="done", phase="done")
            except Exception as exc:
                safe = exc if isinstance(exc, BackupError) else _error()
                with self._lock:
                    item.view = replace(item.view, status="cancelled" if isinstance(exc, BackupCancelled) else "error",
                                        code=safe.code, message=safe.message)
                    try:
                        self._cleanup(item)
                    except (BackupError, OSError):
                        item.view = replace(item.view, code="operation_failed", message=_error().message)
            finally:
                if lease is not None:
                    lease.release()
        try:
            item.thread = threading.Thread(target=run, daemon=True)
            item.thread.start()
        except Exception:
            if lease is not None:
                lease.release()
            item.view = replace(item.view, status="error", code="operation_failed", message=_error().message)
            self._cleanup(item)
            raise _error() from None

    def state(self) -> BackupState:
        with self._lock:
            self._expire()
            epoch, revision = self.coordinator.version()
            document = _read_recovery_state(self.layout)
            prepared = None
            if document and document["state"] == "prepared":
                prepared = PreparedRestore(document["operation_id"], document["operation_id"], "prepared")
            rescues = []
            root = owned_path(self.layout, "backups")
            if root.exists():
                for folder in root.iterdir():
                    if not _id(folder.name):
                        raise _error()
                    archive = owned_path(self.layout, "backups/" + folder.name + "/rescue.zip")
                    journal = owned_path(self.layout, "backups/" + folder.name + "/recovery.json")
                    if archive.is_file() and journal.is_file():
                        control = _read_journal(self.layout, folder.name)
                        rescues.append(RescueView(folder.name, str(control["created_at"]), archive.stat().st_size,
                            control["state"] in {"prepared", "applying", "rolling_back"}))
            # Cheap, non-validating bootstrap: no DB constructors, photo traversal,
            # credentials, or strict business parsing. Estimates are optional.
            estimated = 0
            for name in BUSINESS_FILES:
                try:
                    path = owned_path(self.layout, name)
                    if path.is_file():
                        estimated += path.stat().st_size
                except (OSError, BackupError):
                    pass
            return BackupState(self.coordinator.mode, epoch.epoch, epoch.require_header, revision,
                               prepared, rescues, self._report, estimated, self._photo_estimate)

    def start_export(self, includes_photos: bool) -> OperationView:
        if type(includes_photos) is not bool:
            raise _error("invalid_request")
        with self._lock:
            self._feature()
            self._expire()
            lease = self.coordinator.reserve_exclusive("snapshot")
            try:
                item = self._new("export")
            except Exception:
                lease.release()
                raise _error() from None
            def export():
                snapshot = capture_snapshot(self.layout, memory_path=self.memory_path,
                    includes_photos=includes_photos, destination=item.folder / "capture", limits=self.limits,
                    lease=lease, cancel=item.cancel.is_set, progress=self._progress(item))
                snapshot = replace(snapshot, summary=replace(snapshot.summary, app_version=self.app_version))
                if includes_photos:
                    with self._lock:
                        self._photo_estimate = snapshot.summary.counts["photos"]
                lease.release()
                path = write_archive(snapshot, item.folder / "export.zip", limits=self.limits,
                                     cancel=item.cancel.is_set, progress=self._progress(item))
                with self._lock:
                    if item.cancel.is_set():
                        raise BackupCancelled()
                    item.download_path = path
            self._launch(item, export, lease)
            return item.view

    def begin_upload(self) -> UploadTicket:
        with self._lock:
            self._feature()
            self._expire()
            with self.coordinator.service(None, mutating=False):
                if self._preview is not None:
                    raise _error("maintenance_busy")
                item = self._new("preview")
                self._preview = item.view.id
                try:
                    ticket = UploadTicket(self, item)
                    item.upload = ticket
                    return ticket
                except Exception:
                    self._cleanup(item)
                    raise _error() from None

    def operation(self, id: str) -> OperationView:
        with self._lock:
            self._expire()
            item = self._operations.get(id)
            if item is None:
                raise _error("not_found")
            return item.view

    def cancel(self, id: str) -> None:
        with self._lock:
            item = self._operations.get(id)
            if item is None:
                raise _error("not_found")
            if item.owners or item.view.kind == "prepare" or self.coordinator.mode == "prepared":
                raise _error("maintenance_busy")
            item.cancel.set()
            if item.upload is not None:
                item.upload.abort()
            if item.view.status != "running":
                self._cleanup(item)
                item.view = replace(item.view, status="cancelled", preview=None)

    def prepare(self, preview_id: str, epoch: str, request_id: str) -> OperationView:
        with self._lock:
            self._feature()
            if type(request_id) is not str or not 0 < len(request_id) <= 128 or not _id(preview_id):
                raise _error("invalid_request")
            # Durable authority and same-request lookup precede exclusive admission.
            document = _read_recovery_state(self.layout)
            previous = self._requests.get(request_id)
            if previous:
                bound_preview, bound_epoch, operation = previous
                if (bound_preview, bound_epoch) != (preview_id, epoch):
                    raise _error("prepare_conflict")
                item = self._operations[operation]
                if document and document["state"] == "prepared" and document["request_id"] == request_id:
                    source = self._operations[bound_preview]
                    if (source.validated is None or document["archive_sha256"] != source.validated.archive_sha256
                            or (item.view.prepared is not None
                                and item.view.prepared.operation_id != document["operation_id"])):
                        raise _error("prepare_conflict")
                    # Publication is authoritative even when the worker's ack
                    # or subsequent scratch cleanup failed. Never reserve or
                    # prepare again; reconstruct only the safe existing view.
                    prepared = PreparedRestore(document["operation_id"], document["operation_id"], "prepared")
                    item.view = replace(item.view, status="done", phase="done", prepared=prepared, code="", message="")
                    source.view = replace(source.view, status="cancelled", preview=None)
                    if self._preview == bound_preview:
                        self._preview = None
                    return item.view
                if item.view.status in {"running", "done"}:
                    return item.view
            if document and document["state"] in {"prepared", "applying", "rolling_back"}:
                raise _error("prepare_conflict")
            self._expire()
            source = self._operations.get(preview_id)
            preview = source.view.preview if source else None
            current_epoch, revision = self.coordinator.version()
            if (not preview or source.view.status != "done" or preview.epoch != epoch
                    or epoch != current_epoch.epoch or preview.revision != revision
                    or preview.expires_at <= time.time()):
                raise _error("invalid_preview")
            lease = self.coordinator.reserve_exclusive("prepare")
            try:
                item = self._new("prepare")
            except Exception:
                lease.release()
                raise _error() from None
            self._requests[request_id] = (preview_id, epoch, item.view.id)
            def prepare():
                result = prepare_restore(self.layout, source.validated, preview, request_id=request_id,
                    coordinator=self.coordinator, lease=lease, memory_path=self.memory_path, limits=self.limits,
                    cancel=item.cancel.is_set, progress=self._progress(item))
                with self._lock:
                    item.view = replace(item.view, prepared=result)
                    self._cleanup(source)
                    source.view = replace(source.view, status="cancelled", preview=None)
                    self._cleanup(item)
            self._launch(item, prepare, lease)
            return item.view

    def cancel_restore(self, operation_id: str) -> None:
        with self._lock:
            if not _id(operation_id):
                raise _error("not_found")
            folder = owned_path(self.layout, "backups/" + operation_id)
            if not (folder / "recovery.json").is_file():
                raise _error("not_found")
            if self._rescue_owners.get(operation_id) or self.coordinator.mode == "prepare":
                raise _error("maintenance_busy")
            cancel_prepared_restore(self.layout, operation_id, self.coordinator)
            for key, (_, _, id) in list(self._requests.items()):
                item = self._operations[id]
                if item.view.prepared and item.view.prepared.operation_id == operation_id:
                    item.view = replace(item.view, status="cancelled")
                    del self._requests[key]

    @contextmanager
    def download(self, id: str, *, rescue: bool):
        with self._lock:
            self._expire()
            if rescue:
                if not _id(id):
                    raise _error("not_found")
                path = owned_path(self.layout, "backups/" + id + "/rescue.zip")
                journal = owned_path(self.layout, "backups/" + id + "/recovery.json")
                if not path.is_file() or not journal.is_file():
                    raise _error("not_found")
                _read_journal(self.layout, id)
                self._rescue_owners[id] = self._rescue_owners.get(id, 0) + 1
            else:
                item = self._operations.get(id)
                if not item or item.view.status != "done" or item.download_path is None:
                    raise _error("not_found")
                path = owned_path(self.layout, ".backup-work/" + id + "/export.zip")
                if not path.is_file():
                    raise _error("not_found")
                item.owners += 1
        try:
            yield path
        finally:
            with self._lock:
                if rescue:
                    self._rescue_owners[id] -= 1
                else:
                    item.owners -= 1

    def delete_rescue(self, id: str) -> None:
        with self._lock:
            if not _id(id):
                raise _error("not_found")
            folder = owned_path(self.layout, "backups/" + id)
            if not (folder / "recovery.json").is_file():
                raise _error("not_found")
            _read_recovery_state(self.layout)
            document = _read_journal(self.layout, id)
            if (document["state"] in {"prepared", "applying", "rolling_back"}
                    or self._rescue_owners.get(id) or self.coordinator.mode in {"prepare", "prepared"}):
                raise _error("maintenance_busy")
            try:
                _remove_owned(folder, _identity(folder), tree=True)
            except OSError:
                raise _error() from None


class UploadTicket:
    def __init__(self, service, item):
        self._service, self._item = service, item
        self._stream = (item.folder / "upload.zip").open("xb")
        self._bytes, self._closed = 0, False

    def write(self, chunk: bytes) -> None:
        with self._service._lock:
            if self._closed or self._item.cancel.is_set():
                raise BackupCancelled()
            try:
                if self._bytes + len(chunk) > self._service.limits.max_archive_bytes:
                    raise _error("resource_limit")
                require_space(self._item.folder, len(chunk), self._service.limits)
                self._stream.write(chunk)
                self._bytes += len(chunk)
            except Exception as exc:
                self.abort()
                raise exc if isinstance(exc, BackupError) else _error() from None

    def finish(self) -> OperationView:
        service, item = self._service, self._item
        with service._lock:
            if self._closed:
                raise BackupCancelled()
            self._stream.close()
            self._closed = True
            item.upload = None
            def validate():
                validated = validate_archive(item.folder / "upload.zip", item.folder / "validated",
                    limits=service.limits, cancel=item.cancel.is_set, progress=service._progress(item))
                with service.coordinator.exclusive("snapshot") as lease:
                    snapshot = capture_snapshot(service.layout, memory_path=service.memory_path,
                        includes_photos=validated.snapshot.summary.includes_photos, destination=item.folder / "current",
                        limits=service.limits, lease=lease, cancel=item.cancel.is_set, progress=service._progress(item))
                    epoch, revision = service.coordinator.version()
                with service._lock:
                    if item.cancel.is_set():
                        raise BackupCancelled()
                    item.validated = validated
                    if snapshot.summary.includes_photos:
                        service._photo_estimate = snapshot.summary.counts["photos"]
                    item.view = replace(item.view, preview=RestorePreview(item.view.id, validated.archive_sha256,
                        epoch.epoch, revision, time.time() + 1800, validated.snapshot.summary,
                        replace(snapshot.summary, app_version=service.app_version)))
            service._launch(item, validate)
            return item.view

    def abort(self) -> None:
        with self._service._lock:
            if self._closed:
                return
            self._closed = True
            self._stream.close()
            self._item.cancel.set()
            self._item.upload = None
            self._service._cleanup(self._item)
            self._item.view = replace(self._item.view, status="cancelled")
