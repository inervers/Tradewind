"""Persistent outreach queue and state machine backed by local SQLite."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid
from pathlib import Path


class OutreachError(RuntimeError):
    """Base error for safe outreach workflow failures."""


class OutreachConflict(OutreachError):
    """The requested action conflicts with the current durable state."""


class OutreachNotFound(OutreachError):
    """The requested outreach record does not exist."""


class InvalidTransition(OutreachError):
    """The requested state transition is not allowed."""


class SuppressedContact(OutreachError):
    """The recipient is on the do-not-contact list."""


ACTIVE_STATUSES = {"pending_review", "opened", "sending", "unknown", "failed"}
EMAIL_PROVIDERS = {"qq_smtp", "gmail_smtp"}
ALLOWED_TRANSITIONS = {
    "pending_review": {"opened", "sending", "invalid", "skipped", "suppressed"},
    "opened": {"pending_review", "sent_manual", "invalid", "skipped", "suppressed"},
    "sending": {"accepted", "failed", "unknown"},
    "failed": {"pending_review", "sending", "skipped", "suppressed"},
    "unknown": {"accepted", "failed", "skipped", "suppressed"},
    "accepted": {"replied", "suppressed"},
    "sent_manual": {"replied", "suppressed"},
    "replied": {"suppressed"},
    "invalid": set(),
    "skipped": set(),
    "suppressed": set(),
}


def normalize_recipient(channel: str, recipient: str) -> str:
    if channel == "email":
        value = recipient.strip().casefold()
    elif channel == "whatsapp":
        value = re.sub(r"\D", "", recipient)
    else:
        raise ValueError("不支持的外联渠道")
    if not value:
        raise ValueError("联系方式不能为空")
    return value


def mask_recipient(channel: str, recipient: str) -> str:
    if channel == "email" and "@" in recipient:
        local, domain = recipient.split("@", 1)
        return f"{local[:1]}***@{domain}"
    if len(recipient) <= 5:
        return "*" * len(recipient)
    return f"{recipient[:3]}****{recipient[-2:]}"


class OutreachStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        conn = self._connect()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=3.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 3000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outreach_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT NOT NULL,
                customer_name_snapshot TEXT NOT NULL,
                channel TEXT NOT NULL CHECK(channel IN ('whatsapp', 'email')),
                provider TEXT NOT NULL,
                sender_account_snapshot TEXT DEFAULT '',
                recipient TEXT NOT NULL,
                subject TEXT DEFAULT '',
                body TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                message_id TEXT DEFAULT '',
                status TEXT NOT NULL,
                provider_reference TEXT DEFAULT '',
                error_code TEXT DEFAULT '',
                error_summary TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                opened_at REAL,
                submitted_at REAL,
                replied_at REAL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_active_customer_channel
            ON outreach_jobs(customer_id, channel)
            WHERE status IN ('pending_review', 'opened', 'sending', 'unknown', 'failed');

            CREATE TABLE IF NOT EXISTS contact_suppressions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                normalized_recipient TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(channel, normalized_recipient)
            );
            """
        )
        conn.commit()
        return conn

    @staticmethod
    def _content_hash(subject: str, body: str) -> str:
        return hashlib.sha256(f"{subject}\0{body}".encode("utf-8")).hexdigest()

    @staticmethod
    def _job(row: sqlite3.Row) -> dict:
        return dict(row)

    @staticmethod
    def _suppression(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["masked_recipient"] = mask_recipient(
            item["channel"], item["normalized_recipient"]
        )
        return item

    def _is_suppressed(self, conn: sqlite3.Connection, channel: str, recipient: str) -> bool:
        normalized = normalize_recipient(channel, recipient)
        row = conn.execute(
            "SELECT 1 FROM contact_suppressions WHERE channel=? AND normalized_recipient=?",
            (channel, normalized),
        ).fetchone()
        return row is not None

    def create_job(
        self,
        *,
        customer_id: str,
        customer_name: str,
        channel: str,
        provider: str,
        sender_account: str,
        recipient: str,
        subject: str,
        body: str,
    ) -> dict:
        if channel not in {"email", "whatsapp"}:
            raise ValueError("不支持的外联渠道")
        if (channel == "email" and provider not in EMAIL_PROVIDERS) or (
            channel == "whatsapp" and provider != "whatsapp_link"
        ):
            raise ValueError("渠道与发送方式不匹配")
        customer_id = customer_id.strip()
        customer_name = customer_name.strip()
        body = body.strip()
        subject = subject.strip()
        if not customer_id or not customer_name or not body:
            raise ValueError("客户和外联内容不能为空")
        normalized_recipient = normalize_recipient(channel, recipient)
        now = time.time()
        conn = self._connect()
        try:
            if self._is_suppressed(conn, channel, normalized_recipient):
                raise SuppressedContact("该联系方式已加入禁止联系名单")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO outreach_jobs (
                        customer_id, customer_name_snapshot, channel, provider,
                        sender_account_snapshot, recipient, subject, body,
                        content_hash, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?)
                    """,
                    (
                        customer_id,
                        customer_name,
                        channel,
                        provider,
                        sender_account.strip().casefold(),
                        normalized_recipient,
                        subject,
                        body,
                        self._content_hash(subject, body),
                        now,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise OutreachConflict("该客户已有待处理的同渠道外联任务") from exc
            job_id = int(cursor.lastrowid)
        finally:
            conn.close()
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> dict:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM outreach_jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise OutreachNotFound("外联任务不存在")
        return self._job(row)

    def list_jobs(
        self,
        channel: str | None = None,
        statuses: set[str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if channel:
            if channel not in {"email", "whatsapp"}:
                raise ValueError("不支持的外联渠道")
            clauses.append("channel=?")
            params.append(channel)
        if statuses:
            unknown = set(statuses) - set(ALLOWED_TRANSITIONS)
            if unknown:
                raise ValueError("包含未知外联状态")
            ordered = sorted(statuses)
            clauses.append(f"status IN ({','.join('?' for _ in ordered)})")
            params.extend(ordered)
        sql = "SELECT * FROM outreach_jobs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._job(row) for row in rows]

    def update_content(self, job_id: int, subject: str, body: str) -> dict:
        current = self.get_job(job_id)
        if current["status"] not in {"pending_review", "opened", "failed"}:
            raise OutreachConflict("当前状态不能修改内容")
        body = body.strip()
        subject = subject.strip()
        if not body:
            raise ValueError("外联正文不能为空")
        status = (
            "pending_review"
            if current["status"] in {"opened", "failed"}
            else current["status"]
        )
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE outreach_jobs
                SET subject=?, body=?, content_hash=?, status=?, error_code='',
                    error_summary='', updated_at=?
                WHERE id=?
                """,
                (subject, body, self._content_hash(subject, body), status, time.time(), job_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_job(job_id)

    def rebind_email_sender(
        self, job_id: int, provider: str, sender_account: str
    ) -> dict:
        if provider not in EMAIL_PROVIDERS:
            raise ValueError("不支持的邮箱提供商")
        sender = sender_account.strip().casefold()
        if not sender:
            raise ValueError("发件邮箱不能为空")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE outreach_jobs
                SET provider=?, sender_account_snapshot=?, status='pending_review',
                    message_id='', provider_reference='', error_code='',
                    error_summary='', submitted_at=NULL, updated_at=?
                WHERE id=? AND channel='email'
                  AND status IN ('pending_review','failed')
                """,
                (provider, sender, time.time(), job_id),
            )
            conn.commit()
        finally:
            conn.close()
        if cursor.rowcount != 1:
            self.get_job(job_id)
            raise OutreachConflict("当前邮件状态不能更换发件账号")
        return self.get_job(job_id)

    def mark_opened(self, job_id: int) -> dict:
        conn = self._connect()
        try:
            now = time.time()
            cursor = conn.execute(
                """
                UPDATE outreach_jobs
                SET status='opened', opened_at=?, updated_at=?
                WHERE id=? AND channel='whatsapp' AND status='pending_review'
                """,
                (now, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()
        if cursor.rowcount != 1:
            self.get_job(job_id)
            raise OutreachConflict("当前任务不能打开 WhatsApp")
        return self.get_job(job_id)

    def claim_email_send(self, job_id: int) -> dict:
        message_id = f"<tradewind-{uuid.uuid4().hex}@local>"
        conn = self._connect()
        try:
            now = time.time()
            cursor = conn.execute(
                """
                UPDATE outreach_jobs
                SET status='sending',
                    message_id=CASE WHEN message_id='' THEN ? ELSE message_id END,
                    error_code='', error_summary='', updated_at=?
                WHERE id=? AND channel='email' AND status IN ('pending_review','failed')
                """,
                (message_id, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()
        if cursor.rowcount != 1:
            self.get_job(job_id)
            raise OutreachConflict("该邮件当前不能发送或已在发送")
        return self.get_job(job_id)

    def complete_email_send(
        self,
        job_id: int,
        status: str,
        code: str,
        summary: str,
        reference: str = "",
    ) -> dict:
        if status not in {"accepted", "failed", "unknown"}:
            raise InvalidTransition("邮件发送结果状态无效")
        now = time.time()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE outreach_jobs
                SET status=?, error_code=?, error_summary=?, provider_reference=?,
                    submitted_at=?, updated_at=?
                WHERE id=? AND status='sending'
                """,
                (status, code, summary, reference, now, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()
        if cursor.rowcount != 1:
            self.get_job(job_id)
            raise OutreachConflict("邮件任务不在发送状态")
        return self.get_job(job_id)

    def transition(self, job_id: int, target: str, reason: str = "") -> dict:
        current = self.get_job(job_id)
        if target not in ALLOWED_TRANSITIONS.get(current["status"], set()):
            raise InvalidTransition(
                f"不允许从 {current['status']} 变更为 {target}"
            )
        if current["channel"] == "email" and target in {"opened", "sent_manual"}:
            raise InvalidTransition("邮件任务不能使用 WhatsApp 状态")
        if current["channel"] == "whatsapp" and target in {"sending", "accepted"}:
            raise InvalidTransition("WhatsApp 任务不能使用邮件状态")
        now = time.time()
        replied_at = now if target == "replied" else current["replied_at"]
        submitted_at = (
            now if target in {"accepted", "sent_manual"} else current["submitted_at"]
        )
        error_summary = reason.strip() if reason else current["error_summary"]
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE outreach_jobs
                SET status=?, error_summary=?, replied_at=?, submitted_at=?, updated_at=?
                WHERE id=?
                """,
                (target, error_summary, replied_at, submitted_at, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_job(job_id)

    def recover_interrupted(self) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE outreach_jobs
                SET status='unknown', error_code='recovered_after_restart',
                    error_summary='应用在发送过程中退出，请先到发件箱核查', updated_at=?
                WHERE status='sending'
                """,
                (time.time(),),
            )
            conn.commit()
            return max(0, cursor.rowcount)
        finally:
            conn.close()

    def add_suppression(self, channel: str, recipient: str, reason: str) -> dict:
        normalized = normalize_recipient(channel, recipient)
        reason = reason.strip()
        if not reason:
            raise ValueError("禁止联系原因不能为空")
        conn = self._connect()
        try:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO contact_suppressions
                        (channel, normalized_recipient, reason, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (channel, normalized, reason, time.time()),
                )
                suppression_id = int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT id FROM contact_suppressions
                    WHERE channel=? AND normalized_recipient=?
                    """,
                    (channel, normalized),
                ).fetchone()
                suppression_id = int(row["id"])
            if channel == "email":
                recipient_clause = "lower(trim(recipient))=?"
            else:
                recipient_clause = "recipient=?"
            conn.execute(
                f"""
                UPDATE outreach_jobs
                SET status='suppressed', error_code='suppressed',
                    error_summary=?, updated_at=?
                WHERE channel=? AND {recipient_clause}
                  AND status IN ('pending_review','opened','sending','unknown','failed')
                """,
                (reason, time.time(), channel, normalized),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM contact_suppressions WHERE id=?", (suppression_id,)
            ).fetchone()
        finally:
            conn.close()
        return self._suppression(row)

    def list_suppressions(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM contact_suppressions ORDER BY created_at DESC, id DESC"
            ).fetchall()
        finally:
            conn.close()
        return [self._suppression(row) for row in rows]

    def delete_suppression(self, suppression_id: int) -> None:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM contact_suppressions WHERE id=?", (suppression_id,)
            )
            conn.commit()
        finally:
            conn.close()
        if cursor.rowcount != 1:
            raise OutreachNotFound("禁止联系记录不存在")

    def aggregate_counts(self) -> dict:
        conn = self._connect()
        try:
            total = int(conn.execute("SELECT COUNT(*) FROM outreach_jobs").fetchone()[0])
            by_channel = {
                row["channel"]: int(row["count"])
                for row in conn.execute(
                    "SELECT channel, COUNT(*) AS count FROM outreach_jobs GROUP BY channel"
                )
            }
            by_status = {
                row["status"]: int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM outreach_jobs GROUP BY status"
                )
            }
        finally:
            conn.close()
        return {"jobs_total": total, "by_channel": by_channel, "by_status": by_status}
