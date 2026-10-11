"""Lease-bound business capture and bounded ZIP export from captured data only.

The caller owns both lease release and successful artifact lifetimes. Control
paths are never a deletion capability: cleanup requires exclusive creation and
the same filesystem identity, with links rechecked before each operation.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid
import zipfile

from .maintenance import MaintenanceLease
from .models import (BackupCancelled, BackupError, BackupLayout, BackupLimits,
                     CancelCheck, ProgressSink, Snapshot)
from .paths import (BUSINESS_FILES, PHOTO_DIRECTORIES, _checked_path,
                    check_memory_path, member_path_key, owned_path,
                    require_space, resolve_layout, validate_member_path)
from .validation import _file_limit, _limits, _manifest, inspect_snapshot

_CHUNK = 1024 * 1024


def _error(code="archive_failed"):
    return BackupError(code, {
        "archive_failed": "无法完成本机备份，请检查磁盘和目录权限。",
        "source_changed": "业务资料在备份期间发生变化，请停止相关操作后重试。",
        "resource_limit": "备份超过本版本允许的资源上限。",
        "destination_exists": "备份目标必须是本次操作新建的专属路径。",
        "unsafe_path": "备份路径不安全或不属于允许的业务范围。",
        "invalid_backup": "快照内容损坏或与备份摘要不一致。",
    }[code])


def _check(condition, code="invalid_backup"):
    if not condition:
        raise _error(code)


def _cancel(cancel):
    if cancel():
        raise BackupCancelled()


def _identity(path):
    info = _checked_path(path).lstat()
    return info.st_dev, info.st_ino


def _state(path):
    try:
        info = _checked_path(path).lstat()
    except FileNotFoundError:
        return None
    _check(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), "unsafe_path")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _remove_owned(path, identity, *, tree=False):
    """No caller-supplied existing target, substituted root or link is removed."""
    path = _checked_path(path)
    if not path.exists():
        return
    _check(_identity(path) == identity, "unsafe_path")
    if tree:
        # Check the entire tree before deleting any of it. In particular, never
        # follow a Windows junction substituted inside our operation folder.
        pending, files, directories = [path], [], []
        while pending:
            current = _checked_path(pending.pop())
            directories.append((current, _identity(current)))
            for child in current.iterdir():
                child = _checked_path(child)
                if child.is_dir():
                    pending.append(child)
                else:
                    _check(child.is_file(), "unsafe_path")
                    files.append((child, _identity(child)))
        for child, token in files:
            _check(_identity(child) == token, "unsafe_path")
            _checked_path(child).unlink()
        for child, token in reversed(directories):
            _check(_identity(child) == token, "unsafe_path")
            _checked_path(child).rmdir()
    else:
        _checked_path(path).unlink()


def _inventory(layout, photos, limits, cancel):
    """Only the white list is observed; excluded runtime files are not opened."""
    states, files, directories, aliases = {}, [], [], {}
    total, path_bytes = 0, 0

    def record(relative):
        nonlocal total, path_bytes
        _cancel(cancel)
        name = validate_member_path("data/" + relative)
        key = member_path_key(name)
        _check(key not in aliases, "unsafe_path")
        aliases[key] = name
        path = owned_path(layout, relative)
        info = _state(path)
        states[relative] = info
        if info is None:
            return
        path_bytes += len(name.encode("utf-8"))
        _check(path_bytes <= limits.max_manifest_bytes, "resource_limit")
        if stat.S_ISDIR(info[2]):
            _check(relative.split("/")[0] in PHOTO_DIRECTORIES, "unsafe_path")
            directories.append(relative)
            for child in path.iterdir():
                record(child.relative_to(layout.data_dir).as_posix())
        else:
            _check(info[3] <= _file_limit(name, limits), "resource_limit")
            files.append(relative)
            total += info[3]
            _check(len(files) + 1 <= limits.max_files and
                   total <= limits.max_unpacked_bytes, "resource_limit")

    for relative in sorted(BUSINESS_FILES):
        record(relative)
    if photos:
        for relative in sorted(PHOTO_DIRECTORIES):
            record(relative)
    # WAL/journal state is part of consistency, not archive membership. SHM
    # contains reader locks; the RO SQLite API may change those legitimately.
    memory = owned_path(layout, "tradewind_memory.db")
    for suffix in ("-wal", "-journal"):
        states["tradewind_memory.db" + suffix] = _state(Path(str(memory) + suffix))
    return states, sorted(files), sorted(directories), total


def _chunks(path, cancel):
    with _checked_path(path).open("rb") as source:
        while True:
            _cancel(cancel)
            chunk = source.read(_CHUNK)
            if not chunk:
                return
            yield chunk


def _copy(source, target, ceiling, cancel, progress, completed, total):
    size = 0
    with _checked_path(target).open("xb") as sink:
        for chunk in _chunks(source, cancel):
            size += len(chunk)
            _check(size <= ceiling, "resource_limit")
            sink.write(chunk)
            progress("capture", completed + size, total)
    return size


def _sqlite_capture(source, target, limits, lease, cancel, progress, completed, total):
    # immutable=1 is intentionally absent: committed WAL rows belong to the
    # backup. Neither checkpoint nor VACUUM ever runs against the source.
    deadline = time.monotonic() + 30
    def copying(status, remaining, pages):
        lease.assert_active()
        _cancel(cancel)
        _check(time.monotonic() <= deadline, "source_changed")
        _check(_checked_path(target).stat().st_size <= limits.max_db_bytes, "resource_limit")
        progress("capture", min(total, completed + (pages - remaining) * 4096), total)

    with closing(sqlite3.connect(_checked_path(source).as_uri() + "?mode=ro", uri=True)) as reader, \
            closing(sqlite3.connect(_checked_path(target))) as writer:
        reader.backup(writer, pages=256, progress=copying, sleep=0.01)
        _cancel(cancel)
        writer.execute("PRAGMA journal_mode=DELETE")
        interrupted = False
        def vacuum_budget():
            nonlocal interrupted
            interrupted = bool(cancel())
            return int(interrupted or time.monotonic() > deadline)
        writer.set_progress_handler(vacuum_budget, 1000)
        try:
            writer.execute("VACUUM")
        except sqlite3.Error:
            if interrupted:
                raise BackupCancelled() from None
            raise
    _check(_checked_path(target).stat().st_size <= limits.max_db_bytes, "resource_limit")


def capture_snapshot(layout: BackupLayout, *, memory_path: Path, includes_photos: bool,
                     destination: Path, limits: BackupLimits, lease: MaintenanceLease,
                     cancel: CancelCheck, progress: ProgressSink) -> Snapshot:
    """Capture under the passed snapshot OR prepare lease; never reserve/release."""
    created = None
    destination = Path(destination)
    before = None
    try:
        _limits(limits)
        lease.assert_active()
        _cancel(cancel)
        _check(type(includes_photos) is bool)
        check_memory_path(layout, Path(memory_path))
        destination = _checked_path(destination)
        source_root = _checked_path(layout.data_dir)
        _check(not source_root.is_relative_to(destination), "unsafe_path")
        if destination.is_relative_to(source_root):
            # Only strict descendants of program-derived control directories
            # are admissible within live data, never the control roots themselves.
            relative = destination.relative_to(source_root).as_posix()
            _check(len(relative.split("/")) > 1 and
                   relative.split("/")[0] in {".backup-work", "backups"}, "unsafe_path")
            destination = owned_path(layout, relative)
        if destination.exists():
            raise _error("destination_exists")
        before, files, directories, estimated = _inventory(layout, includes_photos, limits, cancel)
        # Backup and VACUUM can each require a DB-sized scratch copy.
        db_size = before["tradewind_memory.db"][3] if before["tradewind_memory.db"] else 0
        wal_size = before["tradewind_memory.db-wal"][3] if before["tradewind_memory.db-wal"] else 0
        require_space(destination, estimated + 2 * (db_size + wal_size), limits)
        lease.assert_active()
        _cancel(cancel)
        _checked_path(destination).mkdir()
        created = _identity(destination)
        captured = resolve_layout(destination)
        owned_data = _checked_path(captured.data_dir)
        owned_data.mkdir()
        for relative in directories:
            _cancel(cancel)
            owned_path(captured, relative).mkdir(parents=True, exist_ok=True)
        completed = 0
        for relative in files:
            lease.assert_active()
            _cancel(cancel)
            source = owned_path(layout, relative)
            _check(_state(source) == before[relative], "source_changed")
            target = owned_path(captured, relative)
            if relative == "tradewind_memory.db":
                _sqlite_capture(source, target, limits, lease, cancel, progress, completed, estimated)
                size = _checked_path(target).stat().st_size
            else:
                size = _copy(source, target, _file_limit("data/" + relative, limits),
                             cancel, progress, completed, estimated)
            completed += size
            _check(completed <= limits.max_unpacked_bytes, "resource_limit")
        _cancel(cancel)
        _check(before == _inventory(layout, includes_photos, limits, cancel)[0], "source_changed")
        summary = inspect_snapshot(destination, includes_photos=includes_photos, limits=limits)
        progress("capture", completed, completed)
        lease.assert_active()
        _cancel(cancel)
        _check(before == _inventory(layout, includes_photos, limits, cancel)[0], "source_changed")
        _check(_identity(destination) == created, "unsafe_path")
        result = Snapshot(destination, summary)
        created = None  # successful capture transfers artifact lifetime to caller
        return result
    except FileExistsError:
        raise _error("destination_exists") from None
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
        if before is not None:
            try:
                changed = before != _inventory(layout, includes_photos, limits, lambda: False)[0]
            except (OSError, BackupError):
                # A second failing diagnostic must not leak an OS path/error.
                changed = False
            if changed:
                raise _error("source_changed") from None
        raise _error() from None
    finally:
        if created is not None:
            try:
                _remove_owned(destination, created, tree=True)
            except OSError:
                raise _error() from None


class _ArchiveSink:
    """Count high-water output, including ZIP headers and central directory."""
    def __init__(self, stream, maximum, cancel):
        self.stream, self.maximum, self.cancel = stream, maximum, cancel

    def write(self, data):
        _cancel(self.cancel)
        _check(self.stream.tell() + len(data) <= self.maximum, "resource_limit")
        return self.stream.write(data)

    def tell(self):
        return self.stream.tell()

    def seek(self, *args):
        return self.stream.seek(*args)

    def flush(self):
        return self.stream.flush()


def write_archive(snapshot: Snapshot, destination: Path, *, limits: BackupLimits,
                  cancel: CancelCheck, progress: ProgressSink) -> Path:
    """Stream immutable captured files; never read the live business directory."""
    temporary, created = None, None
    try:
        _limits(limits)
        _cancel(cancel)
        folder = _checked_path(snapshot.folder)
        destination = _checked_path(Path(destination))
        _check(not destination.is_relative_to(folder) and not folder.is_relative_to(destination), "unsafe_path")
        if destination.exists():
            raise _error("destination_exists")
        layout = resolve_layout(folder)
        states, files, directories, estimated = _inventory(layout, snapshot.summary.includes_photos, limits, cancel)
        summary = inspect_snapshot(folder, includes_photos=snapshot.summary.includes_photos, limits=limits)
        _check(states == _inventory(layout, snapshot.summary.includes_photos, limits, cancel)[0], "source_changed")
        _check(summary.items_present == snapshot.summary.items_present and
               summary.counts == snapshot.summary.counts)
        manifest = asdict(snapshot.summary)
        entries = []
        for relative in files:
            size, digest = 0, hashlib.sha256()
            for chunk in _chunks(owned_path(layout, relative), cancel):
                size += len(chunk)
                _check(size <= _file_limit("data/" + relative, limits), "resource_limit")
                digest.update(chunk)
            _check(size == states[relative][3], "source_changed")
            entries.append(dict(path="data/" + relative, size=size, sha256=digest.hexdigest()))
        manifest.update(files=entries, directories=["data/" + name for name in directories])
        _manifest(manifest, limits)
        content = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _check(len(content) <= limits.max_manifest_bytes and
               estimated + len(content) <= limits.max_unpacked_bytes, "resource_limit")
        # Conservative DEFLATE bound, names, local+central records, ZIP64 locals.
        budget = estimated + estimated // 1000 + len(content) + 1024 + sum(
            256 + 2 * len(entry["path"].encode("utf-8")) for entry in entries)
        require_space(destination, budget, limits)
        _cancel(cancel)
        temporary = _checked_path(destination.with_name(".archive-" + uuid.uuid4().hex + ".tmp"))
        completed = 0
        with temporary.open("xb") as output:
            created = _identity(temporary)
            sink = _ArchiveSink(output, limits.max_archive_bytes, cancel)
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                zipped.writestr("manifest.json", content)
                for entry in entries:
                    _cancel(cancel)
                    path = owned_path(layout, entry["path"].removeprefix("data/"))
                    size, digest = 0, hashlib.sha256()
                    info = zipfile.ZipInfo(entry["path"])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.file_size = entry["size"]
                    with zipped.open(info, "w", force_zip64=True) as target:
                        for chunk in _chunks(path, cancel):
                            size += len(chunk)
                            _check(size <= entry["size"], "source_changed")
                            digest.update(chunk)
                            target.write(chunk)
                            completed += len(chunk)
                            progress("compress", completed, estimated)
                    _check(size == entry["size"] and digest.hexdigest() == entry["sha256"], "source_changed")
            output.flush()
            os.fsync(output.fileno())
        _check(states == _inventory(layout, snapshot.summary.includes_photos, limits, cancel)[0], "source_changed")
        progress("compress", completed, completed)
        _cancel(cancel)
        _check(_identity(temporary) == created, "unsafe_path")
        # Windows rename is atomic and refuses existing files, including on
        # removable volumes without hardlinks. POSIX rename clobbers; use a
        # no-clobber hardlink there, then remove only our own temporary name.
        if os.name == "nt":
            os.rename(_checked_path(temporary), _checked_path(destination))
            created = None
        else:
            os.link(_checked_path(temporary), _checked_path(destination))
        return destination
    except FileExistsError:
        raise _error("destination_exists") from None
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, TypeError,
            OverflowError, RecursionError):
        raise _error() from None
    finally:
        if created is not None:
            try:
                _remove_owned(temporary, created)
            except OSError:
                raise _error() from None
