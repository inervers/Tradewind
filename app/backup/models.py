"""Local backup contracts; importing these types never opens runtime data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal


CancelCheck = Callable[[], bool]
ProgressSink = Callable[[str, int, int], None]

FORMAT_VERSION = 1
COUNT_KEYS = (
    "customers", "products", "templates", "crawler_seen", "emails",
    "outreach_jobs", "suppressions", "followup_records",
    "followup_deleted_requests", "followup_plans", "active_plans", "photos",
)
ITEM_KEYS = (
    "customers", "products", "templates", "memory", "crawler_seen",
    "crawler_photos", "photo_scan",
)


class BackupError(Exception):
    """A stable error code and a safe, user-facing Chinese explanation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class BackupCancelled(BackupError):
    def __init__(self) -> None:
        super().__init__("cancelled", "备份操作已取消。")


@dataclass(frozen=True)
class BackupLimits:
    max_archive_bytes: int = 2 * 1024**3
    max_unpacked_bytes: int = 8 * 1024**3
    max_files: int = 20_000
    max_photo_bytes: int = 20 * 1024**2
    max_json_bytes: int = 32 * 1024**2
    max_db_bytes: int = 512 * 1024**2
    max_manifest_bytes: int = 8 * 1024**2
    space_margin_bytes: int = 256 * 1024**2


@dataclass(frozen=True)
class BackupLayout:
    data_dir: Path
    work_dir: Path
    rescue_dir: Path
    epoch_file: Path


@dataclass(frozen=True)
class BackupSummary:
    format_version: int
    app_version: str
    created_at: str
    includes_photos: bool
    items_present: dict[str, bool]
    counts: dict[str, int]


@dataclass(frozen=True)
class Snapshot:
    folder: Path
    summary: BackupSummary


@dataclass(frozen=True)
class ValidatedArchive:
    snapshot: Snapshot
    archive_sha256: str


@dataclass(frozen=True)
class EpochState:
    epoch: str
    require_header: bool


@dataclass(frozen=True)
class RestorePreview:
    id: str
    archive_sha256: str
    epoch: str
    revision: int
    expires_at: float
    backup: BackupSummary
    current: BackupSummary


@dataclass(frozen=True)
class PreparedRestore:
    operation_id: str
    rescue_id: str
    status: Literal["prepared"]


@dataclass(frozen=True)
class RestoreOutcome:
    status: Literal["none", "restored", "rolled_back", "blocked"]
    operation_id: str
    code: str
    counts: dict[str, int]


@dataclass(frozen=True)
class OperationView:
    id: str
    kind: Literal["export", "preview", "prepare"]
    status: Literal["running", "done", "cancelled", "error"]
    phase: str
    completed: int
    total: int
    code: str
    message: str
    preview: RestorePreview | None
    prepared: PreparedRestore | None


@dataclass(frozen=True)
class RescueView:
    id: str
    created_at: str
    size_bytes: int
    active: bool


@dataclass(frozen=True)
class BackupState:
    mode: Literal["ready", "snapshot", "prepare", "prepared", "blocked"]
    epoch: str
    require_header: bool
    revision: int
    prepared: PreparedRestore | None
    rescues: list[RescueView]
    report: RestoreOutcome
    estimated_bytes: int
    photo_count: int
    current_preview: OperationView | None = None
