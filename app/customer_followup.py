"""Local customer follow-up notes and plans; never performs outreach."""

from __future__ import annotations

import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path


CHANNELS = {"email", "whatsapp", "phone", "other"}
OUTCOMES = {"awaiting_reply", "replied", "in_progress", "not_interested", "invalid", "note"}


class FollowupConflict(RuntimeError):
    """An operation no longer matches the saved state."""


class FollowupNotFound(RuntimeError):
    """The requested record does not belong to this customer."""


def _text(value: str, maximum: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{label}不能为空且最多 {maximum} 个字符")
    return value.strip()


def _date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("跟进日期格式应为 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("跟进日期无效") from exc
    return value


class FollowupStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS customer_followup_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id TEXT NOT NULL,
                    customer_name_snapshot TEXT NOT NULL,
                    contacted_at REAL NOT NULL,
                    channel TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    request_id TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_followup_record_customer_time
                    ON customer_followup_records(customer_id, contacted_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS customer_followup_deleted_requests (
                    request_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_followup_deleted_request_customer
                    ON customer_followup_deleted_requests(customer_id);
                CREATE TABLE IF NOT EXISTS customer_followup_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id TEXT NOT NULL,
                    customer_name_snapshot TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','completed','cancelled','superseded')),
                    previous_plan_id INTEGER,
                    created_at REAL NOT NULL,
                    closed_at REAL,
                    request_id TEXT NOT NULL UNIQUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_followup_active_customer
                    ON customer_followup_plans(customer_id) WHERE status='active';
                CREATE INDEX IF NOT EXISTS idx_followup_plan_customer
                    ON customer_followup_plans(customer_id, created_at DESC, id DESC);
            """)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_record(self, customer_id: str, customer_name: str, *, contacted_at: float,
                   channel: str, outcome: str, content: str, request_id: str) -> dict:
        customer_id = _text(customer_id, 200, "客户编号")
        customer_name = _text(customer_name, 500, "客户名称")
        content = _text(content, 4000, "沟通内容")
        request_id = _text(request_id, 128, "请求编号")
        if channel not in CHANNELS or outcome not in OUTCOMES:
            raise ValueError("沟通渠道或结果无效")
        if isinstance(contacted_at, bool) or not isinstance(contacted_at, (int, float)) or (
            not math.isfinite(contacted_at) or contacted_at <= 0 or contacted_at > time.time() + 300
        ):
            raise ValueError("沟通时间无效，不能记录尚未发生的沟通")
        values = (customer_id, float(contacted_at), channel, outcome, content)
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM customer_followup_deleted_requests WHERE request_id=?",
                            (request_id,)).fetchone():
                raise FollowupConflict("该沟通记录已被删除，请刷新后确认；重试不会重新创建记录")
            existing = conn.execute("SELECT * FROM customer_followup_records WHERE request_id=?",
                                    (request_id,)).fetchone()
            if existing:
                if tuple(existing[k] for k in ("customer_id", "contacted_at", "channel", "outcome", "content")) != values:
                    raise FollowupConflict("该请求已保存不同的沟通内容，请刷新后重试")
                return dict(existing)
            cursor = conn.execute("""
                INSERT INTO customer_followup_records
                    (customer_id,customer_name_snapshot,contacted_at,channel,outcome,content,created_at,request_id)
                VALUES (?,?,?,?,?,?,?,?)
            """, (customer_id, customer_name, float(contacted_at), channel, outcome, content, time.time(), request_id))
            return dict(conn.execute("SELECT * FROM customer_followup_records WHERE id=?",
                                     (cursor.lastrowid,)).fetchone())

    def delete_record(self, customer_id: str, record_id: int) -> None:
        with self._transaction() as conn:
            record = conn.execute("SELECT request_id FROM customer_followup_records WHERE id=? AND customer_id=?",
                                  (record_id, customer_id)).fetchone()
            if not record:
                raise FollowupNotFound("跟进记录不存在或不属于该客户")
            # Keep only identity metadata, never the deleted communication content.
            conn.execute("INSERT OR IGNORE INTO customer_followup_deleted_requests VALUES (?,?)",
                         (record["request_id"], customer_id))
            conn.execute("DELETE FROM customer_followup_records WHERE id=? AND customer_id=?",
                         (record_id, customer_id))

    def set_plan(self, customer_id: str, customer_name: str, *, due_date: str, action: str,
                 request_id: str, expected_plan_id: int | None) -> dict:
        customer_id = _text(customer_id, 200, "客户编号")
        customer_name = _text(customer_name, 500, "客户名称")
        action = _text(action, 500, "待办事项")
        request_id = _text(request_id, 128, "请求编号")
        due_date = _date(due_date)
        if expected_plan_id is not None and (
            isinstance(expected_plan_id, bool) or not isinstance(expected_plan_id, int) or expected_plan_id <= 0
        ):
            raise ValueError("原安排编号无效")
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM customer_followup_plans WHERE request_id=?",
                                    (request_id,)).fetchone()
            if existing:
                if (existing["customer_id"], existing["due_date"], existing["action"], existing["previous_plan_id"]) != (
                    customer_id, due_date, action, expected_plan_id
                ):
                    raise FollowupConflict("该请求已保存不同的安排，请刷新后重试")
                return dict(existing)
            current = conn.execute("SELECT id FROM customer_followup_plans WHERE customer_id=? AND status='active'",
                                   (customer_id,)).fetchone()
            current_id = current["id"] if current else None
            if current_id != expected_plan_id:
                raise FollowupConflict("跟进安排已被更改，请刷新后重试")
            now = time.time()
            if current:
                conn.execute("UPDATE customer_followup_plans SET status='superseded',closed_at=? WHERE id=?",
                             (now, current_id))
            cursor = conn.execute("""
                INSERT INTO customer_followup_plans
                    (customer_id,customer_name_snapshot,due_date,action,status,previous_plan_id,created_at,request_id)
                VALUES (?,?,?,?,'active',?,?,?)
            """, (customer_id, customer_name, due_date, action, current_id, now, request_id))
            return dict(conn.execute("SELECT * FROM customer_followup_plans WHERE id=?",
                                     (cursor.lastrowid,)).fetchone())

    def close_plan(self, customer_id: str, plan_id: int, status: str) -> dict:
        if status not in {"completed", "cancelled"}:
            raise ValueError("只能完成或取消安排")
        with self._transaction() as conn:
            current = conn.execute("SELECT * FROM customer_followup_plans WHERE id=? AND customer_id=?",
                                   (plan_id, customer_id)).fetchone()
            if not current:
                raise FollowupNotFound("跟进安排不存在或不属于该客户")
            if current["status"] == status:
                return dict(current)
            if current["status"] != "active":
                raise FollowupConflict("该安排已结束或被更改，请刷新后重试")
            conn.execute("UPDATE customer_followup_plans SET status=?,closed_at=? WHERE id=?",
                         (status, time.time(), plan_id))
            return dict(conn.execute("SELECT * FROM customer_followup_plans WHERE id=?", (plan_id,)).fetchone())

    def active_plan(self, customer_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM customer_followup_plans WHERE customer_id=? AND status='active'",
                               (customer_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def summaries(self, customer_ids: list[str], today: str) -> dict[str, dict]:
        _date(today)
        ids = list(dict.fromkeys(customer_ids))
        result = {cid: {"customer_id": cid, "plan": None, "due_status": "unplanned", "last_contact_at": None} for cid in ids}
        conn = self._connect()
        try:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                plans = conn.execute(f"SELECT * FROM customer_followup_plans WHERE status='active' AND customer_id IN ({placeholders})", batch)
                for row in plans:
                    item = result[row["customer_id"]]
                    item["plan"] = dict(row)
                    item["due_status"] = "overdue" if row["due_date"] < today else "today" if row["due_date"] == today else "upcoming"
                records = conn.execute(f"SELECT customer_id, MAX(contacted_at) AS last_at FROM customer_followup_records WHERE outcome<>'note' AND customer_id IN ({placeholders}) GROUP BY customer_id", batch)
                for row in records:
                    result[row["customer_id"]]["last_contact_at"] = row["last_at"]
            return result
        finally:
            conn.close()

    def timeline_items(self, customer_id: str) -> list[dict]:
        """Read only this customer's known local events, without a global cutoff."""
        conn = self._connect()
        try:
            items = []
            for row in conn.execute("SELECT * FROM customer_followup_records WHERE customer_id=?", (customer_id,)):
                items.append({"id": f"record:{row['id']}", "kind": "record", "occurred_at": row["contacted_at"],
                              "record_id": row["id"], "channel": row["channel"], "outcome": row["outcome"], "content": row["content"]})
            for row in conn.execute("""
                SELECT p.*, old.due_date AS previous_due_date
                FROM customer_followup_plans p LEFT JOIN customer_followup_plans old ON old.id=p.previous_plan_id
                WHERE p.customer_id=?
            """, (customer_id,)):
                common = {"plan_id": row["id"], "due_date": row["due_date"], "content": row["action"], "channel": "other"}
                items.append({**common, "id": f"plan:{row['id']}:created", "occurred_at": row["created_at"],
                              "kind": "plan_rescheduled" if row["previous_plan_id"] else "plan_created",
                              "previous_due_date": row["previous_due_date"]})
                if row["status"] in {"completed", "cancelled"}:
                    items.append({**common, "id": f"plan:{row['id']}:{row['status']}", "occurred_at": row["closed_at"],
                                  "kind": f"plan_{row['status']}"})
            return items
        finally:
            conn.close()

    def timeline(self, customer_id: str, *, limit: int = 50, offset: int = 0) -> dict:
        return paginate_timeline(self.timeline_items(customer_id), limit=limit, offset=offset)

    def delete_customers(self, customer_ids: list[str]) -> None:
        if not customer_ids:
            return
        with self._transaction() as conn:
            for start in range(0, len(customer_ids), 500):
                batch = customer_ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                conn.execute(f"DELETE FROM customer_followup_records WHERE customer_id IN ({placeholders})", batch)
                conn.execute(f"DELETE FROM customer_followup_deleted_requests WHERE customer_id IN ({placeholders})", batch)
                conn.execute(f"DELETE FROM customer_followup_plans WHERE customer_id IN ({placeholders})", batch)

    def prune_orphans(self, customer_ids: list[str]) -> None:
        with self._transaction() as conn:
            conn.execute("CREATE TEMP TABLE keep_followup_customers (id TEXT PRIMARY KEY)")
            conn.executemany("INSERT OR IGNORE INTO keep_followup_customers VALUES (?)", ((cid,) for cid in customer_ids))
            conn.execute("DELETE FROM customer_followup_records WHERE customer_id NOT IN (SELECT id FROM keep_followup_customers)")
            conn.execute("DELETE FROM customer_followup_deleted_requests WHERE customer_id NOT IN (SELECT id FROM keep_followup_customers)")
            conn.execute("DELETE FROM customer_followup_plans WHERE customer_id NOT IN (SELECT id FROM keep_followup_customers)")


def paginate_timeline(items: list[dict], *, limit: int = 50, offset: int = 0) -> dict:
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("时间线分页参数无效")
    ordered = sorted(items, key=lambda item: (item["occurred_at"], item["id"]), reverse=True)
    page = ordered[offset:offset + limit]
    return {"items": page, "has_more": offset + len(page) < len(ordered), "next_offset": offset + len(page)}
