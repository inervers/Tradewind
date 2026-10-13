"""Windows-safe names and checked paths, without creating or deleting files.

Archive names include ``data/``; owned_path names are relative to data_dir.
The internal namespaces are trusted program control paths, never ZIP members
or authorization to delete arbitrary directories. Callers must recheck using
owned_path immediately before each operation and keep their own ID registry.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
import shutil
import stat
import unicodedata
import uuid

from .models import BackupError, BackupLayout, BackupLimits


BUSINESS_FILES = frozenset({
    "customers.json", "products.json", "emails.json", "crawler_seen.json",
    "tradewind_memory.db",
})
PHOTO_DIRECTORIES = frozenset({"crawler_photos", "photo_scan"})
_CONTROL_DIRECTORIES = frozenset({".backup-work", "backups"})
_DEVICE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"),
})


def _invalid_path() -> BackupError:
    return BackupError("unsafe_path", "备份路径不安全或不属于允许的业务范围。")


def _parts(name: str) -> tuple[str, ...]:
    # Do not let pathlib silently collapse empty, dot, or Windows components.
    if not isinstance(name, str) or not name or "\\" in name:
        raise _invalid_path()
    windows = PureWindowsPath(name)
    if windows.drive or windows.root:
        raise _invalid_path()
    parts = tuple(name.split("/"))
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or any(char in ':<>"|?*' or unicodedata.category(char) in {"Cc", "Cs"}
                       for char in part)
                or part.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES):
            raise _invalid_path()
        if len(part.encode("utf-16-le")) // 2 > 255:
            raise _invalid_path()
    return parts


def _business_path(parts: tuple[str, ...]) -> bool:
    return ((len(parts) == 1 and parts[0] in BUSINESS_FILES)
            or parts[0] in PHOTO_DIRECTORIES)


def validate_member_path(name: str) -> str:
    """Validate a manifest/file/empty-photo-directory name without renaming it.

    File-vs-directory and photo content/extension checks belong to the archive
    validator. This function admits only the archive's fixed business namespace.
    """
    parts = _parts(name)
    if parts == ("manifest.json",):
        return name
    if len(parts) < 2 or parts[0] != "data" or not _business_path(parts[1:]):
        raise _invalid_path()
    return name


def member_path_key(name: str) -> str:
    """Comparison key for detecting case/NFC aliases; never a renamed target."""
    return unicodedata.normalize("NFC", validate_member_path(name)).casefold()


def _check_ancestors(path: Path) -> None:
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise BackupError("path_unavailable", "无法安全检查备份路径，请检查本机目录权限。") from None
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise _invalid_path()
        if ancestor != path and not stat.S_ISDIR(info.st_mode):
            raise _invalid_path()


def _checked_path(path: Path) -> Path:
    absolute = path.absolute()
    # Do not require the target machine or legacy business readers to opt in
    # to extended Win32 paths. The terminating NUL occupies the 260th unit.
    try:
        units = len(str(absolute).encode("utf-16-le")) // 2
    except UnicodeError:
        raise _invalid_path() from None
    if units > 259:
        raise BackupError("path_too_long", "备份路径过长，请缩短本机目录或照片名称。")
    _check_ancestors(absolute)
    try:
        resolved = absolute.resolve(strict=False)
    except (OSError, RuntimeError):
        raise BackupError("path_unavailable", "无法安全检查备份路径，请检查本机目录权限。") from None
    # Check the lexical route again: resolve() alone would hide a junction.
    _check_ancestors(absolute)
    _check_ancestors(resolved)
    return resolved


def resolve_layout(root: Path) -> BackupLayout:
    """Resolve fixed internal locations, checking links but never mkdir/open."""
    root = _checked_path(root)
    data_dir = _checked_path(root / "data")
    return BackupLayout(
        data_dir=data_dir,
        work_dir=_checked_path(data_dir / ".backup-work"),
        rescue_dir=_checked_path(data_dir / "backups"),
        epoch_file=_checked_path(data_dir / ".data-epoch.json"),
    )


def owned_path(layout: BackupLayout, relative: str) -> Path:
    """Recheck a business or program control path beneath this layout's data_dir.

    ``relative`` must be program-derived for control namespaces. It is not a
    cleanup capability and must never be taken from uploaded ZIP target names.
    """
    parts = _parts(relative)
    deletion = parts == (".backup-deletions",)
    if len(parts) == 2 and parts[0] == ".backup-deletions" and parts[1].endswith((".json", ".tmp")):
        stem = parts[1].rsplit(".", 1)[0]
        try:
            deletion = str(uuid.UUID(stem)) == stem and uuid.UUID(stem).version == 4
        except ValueError:
            pass
    if not (_business_path(parts) or parts[0] in _CONTROL_DIRECTORIES
            or parts == (".data-epoch.json",) or deletion):
        raise _invalid_path()
    data_dir = _checked_path(layout.data_dir)
    target = _checked_path(data_dir.joinpath(*parts))
    if not target.is_relative_to(data_dir) or target == data_dir:
        raise _invalid_path()
    _check_ancestors(layout.data_dir.absolute())
    _check_ancestors(target)
    return target


def check_memory_path(layout: BackupLayout, configured: Path) -> None:
    """Refuse custom memory databases, including another file in the same dir."""
    expected = owned_path(layout, "tradewind_memory.db")
    if configured.absolute() != expected:
        raise BackupError("custom_memory_path", "当前使用自定义历史库，请关闭程序后人工备份。")
    if _checked_path(configured) != expected:
        raise _invalid_path()


def require_space(path: Path, additional_bytes: int, limits: BackupLimits) -> None:
    """Require the full additional budget plus margin on the target volume."""
    if (type(additional_bytes) is not int or additional_bytes < 0
            or type(limits.space_margin_bytes) is not int or limits.space_margin_bytes < 0):
        raise BackupError("invalid_space_budget", "备份空间预算无效。")
    checked = _checked_path(path)
    # disk_usage needs an existing path, but estimating must not create one.
    for candidate in (checked, *checked.parents):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise BackupError("path_unavailable", "无法检查本机备份可用空间。") from None
        _check_ancestors(candidate)
        try:
            free = shutil.disk_usage(candidate)[2]
        except OSError:
            raise BackupError("path_unavailable", "无法检查本机备份可用空间。") from None
        if free < additional_bytes + limits.space_margin_bytes:
            raise BackupError("insufficient_space", "本机磁盘空间不足，请保留备份所需空间及安全余量。")
        return
    raise BackupError("path_unavailable", "无法检查本机备份可用空间。")
