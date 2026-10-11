"""Rescue preparation and initializer-free startup transactions.

Private control contract for BackupService: ``_read_recovery_state(layout)``
returns a validated v1 journal dictionary (active, otherwise latest terminal),
or None. It is read-only and raises sanitized BackupError on ambiguity. Only
this module writes recovery.json; no caller-provided paths are persisted.
PreparedRestore IDs identify backups/<canonical UUID4>/rescue.zip. The startup
caller MUST gate normal imports/API on a blocked outcome.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .archive import capture_snapshot, write_archive, _identity, _remove_owned
from .maintenance import MaintenanceCoordinator, MaintenanceLease, _read_epoch
from .models import (BackupCancelled, BackupError, BackupLayout, BackupLimits,
                     CancelCheck, PreparedRestore, ProgressSink, RestoreOutcome,
                     RestorePreview, ValidatedArchive)
from .paths import (_checked_path, owned_path, resolve_layout, check_memory_path,
                    PHOTO_DIRECTORIES)
from .validation import (inspect_snapshot, validate_archive, _schema, _normalized,
                         _OUTREACH_DDL, _limits)

_JSON_FILES = ("customers.json", "products.json", "emails.json", "crawler_seen.json")
_RUNTIME_FILES = ("tradewind_memory.db-wal", "tradewind_memory.db-shm",
                  "tradewind_memory.db-journal", "tradewind_workflows.db",
                  "tradewind_workflows.db-wal", "tradewind_workflows.db-shm",
                  "tradewind_workflows.db-journal")
_SAFETY_KEYS = ("suppressions_added", "suppressed_jobs", "email_requires_sentbox_check",
                "whatsapp_requires_review")
_STATES = {"prepared", "cancelled", "applying", "rolling_back", "rolled_back", "committed"}
_ACTIVE_STATES = {"prepared", "applying", "rolling_back"}
_CONTROL_BOUND = 64 * 1024


def _error(code="restore_blocked"):
    return BackupError(code, {
        "restore_blocked": "恢复状态无法安全确认，请保留救援备份并联系维护人员。",
        "invalid_recovery_state": "恢复日志无效，请保留本机资料并联系维护人员。",
        "invalid_preview": "恢复预览已失效，请重新上传并预览。",
        "prepare_conflict": "已有不同的恢复准备，请先取消原准备。",
        "prepare_failed": "无法完成恢复准备，请检查磁盘权限后重试。",
        "cancel_failed": "无法持久保存取消状态，仍保持维护，请重试取消。",
    }[code])


def _check(value, code="invalid_recovery_state"):
    if not value:
        raise _error(code)


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _operation(value):
    _check(_uuid(value) and uuid.UUID(value).version == 4)
    return value


def _op_path(layout, operation, relative=""):
    base = "backups/" + _operation(operation)
    return owned_path(layout, base + ("/" + relative if relative else ""))


def _runtime_target(layout, name):
    """Closed extra runtime namespace; never expands archive/path APIs."""
    if name not in _RUNTIME_FILES:
        return owned_path(layout, name)
    # resolve_layout validates every lexical ancestor before resolving links.
    checked = resolve_layout(layout.data_dir.parent)
    _check(checked == layout)
    target = _checked_path(checked.data_dir / name)
    _check(target.parent == checked.data_dir)
    return target


def _names(photos):
    return (*_JSON_FILES, "tradewind_memory.db",
            *(sorted(PHOTO_DIRECTORIES) if photos else ()), *_RUNTIME_FILES, ".data-epoch.json")


def _digest(path):
    """Bounded reads, checked whole directory trees including empty folders."""
    path = _checked_path(path)
    if not path.exists():
        return None
    result = hashlib.sha256()
    def walk(current):
        current = _checked_path(current)
        relative = current.relative_to(path).as_posix().encode("utf-8")
        _check(current.is_dir() or current.is_file())
        result.update(b"D" if current.is_dir() else b"F")
        result.update(len(relative).to_bytes(4, "big") + relative)
        if current.is_dir():
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                walk(child)
        else:
            result.update(current.stat().st_size.to_bytes(8, "big"))
            with current.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    result.update(chunk)
    walk(path)
    return result.hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with _checked_path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path):
    # Windows os.replace uses the local filesystem's atomic namespace update;
    # Python cannot fsync a Windows directory handle. POSIX also syncs parents.
    if os.name != "nt":
        descriptor = os.open(_checked_path(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _move(source, target):
    source, target = _checked_path(source), _checked_path(target)
    _check(source.exists() and not target.exists())
    os.replace(source, target)
    _sync_directory(source.parent)
    if source.parent != target.parent:
        _sync_directory(target.parent)


def _pairs(values):
    result = {}
    for key, value in values:
        _check(key not in result)
        result[key] = value
    return result


def _validate_control(document, operation):
    _check(type(document) is dict and set(document) == {
        "format_version", "operation_id", "request_id", "archive_sha256", "rescue_sha256",
        "includes_photos", "created_at", "state", "entries", "counts", "new_epoch", "pending_sha256"})
    _check(type(document["format_version"]) is int and document["format_version"] == 1)
    _check(document["operation_id"] == _operation(operation))
    _check(type(document["request_id"]) is str and 0 < len(document["request_id"]) <= 128)
    for key in ("archive_sha256", "rescue_sha256", "pending_sha256"):
        _check(type(document[key]) is str and re.fullmatch("[0-9a-f]{64}", document[key]))
    _check(type(document["includes_photos"]) is bool and document["state"] in _STATES)
    _check(type(document["created_at"]) in {int, float} and math.isfinite(document["created_at"])
           and document["created_at"] > 0)
    counts = document["counts"]
    _check(type(counts) is dict and set(counts) == set(_SAFETY_KEYS) and
           all(type(value) is int and value >= 0 for value in counts.values()))
    entries = document["entries"]
    _check(type(entries) is list)
    if document["state"] in {"prepared", "cancelled"} or document["state"] == "rolled_back" and not entries:
        _check(not entries and document["new_epoch"] == "")
    else:
        _check(_uuid(document["new_epoch"]) and uuid.UUID(document["new_epoch"]).version == 4)
        _check(len(entries) == len(_names(document["includes_photos"])))
        for entry, name in zip(entries, _names(document["includes_photos"])):
            _check(type(entry) is dict and set(entry) == {"name", "old", "new", "phase"})
            _check(entry["name"] == name and entry["phase"] in
                   {"queued", "intent", "done", "undo_intent", "undone"})
            for key in ("old", "new"):
                value = entry[key]
                _check(value is None or type(value) is str and re.fullmatch("[0-9a-f]{64}", value))
            if name in _RUNTIME_FILES:
                _check(entry["new"] is None)
        _check(entries[-1]["new"] is not None)
        if document["state"] == "committed":
            _check(all(entry["phase"] == "done" for entry in entries))
        if document["state"] == "rolled_back":
            _check(all(entry["phase"] == "undone" for entry in entries))
    return document


def _read_journal(layout, operation):
    path = _op_path(layout, operation, "recovery.json")
    with path.open("rb") as stream:
        raw = stream.read(_CONTROL_BOUND + 1)
    _check(len(raw) <= _CONTROL_BOUND)
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_check(False)))
    return _validate_control(document, operation)


def _read_recovery_state(layout: BackupLayout):
    """Read-only Service interface; strict fixed journals, at most one active."""
    try:
        root = owned_path(layout, "backups")
        _check(root == layout.rescue_dir)
        if not root.exists():
            return None
        documents = []
        for directory in root.iterdir():
            # This namespace is private. Unknown names or links fail closed,
            # rather than being guessed as another recovery transaction.
            operation = _operation(directory.name)
            directory = _op_path(layout, operation)
            _check(directory.is_dir())
            journal = _op_path(layout, operation, "recovery.json")
            if journal.exists():
                documents.append(_read_journal(layout, operation))
        active = [document for document in documents if document["state"] in _ACTIVE_STATES]
        _check(len(active) <= 1)
        return active[0] if active else max(documents, key=lambda doc: doc["created_at"], default=None)
    except BackupError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise _error("invalid_recovery_state") from None


def _persist(layout, document):
    """Only writer: bounded validated JSON, fsync before atomic publication."""
    operation = document["operation_id"]
    _validate_control(document, operation)
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    _check(len(raw) <= _CONTROL_BOUND)
    temporary = _op_path(layout, operation, "recovery.tmp")
    # A leftover temp from a crash is not authority; it must be a regular file.
    if temporary.exists():
        _check(temporary.is_file())
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(_op_path(layout, operation, "recovery.tmp"),
               _op_path(layout, operation, "recovery.json"))
    _sync_directory(_op_path(layout, operation))


def _prepared(document):
    return PreparedRestore(document["operation_id"], document["operation_id"], "prepared")


def prepare_restore(layout: BackupLayout, validated: ValidatedArchive, preview: RestorePreview, *,
                    request_id: str, coordinator: MaintenanceCoordinator, lease: MaintenanceLease,
                    memory_path: Path, limits: BackupLimits, cancel: CancelCheck,
                    progress: ProgressSink) -> PreparedRestore:
    operation, identity = None, None
    try:
        existing = _read_recovery_state(layout)
        if existing and existing["state"] in _ACTIVE_STATES:
            _check(existing["state"] == "prepared" and existing["request_id"] == request_id
                   and existing["archive_sha256"] == validated.archive_sha256, "prepare_conflict")
            return _prepared(existing)
        lease.assert_active()
        _check(lease._coordinator is coordinator and lease._kind == "prepare", "invalid_preview")
        _limits(limits)
        check_memory_path(layout, memory_path)
        epoch, revision = coordinator.version()
        _check(_read_epoch(layout) == epoch, "invalid_preview")
        now = time.time()
        _check(_uuid(preview.id) and preview.epoch == epoch.epoch and preview.revision == revision
               and type(preview.expires_at) in {int, float} and math.isfinite(preview.expires_at)
               and now < preview.expires_at <= now + 1801
               and preview.archive_sha256 == validated.archive_sha256
               and type(request_id) is str and 0 < len(request_id) <= 128, "invalid_preview")
        _check(preview.backup == validated.snapshot.summary, "invalid_preview")
        if cancel():
            raise BackupCancelled()
        summary = inspect_snapshot(validated.snapshot.folder,
            includes_photos=validated.snapshot.summary.includes_photos, limits=limits)
        _check(summary.counts == preview.backup.counts and summary.items_present == preview.backup.items_present,
               "invalid_preview")
        operation = str(uuid.uuid4())
        owned_path(layout, "backups").mkdir(parents=True, exist_ok=True)
        folder = _op_path(layout, operation)
        folder.mkdir()
        identity = _identity(folder)
        snapshot = capture_snapshot(layout, memory_path=memory_path, includes_photos=True,
            destination=_op_path(layout, operation, "capture"), limits=limits, lease=lease,
            cancel=cancel, progress=progress)
        lease.assert_active()
        rescue = write_archive(snapshot, _op_path(layout, operation, "rescue.zip"), limits=limits,
                               cancel=cancel, progress=progress)
        lease.assert_active()
        checked = validate_archive(rescue, _op_path(layout, operation, "rescue-validated"),
                                   limits=limits, cancel=cancel, progress=progress)
        lease.assert_active()
        incoming = write_archive(validated.snapshot, _op_path(layout, operation, "incoming.zip"),
                                 limits=limits, cancel=cancel, progress=progress)
        lease.assert_active()
        validate_archive(incoming, _op_path(layout, operation, "pending"), limits=limits,
                         cancel=cancel, progress=progress)
        lease.assert_active()
        # Safety changes are made only to the isolated incoming copy, using
        # ONLY the verified completed rescue, while the prepare lease is held.
        # Hash/finalize this copy before publishing prepared; startup never
        # reruns SQL on a half-processed incoming database after a crash.
        pending = _op_path(layout, operation, "pending")
        for name in _JSON_FILES:
            target = _op_path(layout, operation, "pending/data/" + name)
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(b"[]")
        counts = apply_contact_safety(_op_path(layout, operation, "pending/data/tradewind_memory.db"),
                                     _op_path(layout, operation, "rescue-validated/data/tradewind_memory.db"))
        inspect_snapshot(pending, includes_photos=summary.includes_photos, limits=limits)
        for file in pending.rglob("*"):
            file = _checked_path(file)
            if file.is_file():
                with file.open("r+b") as stream:
                    os.fsync(stream.fileno())
        lease.assert_active()
        if cancel():
            raise BackupCancelled()
        document = dict(format_version=1, operation_id=operation, request_id=request_id,
            archive_sha256=validated.archive_sha256, rescue_sha256=checked.archive_sha256,
            includes_photos=summary.includes_photos, created_at=now, state="prepared", entries=[],
            counts=counts, new_epoch="", pending_sha256=_digest(pending))
        _persist(layout, document)
        coordinator.hold_prepared(operation)
        identity = None
        return _prepared(document)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise _error("prepare_failed") from None
    finally:
        if identity is not None:
            # If publication succeeded but reporting/fsync failed, the durable
            # preparation still owns maintenance. Never delete its material.
            durable = _op_path(layout, operation, "recovery.json")
            if durable.exists():
                coordinator.hold_prepared(operation)
            else:
                try:
                    _remove_owned(_op_path(layout, operation), identity, tree=True)
                except OSError:
                    raise _error("prepare_failed") from None


def cancel_prepared_restore(layout: BackupLayout, operation_id: str,
                            coordinator: MaintenanceCoordinator) -> None:
    try:
        document = _read_journal(layout, _operation(operation_id))
        _check(document["state"] in {"prepared", "cancelled"}, "prepare_conflict")
        if document["state"] == "prepared":
            document["state"] = "cancelled"
            _persist(layout, document)
        coordinator.cancel_prepared(operation_id)
        for name in ("pending", "capture", "rescue-validated", "incoming.zip"):
            path = _op_path(layout, operation_id, name)
            if path.exists():
                _remove_owned(path, _identity(path), tree=path.is_dir())
    except (OSError, ValueError, TypeError):
        raise _error("cancel_failed") from None


def apply_contact_safety(restored_db: Path, rescue_db: Path) -> dict[str, int]:
    """Only trusted SQL; rescue is a completed snapshot, never the live DB."""
    counts = dict.fromkeys(_SAFETY_KEYS, 0)
    local = []
    rescue_db, restored_db = _checked_path(rescue_db), _checked_path(restored_db)
    if rescue_db.exists():
        with closing(sqlite3.connect(rescue_db.as_uri() + "?mode=ro&immutable=1", uri=True)) as reader:
            reader.execute("PRAGMA trusted_schema=OFF")
            tables = _schema(reader)
            if "contact_suppressions" in tables:
                local = reader.execute("SELECT channel,normalized_recipient,reason,created_at FROM contact_suppressions ORDER BY id").fetchall()
    if not restored_db.exists() and not local:
        return counts
    with closing(sqlite3.connect(restored_db)) as writer, writer:
        writer.enable_load_extension(False)
        writer.execute("PRAGMA trusted_schema=OFF")
        tables = _schema(writer)
        if local and "outreach_jobs" not in tables:
            if tables == {"emails"} and len(writer.execute("PRAGMA table_info(emails)").fetchall()) == 7:
                # Earliest memory must become the known current memory group
                # before adding outreach; retain rows, IDs and sequence.
                for definition in ("language TEXT DEFAULT 'zh-hant'", "format TEXT DEFAULT 'email'",
                                   "workflow_id TEXT DEFAULT ''"):
                    writer.execute("ALTER TABLE emails ADD COLUMN " + definition)
                writer.execute("CREATE UNIQUE INDEX uq_emails_workflow ON emails(workflow_id) WHERE workflow_id <> ''")
            # This is the shared tested constant, never SQL from the archive.
            writer.executescript(_OUTREACH_DDL)
            tables = _schema(writer)
        if "outreach_jobs" not in tables:
            return counts
        for channel, recipient, reason, created in local:
            existing = writer.execute("SELECT id FROM contact_suppressions WHERE channel=? AND normalized_recipient=?",
                                      (channel, recipient)).fetchone()
            if existing:
                writer.execute("UPDATE contact_suppressions SET reason=?,created_at=? WHERE id=?",
                               (reason, created, existing[0]))
            else:
                writer.execute("INSERT INTO contact_suppressions(channel,normalized_recipient,reason,created_at) VALUES(?,?,?,?)",
                               (channel, recipient, reason, created))
                counts["suppressions_added"] += 1
        for job, channel, recipient, status in writer.execute("SELECT id,channel,recipient,status FROM outreach_jobs").fetchall():
            terminal = status not in {"pending_review", "opened", "sending", "unknown", "failed"}
            suppressed = writer.execute("SELECT 1 FROM contact_suppressions WHERE channel=? AND normalized_recipient=?",
                                        (channel, _normalized(channel, recipient))).fetchone()
            if not terminal and suppressed:
                target, code, key = "suppressed", "suppressed", "suppressed_jobs"
            elif channel == "email" and status in {"pending_review", "opened", "sending", "failed"}:
                target, code, key = "unknown", "restore_sentbox_check", "email_requires_sentbox_check"
            elif channel == "whatsapp" and status in {"pending_review", "opened", "failed"}:
                target, code, key = "skipped", "restore_manual_review", "whatsapp_requires_review"
            else:
                continue
            writer.execute("UPDATE outreach_jobs SET status=?,error_code=?,error_summary=? WHERE id=?",
                (target, code, "恢复后请人工核对发件箱。" if target == "unknown" else "恢复后请人工复核，禁止自动续跑。", job))
            counts[key] += 1
    return counts


def _stage(layout, document, name):
    relative = "epoch.json" if name == ".data-epoch.json" else "pending/data/" + name
    return _op_path(layout, document["operation_id"], relative)


def _entry_paths(layout, document, entry):
    return (_runtime_target(layout, entry["name"]),
            _op_path(layout, document["operation_id"], "original/" + entry["name"]),
            _stage(layout, document, entry["name"]))


def _verify_rollback(layout, document):
    """Validate ALL paths/material before performing any rollback writes."""
    for entry in document["entries"]:
        live, original, stage = _entry_paths(layout, document, entry)
        current, saved, pending = _digest(live), _digest(original), _digest(stage)
        old, new = entry["old"], entry["new"]
        _check(saved is None or old is not None and saved == old)
        _check(pending is None or new is not None and pending == new)
        if entry["phase"] in {"queued", "undone"}:
            _check(current == old and saved is None and pending == new)
        elif saved is not None:
            _check(current is None or new is not None and current == new and pending is None)
            _check(new is None or pending == new or current == new)
        elif old is not None:
            _check(current == old and pending == new)
        else:
            _check(current is None or new is not None and current == new and pending is None)
            _check(new is None or pending == new or current == new)


def _rollback(layout, document):
    _verify_rollback(layout, document)
    document["state"] = "rolling_back"
    _persist(layout, document)
    for entry in reversed(document["entries"]):
        live, original, stage = _entry_paths(layout, document, entry)
        if entry["phase"] == "undone":
            continue
        entry["phase"] = "undo_intent"
        _persist(layout, document)
        saved = _digest(original)
        current = _digest(live)
        if saved is not None or entry["old"] is None:
            if current is not None:
                _check(entry["new"] is not None and current == entry["new"] and not stage.exists())
                _move(live, stage)
            if saved is not None:
                _move(original, live)
        entry["phase"] = "undone"
        _persist(layout, document)
    _verify_rollback(layout, document)
    document["state"] = "rolled_back"
    _persist(layout, document)
    return RestoreOutcome("rolled_back", document["operation_id"], "restore_rolled_back", {})


def _initialize_transaction(layout, document, limits):
    operation = document["operation_id"]
    rescue = _op_path(layout, operation, "rescue.zip")
    _check(_file_sha(rescue) == document["rescue_sha256"])
    rescue_folder = _op_path(layout, operation, "rescue-validated")
    inspect_snapshot(rescue_folder, includes_photos=True, limits=limits)
    # Revalidate the rescue ZIP on each first application, including its actual
    # contents, not merely the prior unpacked directory supplied by Service.
    fresh = _op_path(layout, operation, "apply-rescue")
    if fresh.exists():
        _remove_owned(fresh, _identity(fresh), tree=True)
    validated = validate_archive(rescue, fresh, limits=limits, cancel=lambda: False,
                                 progress=lambda *args: None)
    _check(validated.archive_sha256 == document["rescue_sha256"])
    pending = _op_path(layout, operation, "pending")
    _check(_digest(pending) == document["pending_sha256"])
    inspect_snapshot(pending, includes_photos=document["includes_photos"], limits=limits)
    # Make every staged payload durable before a journal can authorize a rename.
    for file in pending.rglob("*"):
        file = _checked_path(file)
        if file.is_file():
            with file.open("r+b") as stream:
                os.fsync(stream.fileno())
    _read_epoch(layout)  # refuse a corrupt old epoch before changing anything
    document["new_epoch"] = str(uuid.uuid4())
    epoch_path = _op_path(layout, operation, "epoch.json")
    with epoch_path.open("wb") as stream:
        stream.write(json.dumps(dict(format_version=1, epoch=document["new_epoch"], require_header=True),
                                separators=(",", ":")).encode())
        stream.flush()
        os.fsync(stream.fileno())
    original = _op_path(layout, operation, "original")
    original.mkdir(exist_ok=True)
    _check(not list(original.iterdir()))
    document["entries"] = [dict(name=name, old=_digest(_runtime_target(layout, name)),
                               new=_digest(_stage(layout, document, name)), phase="queued")
                           for name in _names(document["includes_photos"])]
    document["state"] = "applying"


def apply_pending_restore(layout: BackupLayout, *, memory_path: Path,
                          limits: BackupLimits) -> RestoreOutcome:
    document, transaction_initialized, recovery_allowed = None, False, False
    try:
        document = _read_recovery_state(layout)
        if document is None or document["state"] == "cancelled":
            return RestoreOutcome("none", "", "", {})
        if document["state"] in {"committed", "rolled_back"}:
            status = "restored" if document["state"] == "committed" else "rolled_back"
            return RestoreOutcome(status, document["operation_id"],
                                  "" if status == "restored" else "restore_rolled_back",
                                  document["counts"] if status == "restored" else {})
        _limits(limits)
        check_memory_path(layout, memory_path)
        recovery_allowed = True
        if document["state"] in {"applying", "rolling_back"}:
            return _rollback(layout, document)
        # The flag distinguishes preflight tampering (blocked) from a genuine
        # first-intent disk failure after all payload/material checks passed.
        _initialize_transaction(layout, document, limits)
        transaction_initialized = True
        _persist(layout, document)
        for entry in document["entries"]:
            live, original, stage = _entry_paths(layout, document, entry)
            _check(_digest(live) == entry["old"] and _digest(stage) == entry["new"] and not original.exists())
            entry["phase"] = "intent"
            _persist(layout, document)
            if entry["old"] is not None:
                _move(live, original)
            if entry["new"] is not None:
                _move(stage, live)
            entry["phase"] = "done"
            _persist(layout, document)
        document["state"] = "committed"
        _persist(layout, document)
        return RestoreOutcome("restored", document["operation_id"], "", document["counts"])
    except (BackupError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
        if not recovery_allowed:
            return RestoreOutcome("blocked", document["operation_id"] if document else "", "restore_blocked", {})
        # Read disk again: an exception AFTER atomic commit publication is
        # success, not authority to roll a fully committed transaction back.
        try:
            persisted = _read_recovery_state(layout)
            if persisted and persisted["state"] == "committed":
                return RestoreOutcome("restored", persisted["operation_id"], "", persisted["counts"])
            if persisted and persisted["state"] in {"applying", "rolling_back"}:
                return _rollback(layout, persisted)
            if persisted and persisted["state"] == "prepared" and transaction_initialized:
                persisted.update(state="rolled_back", entries=[], new_epoch="")
                _persist(layout, persisted)
                return RestoreOutcome("rolled_back", persisted["operation_id"], "restore_rolled_back", {})
        except (BackupError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            pass
        return RestoreOutcome("blocked", document["operation_id"] if document else "", "restore_blocked", {})
