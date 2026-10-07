"""Owned synthetic backup fixtures, independent of the production exporter."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch
import zipfile

from app.backup.models import BackupLayout
from app.backup.paths import resolve_layout


def make_business_fixture(root: Path) -> BackupLayout:
    layout = resolve_layout(root)
    layout.data_dir.mkdir(parents=True)
    documents = {
        "customers.json": [{"id": "customer-甲", "name": "合成甲店", "status": "legacy-status",
                            "email": "owner@shop.example", "created_at": 1704067200}],
        "products.json": [{"id": "product-1", "title": "合成设备", "content": "演示资料", "tags": ["产品"]}],
        "emails.json": [{"id": "template-1", "title": "合成话术", "content": "演示正文", "source": "manual"}],
        "crawler_seen.json": ["old.example", {"domain": "web.example", "first_seen_at": 1704067200},
                              {"source": "maps", "place_key": "synthetic-place", "name": "合成乙店",
                               "country": "测试", "domain": "maps.example", "website": "https://maps.example",
                               "phone": "00000000", "first_seen_at": 1704067200}],
    }
    for name, value in documents.items():
        (layout.data_dir / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    # Establish the owned environment BEFORE lazy imports. memory/config has
    # import-time paths; patch them even when an earlier isolated test imported it.
    with patch.dict(os.environ, {"TRADEWIND_DATA_DIR": str(root),
                                 "MEMORY_DB_PATH": str(layout.data_dir / "tradewind_memory.db")}):
        memory = importlib.import_module("app.memory")
        outreach = importlib.import_module("app.outreach_store")
        followup = importlib.import_module("app.customer_followup")
        with patch.object(memory, "MEMORY_DB", layout.data_dir / "tradewind_memory.db"):
            memory._connect().close()
        outreach.OutreachStore(layout.data_dir / "tradewind_memory.db")
        followup.FollowupStore(layout.data_dir / "tradewind_memory.db")
    with closing(sqlite3.connect(layout.data_dir / "tradewind_memory.db")) as conn, conn:
        conn.execute("INSERT INTO emails(id,customer,email,created_at,workflow_id) VALUES(7,'已删除合成店','历史正文',1704067200,'history-workflow')")
        conn.execute("INSERT INTO outreach_jobs(id,workflow_id,customer_id,customer_name_snapshot,channel,provider,recipient,body,content_hash,status,created_at,updated_at) VALUES(11,'job-workflow','deleted-customer','已删除合成店','email','qq_smtp','old@shop.example','历史文案',?,'accepted',1704067200,1704067200)", (hashlib.sha256(b'\0' + '历史文案'.encode()).hexdigest(),))
        conn.execute("INSERT INTO contact_suppressions(id,channel,normalized_recipient,reason,created_at) VALUES(3,'email','blocked@shop.example','合成规则',1704067200)")
        conn.execute("INSERT INTO customer_followup_records VALUES(5,'customer-甲','合成甲店',1704067200,'phone','note','合成沟通',1704067200,'record-request')")
        conn.execute("INSERT INTO customer_followup_deleted_requests VALUES('deleted-request','customer-甲')")
        conn.execute("INSERT INTO customer_followup_plans VALUES(9,'customer-甲','合成甲店','2026-10-10','合成事项','superseded',NULL,1704067200,1704067300,'plan-old')")
        conn.execute("INSERT INTO customer_followup_plans VALUES(13,'customer-甲','合成甲店','2026-10-12','合成后续','active',9,1704067300,NULL,'plan-current')")
        conn.execute("UPDATE sqlite_sequence SET seq=50 WHERE name='customer_followup_records'")
    (layout.data_dir / "crawler_photos/空店").mkdir(parents=True)
    (layout.data_dir / "crawler_photos/合成甲店").mkdir()
    (layout.data_dir / "photo_scan").mkdir()
    # Minimal valid 1x1 GIF. No real media or customer identifiers.
    (layout.data_dir / "crawler_photos/合成甲店/01.gif").write_bytes(
        bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"))
    return layout


def make_constructor_fixture(root: Path, *, groups: tuple[str, ...]) -> BackupLayout:
    """Initialize ONLY selected current constructors against an owned database."""
    if len(set(groups)) != len(groups) or not set(groups) <= {"memory", "outreach", "followup"}:
        raise ValueError("unknown fixture constructor group")
    layout = resolve_layout(root)
    layout.data_dir.mkdir(parents=True)
    (layout.data_dir / "customers.json").write_text(
        json.dumps([{"id": "customer-甲", "name": "合成甲店"}], ensure_ascii=False), encoding="utf-8")
    db = layout.data_dir / "tradewind_memory.db"
    with patch.dict(os.environ, {"TRADEWIND_DATA_DIR": str(root), "MEMORY_DB_PATH": str(db)}):
        if "memory" in groups:
            memory = importlib.import_module("app.memory")
            with patch.object(memory, "MEMORY_DB", db):
                memory._connect().close()
        if "outreach" in groups:
            importlib.import_module("app.outreach_store").OutreachStore(db)
        if "followup" in groups:
            importlib.import_module("app.customer_followup").FollowupStore(db)
    with closing(sqlite3.connect(db)) as conn, conn:
        if not groups:
            # connect() alone can leave a zero-byte file. VACUUM persists a
            # real SQLite header with an empty, valid standard schema.
            conn.execute("VACUUM")
        if "memory" in groups:
            conn.execute("INSERT INTO emails(id,customer,email,created_at,workflow_id) VALUES(7,'合成旧店','历史正文',1704067200,'history-workflow')")
            conn.execute("UPDATE sqlite_sequence SET seq=70 WHERE name='emails'")
        if "outreach" in groups:
            conn.execute("INSERT INTO outreach_jobs(id,workflow_id,customer_id,customer_name_snapshot,channel,provider,recipient,body,content_hash,status,created_at,updated_at) VALUES(11,'job-workflow','deleted-customer','合成旧店','email','qq_smtp','old@shop.example','历史文案',?,'accepted',1704067200,1704067200)",
                         (hashlib.sha256(b'\0' + '历史文案'.encode()).hexdigest(),))
            conn.execute("INSERT INTO contact_suppressions(id,channel,normalized_recipient,reason,created_at) VALUES(3,'email','blocked@shop.example','合成规则',1704067200)")
            conn.execute("UPDATE sqlite_sequence SET seq=110 WHERE name='outreach_jobs'")
            conn.execute("UPDATE sqlite_sequence SET seq=30 WHERE name='contact_suppressions'")
        if "followup" in groups:
            conn.execute("INSERT INTO customer_followup_records VALUES(5,'customer-甲','合成甲店',1704067200,'phone','note','合成沟通',1704067200,'record-request')")
            conn.execute("INSERT INTO customer_followup_deleted_requests VALUES('deleted-request','customer-甲')")
            conn.execute("INSERT INTO customer_followup_plans VALUES(9,'customer-甲','合成甲店','2026-10-10','合成事项','superseded',NULL,1704067200,1704067300,'plan-old')")
            conn.execute("INSERT INTO customer_followup_plans VALUES(13,'customer-甲','合成甲店','2026-10-12','合成后续','active',9,1704067300,NULL,'plan-current')")
            conn.execute("UPDATE sqlite_sequence SET seq=50 WHERE name='customer_followup_records'")
            conn.execute("UPDATE sqlite_sequence SET seq=130 WHERE name='customer_followup_plans'")
    return layout


def business_digest(layout: BackupLayout) -> dict[str, str]:
    return {path.relative_to(layout.data_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(layout.data_dir.rglob("*")) if path.is_file()
            and not path.relative_to(layout.data_dir).parts[0].startswith((".backup", "backups"))}


def fixture_manifest(layout: BackupLayout, *, photos: bool) -> dict:
    """Hand-built wire format and independently queried fixture counts."""
    counts = {key: 0 for key in (
        "customers", "products", "templates", "crawler_seen", "emails", "outreach_jobs", "suppressions",
        "followup_records", "followup_deleted_requests", "followup_plans", "active_plans", "photos")}
    items = {key: False for key in ("customers", "products", "templates", "memory", "crawler_seen", "crawler_photos", "photo_scan")}
    files, directories = [], []
    for name, key in (("customers.json", "customers"), ("products.json", "products"),
                      ("emails.json", "templates"), ("crawler_seen.json", "crawler_seen")):
        path = layout.data_dir / name
        if path.exists():
            items[key] = True
            counts[key] = len(json.loads(path.read_text(encoding="utf-8")))
    db = layout.data_dir / "tradewind_memory.db"
    if db.exists():
        items["memory"] = True
        with closing(sqlite3.connect(db)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, key in (("emails", "emails"), ("outreach_jobs", "outreach_jobs"),
                               ("contact_suppressions", "suppressions"), ("customer_followup_records", "followup_records"),
                               ("customer_followup_deleted_requests", "followup_deleted_requests"), ("customer_followup_plans", "followup_plans")):
                if table in tables:
                    counts[key] = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            if "customer_followup_plans" in tables:
                counts["active_plans"] = conn.execute("SELECT count(*) FROM customer_followup_plans WHERE status='active'").fetchone()[0]
    for path in sorted(layout.data_dir.rglob("*")):
        relative = path.relative_to(layout.data_dir)
        is_photo = relative.parts[0] in {"crawler_photos", "photo_scan"}
        if is_photo and not photos:
            continue
        if path.is_dir() and is_photo:
            directories.append("data/" + relative.as_posix())
            items[relative.parts[0]] = True
        elif path.is_file() and (is_photo or len(relative.parts) == 1 and relative.name in {
                "customers.json", "products.json", "emails.json", "crawler_seen.json", "tradewind_memory.db"}):
            content = path.read_bytes()
            files.append({"path": "data/" + relative.as_posix(), "size": len(content),
                          "sha256": hashlib.sha256(content).hexdigest()})
            counts["photos"] += int(is_photo)
    return dict(format_version=1, app_version="0.2.0", created_at="2026-10-07T00:00:00Z",
                includes_photos=photos, items_present=items, counts=counts, files=files, directories=directories)


def make_archive(layout: BackupLayout, destination: Path, *, photos: bool) -> Path:
    manifest = fixture_manifest(layout, photos=photos)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for entry in manifest["files"]:
            archive.write(layout.data_dir / entry["path"].removeprefix("data/"), entry["path"])
    return destination


@contextmanager
def open_fixture_server(layout: BackupLayout):
    """Task6 supplies the HTTP fixture; never fall back to the live server."""
    raise NotImplementedError("Task6 must supply an isolated fixture server")
    yield  # pragma: no cover
