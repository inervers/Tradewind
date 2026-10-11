"""Process-local business ownership and read-only persisted data epochs.

Tickets are reserved before starting workers; UI status has no bearing on
ownership. Exclusive leases belong to a coordinator generation, not a thread.
Task5 owns persisted restore metadata; this module never writes control files.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import threading
from typing import Callable, Iterator, Literal
import unicodedata
import uuid

from .models import BackupError, BackupLayout, EpochState
from .paths import owned_path


def _busy() -> BackupError:
    return BackupError("maintenance_busy", "业务操作或备份维护尚未结束，请稍后重试。")


def _invalid_epoch() -> BackupError:
    return BackupError("invalid_epoch_state", "数据版本文件无效，请保留本机资料并联系维护人员。")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_epoch(layout: BackupLayout) -> EpochState:
    checked = owned_path(layout, ".data-epoch.json")
    if layout.epoch_file.absolute() != checked:
        raise BackupError("unsafe_path", "备份路径不安全或不属于允许的业务范围。")
    try:
        stream = checked.open("rb")
    except FileNotFoundError:
        # Recheck even the absent-file route; never create bootstrap metadata.
        checked = owned_path(layout, ".data-epoch.json")
        root = unicodedata.normalize("NFC", str(checked.parent)).casefold()
        return EpochState(str(uuid.uuid5(uuid.NAMESPACE_URL, "tradewind-initial:" + root)), False)
    except OSError:
        raise _invalid_epoch() from None
    try:
        with stream:
            raw = stream.read(1025)
    except OSError:
        raise _invalid_epoch() from None
    owned_path(layout, ".data-epoch.json")
    try:
        if len(raw) > 1024:
            raise ValueError("oversized epoch")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (type(document) is not dict
                or set(document) != {"format_version", "epoch", "require_header"}
                or type(document["format_version"]) is not int
                or document["format_version"] != 1
                or type(document["epoch"]) is not str
                or type(document["require_header"]) is not bool
                or str(uuid.UUID(document["epoch"])) != document["epoch"]):
            raise ValueError("invalid epoch schema")
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid_epoch() from None
    return EpochState(document["epoch"], document["require_header"])


class MaintenanceCoordinator:
    def __init__(self, layout: BackupLayout) -> None:
        # Invalid metadata refuses construction before any lease can exist.
        self._epoch = _read_epoch(layout)
        self._lock = threading.Lock()
        self._revision = 0
        self._business = 0
        self._generation = 0
        self._exclusive: MaintenanceLease | None = None
        self._prepared: str | None = None

    @property
    def mode(self) -> Literal["ready", "snapshot", "prepare", "prepared"]:
        with self._lock:
            if self._prepared is not None:
                return "prepared"
            return self._exclusive._kind if self._exclusive is not None else "ready"

    @property
    def prepared_operation_id(self) -> str | None:
        with self._lock:
            return self._prepared

    def version(self) -> tuple[EpochState, int]:
        with self._lock:
            return self._epoch, self._revision

    def _admit_business_locked(self, mutating: bool) -> None:
        if self._exclusive is not None or self._prepared is not None:
            raise _busy()
        self._business += 1
        if mutating:
            self._revision += 1

    def _finish_business_locked(self, mutating: bool) -> None:
        if mutating:
            self._revision += 1
        self._business -= 1

    @contextmanager
    def service(self, epoch: str | None, *, mutating: bool) -> Iterator[None]:
        with self._lock:
            if mutating and ((epoch is None and self._epoch.require_header)
                             or (epoch is not None and epoch != self._epoch.epoch)):
                raise BackupError("stale_epoch", "数据版本已变化，请复制未保存内容并重新打开页面。")
            self._admit_business_locked(mutating)
        try:
            yield
        finally:
            with self._lock:
                self._finish_business_locked(mutating)

    def reserve_worker(self, kind: str, *, mutating: bool) -> WorkerTicket:
        with self._lock:
            ticket = WorkerTicket(self, kind, mutating=mutating)
            self._admit_business_locked(mutating)
            return ticket

    def reserve_exclusive(self, kind: Literal["snapshot", "prepare"]) -> MaintenanceLease:
        if kind not in {"snapshot", "prepare"}:
            raise ValueError("unknown maintenance kind")
        with self._lock:
            if self._business or self._exclusive is not None or self._prepared is not None:
                raise _busy()
            self._generation += 1
            lease = MaintenanceLease(self, self._generation, kind)
            self._exclusive = lease
            # Neither reservation changes business data. In particular,
            # prepare must still be able to validate its bound preview.
            return lease

    @contextmanager
    def exclusive(self, kind: Literal["snapshot", "prepare"]) -> Iterator[MaintenanceLease]:
        lease = self.reserve_exclusive(kind)
        try:
            yield lease
        finally:
            lease.release()

    def _assert_lease_locked(self, lease: MaintenanceLease) -> None:
        if (lease._coordinator is not self or lease._released
                or lease._generation != self._generation or self._exclusive is not lease):
            raise _busy()

    def hold_prepared(self, operation_id: str) -> None:
        with self._lock:
            if self._prepared == operation_id and self._prepared is not None:
                return
            if (not operation_id or self._prepared is not None
                    or self._exclusive is None or self._exclusive._kind != "prepare"):
                raise _busy()
            self._prepared = operation_id

    def cancel_prepared(self, operation_id: str) -> None:
        with self._lock:
            if self._prepared is None:
                return
            if self._prepared != operation_id:
                raise _busy()
            # Task5 must durably cancel its marker before calling this method.
            self._prepared = None


class WorkerTicket:
    """One registered worker; aborted/finished/repeated runs are harmless."""

    def __init__(self, coordinator: MaintenanceCoordinator, kind: str, *, mutating: bool) -> None:
        self._coordinator = coordinator
        self._kind = kind
        self._mutating = mutating
        self._state = "reserved"

    def _finish_locked(self) -> None:
        self._state = "released"
        self._coordinator._finish_business_locked(self._mutating)

    def run(self, target: Callable[..., None], args: tuple = ()) -> None:
        with self._coordinator._lock:
            if self._state != "reserved":
                return
            self._state = "running"
        try:
            target(*args)
        finally:
            with self._coordinator._lock:
                self._finish_locked()

    def abort_start(self) -> None:
        with self._coordinator._lock:
            # In particular, never release a target that is already running.
            if self._state == "reserved":
                self._finish_locked()


class MaintenanceLease:
    """Transferable exclusive ownership, checked by identity and generation."""

    def __init__(self, coordinator: MaintenanceCoordinator, generation: int,
                 kind: Literal["snapshot", "prepare"]) -> None:
        self._coordinator = coordinator
        self._generation = generation
        self._kind = kind
        self._released = False

    def assert_active(self) -> None:
        with self._coordinator._lock:
            self._coordinator._assert_lease_locked(self)

    def release(self) -> None:
        with self._coordinator._lock:
            if self._released:
                return
            self._coordinator._assert_lease_locked(self)
            self._coordinator._exclusive = None
            self._released = True
            # The independent prepared hold deliberately survives this release.
