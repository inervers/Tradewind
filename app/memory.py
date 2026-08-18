"""Tradewind 生成历史与质量评测日志（SQLite，零依赖）。

用途：每次生成的开发信 + 客户 + 质量评分落库，供人工复盘。
该数据库不会自动回灌 Prompt；认可的内容需由用户显式晋升到话术模板库，
避免低质量输出和客户信息形成无审查的反馈循环。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.config import settings

MEMORY_DB = Path(settings.memory_db_path)


def _connect() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            customer   TEXT NOT NULL,
            country    TEXT DEFAULT '',
            product    TEXT DEFAULT '',
            email      TEXT NOT NULL,
            score      REAL DEFAULT 0,
            language   TEXT DEFAULT 'zh-hant',
            format     TEXT DEFAULT 'email',
            created_at REAL NOT NULL
        )
        """
    )
    # 兼容旧库：早期表没有 language/format 列，补齐
    cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    if "language" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN language TEXT DEFAULT 'zh-hant'")
    if "format" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN format TEXT DEFAULT 'email'")
    return conn


def save_email(customer: str, country: str, product: str, email: str,
               score: float = 0, language: str = "zh-hant", format_: str = "email") -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO emails (customer, country, product, email, score, language, format, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (customer, country, product, email[:2000], score, language, format_, time.time()),
    )
    conn.commit()
    conn.close()


def recent_emails(limit: int = 10) -> list[dict]:
    """最近生成的开发信（复盘用），带 id 供删除。"""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, customer, country, product, email, score, language, format, created_at "
        "FROM emails ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "customer": r[1], "country": r[2], "product": r[3],
         "email": r[4], "score": r[5], "language": r[6], "format": r[7], "created_at": r[8]}
        for r in rows
    ]


def delete_email(email_id: int) -> None:
    """删除一条历史记录。"""
    conn = _connect()
    conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    conn.commit()
    conn.close()


def delete_emails(email_ids: list[int]) -> int:
    """一次删除多条历史记录，返回实际删除数量。"""
    ids = sorted({int(email_id) for email_id in email_ids})
    if not ids:
        return 0
    conn = _connect()
    placeholders = ",".join("?" for _ in ids)
    cursor = conn.execute(f"DELETE FROM emails WHERE id IN ({placeholders})", ids)
    removed = max(0, cursor.rowcount)
    conn.commit()
    conn.close()
    return removed
