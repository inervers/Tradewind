"""Strict, initializer-free validation of untrusted local backup snapshots.

Only our constant DDL is executed (in an in-memory reference database).
Uploaded SQL is inspected as data, never executed. No configuration, business
store, model, credential or exporter imports belong in this module.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import struct
import time
import zipfile
import zlib

from app.version import APP_VERSION

from .models import (BackupCancelled, BackupError, BackupLimits, BackupSummary,
                     CancelCheck, COUNT_KEYS, FORMAT_VERSION, ITEM_KEYS,
                     ProgressSink, Snapshot, ValidatedArchive)
from .paths import (BUSINESS_FILES, PHOTO_DIRECTORIES, member_path_key,
                    owned_path, require_space, resolve_layout, validate_member_path,
                    _checked_path)

_CHUNK = 1024 * 1024
_JSON_ITEMS = {"customers.json": "customers", "products.json": "products",
               "emails.json": "templates", "crawler_seen.json": "crawler_seen"}
_ACTIVE = {"pending_review", "opened", "sending", "unknown", "failed"}
_OUTREACH_STATUSES = _ACTIVE | {"accepted", "sent_manual", "replied", "invalid", "skipped", "suppressed"}
_FOLLOWUP_CHANNELS = {"email", "whatsapp", "phone", "other"}
_OUTCOMES = {"awaiting_reply", "replied", "in_progress", "not_interested", "invalid", "note"}
_PLAN_STATUSES = {"active", "completed", "cancelled", "superseded"}
_TABLE_COUNTS = {"emails": "emails", "outreach_jobs": "outreach_jobs",
                 "contact_suppressions": "suppressions",
                 "customer_followup_records": "followup_records",
                 "customer_followup_deleted_requests": "followup_deleted_requests",
                 "customer_followup_plans": "followup_plans"}

# Source definitions copied from memory._connect, OutreachStore._connect and
# FollowupStore.__init__. These are a closed, versioned allowlist, not migrations.
_EMAIL_EARLY = """CREATE TABLE emails (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT NOT NULL,
 country TEXT DEFAULT '', product TEXT DEFAULT '', email TEXT NOT NULL,
 score REAL DEFAULT 0, created_at REAL NOT NULL)"""
_EMAIL_CURRENT = """CREATE TABLE emails (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT NOT NULL,
 country TEXT DEFAULT '', product TEXT DEFAULT '', email TEXT NOT NULL,
 score REAL DEFAULT 0, language TEXT DEFAULT 'zh-hant', format TEXT DEFAULT 'email',
 created_at REAL NOT NULL, workflow_id TEXT DEFAULT '')"""
_OUTREACH_DDL = """
CREATE TABLE outreach_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT DEFAULT '',
 customer_id TEXT NOT NULL, customer_name_snapshot TEXT NOT NULL,
 channel TEXT NOT NULL CHECK(channel IN ('whatsapp', 'email')), provider TEXT NOT NULL,
 sender_account_snapshot TEXT DEFAULT '', recipient TEXT NOT NULL,
 subject TEXT DEFAULT '', body TEXT NOT NULL, content_hash TEXT NOT NULL,
 message_id TEXT DEFAULT '', status TEXT NOT NULL, provider_reference TEXT DEFAULT '',
 error_code TEXT DEFAULT '', error_summary TEXT DEFAULT '', created_at REAL NOT NULL,
 updated_at REAL NOT NULL, opened_at REAL, submitted_at REAL, replied_at REAL);
CREATE UNIQUE INDEX uq_outreach_active_customer_channel ON outreach_jobs(customer_id, channel)
 WHERE status IN ('pending_review', 'opened', 'sending', 'unknown', 'failed');
CREATE INDEX idx_outreach_customer_history ON outreach_jobs(customer_id, created_at DESC, id DESC);
CREATE UNIQUE INDEX uq_outreach_workflow ON outreach_jobs(workflow_id) WHERE workflow_id <> '';
CREATE TABLE contact_suppressions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
 normalized_recipient TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(channel, normalized_recipient));
"""
_FOLLOWUP_DDL = """
CREATE TABLE customer_followup_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL,
 customer_name_snapshot TEXT NOT NULL, contacted_at REAL NOT NULL, channel TEXT NOT NULL,
 outcome TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL, request_id TEXT NOT NULL UNIQUE);
CREATE INDEX idx_followup_record_customer_time ON customer_followup_records(customer_id, contacted_at DESC, id DESC);
CREATE TABLE customer_followup_deleted_requests (request_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL);
CREATE INDEX idx_followup_deleted_request_customer ON customer_followup_deleted_requests(customer_id);
CREATE TABLE customer_followup_plans (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, customer_name_snapshot TEXT NOT NULL,
 due_date TEXT NOT NULL, action TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','completed','cancelled','superseded')),
 previous_plan_id INTEGER, created_at REAL NOT NULL, closed_at REAL, request_id TEXT NOT NULL UNIQUE);
CREATE UNIQUE INDEX uq_followup_active_customer ON customer_followup_plans(customer_id) WHERE status='active';
CREATE INDEX idx_followup_plan_customer ON customer_followup_plans(customer_id, created_at DESC, id DESC);
"""


def _error(code="invalid_backup"):
    messages = {
        "invalid_backup": "备份内容损坏或不符合业务备份格式。",
        "resource_limit": "备份超过本版本允许的资源上限。",
        "unsupported_schema": "备份数据库结构不受本版本支持，请使用兼容版本或人工停机备份。",
        "unsupported_version": "备份格式版本不受支持，请使用兼容版本。",
        "destination_exists": "验证目录必须是本次操作新建的专属目录。",
        "invalid_json": "备份 JSON 数据损坏或业务字段无效。",
        "invalid_database": "备份数据库损坏或业务数据无效。",
    }
    return BackupError(code, messages[code])


def _check(condition, code="invalid_backup"):
    if not condition:
        raise _error(code)


def _cancel(cancel):
    if cancel():
        raise BackupCancelled()


def _limits(limits):
    for value in vars(limits).values():
        _check(type(value) is int and value >= 0, "resource_limit")


class _Budget:
    def __init__(self, limits, cancel):
        self.limits, self.cancel, self.total = limits, cancel, 0

    def add(self, size):
        _cancel(self.cancel)
        self.total += size
        _check(self.total <= self.limits.max_unpacked_bytes, "resource_limit")


def _json(content):
    def pairs(values):
        result = {}
        for key, value in values:
            _check(key not in result, "invalid_json")
            result[key] = value
        return result
    def invalid_constant(value):
        raise _error("invalid_json")
    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _error("invalid_json") from None


def _text(value, *, required=False, maximum=None):
    return (type(value) is str and (not required or bool(value.strip()))
            and (maximum is None or len(value) <= maximum))


def _number(value, *, positive=False):
    return (type(value) in {int, float} and math.isfinite(value)
            and (not positive or value > 0))


def _metadata(manifest, includes_photos=None):
    _check(type(manifest) is dict)
    _check(type(manifest.get("format_version")) is int and manifest["format_version"] == FORMAT_VERSION,
           "unsupported_version")
    _check(_text(manifest.get("app_version"), required=True, maximum=100))
    created = manifest.get("created_at")
    _check(_text(created, required=True, maximum=100))
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        _check(stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0)
    except ValueError:
        raise _error() from None
    _check(type(manifest.get("includes_photos")) is bool)
    if includes_photos is not None:
        _check(manifest["includes_photos"] == includes_photos)
    for field, keys, cls in (("items_present", ITEM_KEYS, bool), ("counts", COUNT_KEYS, int)):
        values = manifest.get(field)
        _check(type(values) is dict and set(values) == set(keys))
        _check(all(type(value) is cls and (cls is bool or value >= 0) for value in values.values()))
    return manifest


def _file_limit(name, limits):
    if name == "manifest.json":
        return limits.max_manifest_bytes
    relative = name.removeprefix("data/")
    parts = relative.split("/")
    if parts[0] in PHOTO_DIRECTORIES:
        _check(len(parts) >= 2)
        _check(Path(parts[-1]).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"})
        return limits.max_photo_bytes
    _check(relative in BUSINESS_FILES)
    return limits.max_db_bytes if relative.endswith(".db") else limits.max_json_bytes


def _manifest(manifest, limits):
    _metadata(manifest)
    _check(set(manifest) == {"format_version", "app_version", "created_at", "includes_photos",
                             "items_present", "counts", "files", "directories"})
    files, directories = manifest.get("files"), manifest.get("directories")
    _check(type(files) is list and type(directories) is list)
    _check(len(files) + 1 <= limits.max_files, "resource_limit")
    # Every directory consumes manifest bytes; additionally bound path work.
    _check(len(directories) <= limits.max_manifest_bytes, "resource_limit")
    names, keys, all_paths = {}, {}, []
    for entry in files:
        _check(type(entry) is dict and set(entry) == {"path", "size", "sha256"})
        name = validate_member_path(entry["path"])
        _check(name != "manifest.json")
        _check(type(entry["size"]) is int and 0 <= entry["size"] <= _file_limit(name, limits), "resource_limit")
        _check(type(entry["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None)
        _check(name not in names)
        names[name] = entry
        all_paths.append((name, False))
    for name in directories:
        validate_member_path(name)
        _check(name.startswith(tuple("data/" + value for value in PHOTO_DIRECTORIES)))
        _check(name.split("/")[1] in PHOTO_DIRECTORIES)
        all_paths.append((name, True))
    # Include implicit parents: aliases in two otherwise different leaf names
    # must not collapse a Windows directory or mask a file/dir conflict.
    kinds = {}
    for name, directory in all_paths:
        parts = name.split("/")
        for index in range(2, len(parts) + 1):
            part = "/".join(parts[:index])
            key = member_path_key(part)
            is_dir = index < len(parts) or directory
            _check(key not in keys or keys[key] == part)
            _check(key not in kinds or kinds[key] == is_dir)
            keys[key], kinds[key] = part, is_dir
        _check(name not in directories or directory)
    _check(len(directories) == len(set(directories)))
    if not manifest["includes_photos"]:
        _check(not directories and not any(name.split("/")[1] in PHOTO_DIRECTORIES for name in names))
        _check(not manifest["items_present"]["crawler_photos"] and not manifest["items_present"]["photo_scan"])
    return names, directories


def _zip_directory_bound(source, size, limits):
    """Reject an oversized central directory before ZipFile allocates entries."""
    source.seek(max(0, size - 65557))
    tail = source.read(min(size, 65557))
    index = tail.rfind(b"PK\x05\x06")
    _check(index >= 0 and len(tail) - index >= 22)
    eocd = struct.unpack_from("<4s4H2LH", tail, index)
    _check(index + 22 + eocd[7] == len(tail))
    _check(eocd[1] == eocd[2] == 0 and eocd[3] == eocd[4])
    # Our <=2GiB/20k format needs no ZIP64 EOCD. ZIP64 local file
    # headers (force_zip64 by streaming exporters) remain supported below.
    _check(eocd[4] != 65535 and eocd[5] != 0xffffffff and eocd[6] != 0xffffffff)
    _check(eocd[4] <= limits.max_files, "resource_limit")
    # A record has a fixed 46 byte header and <=65535 bytes per variable
    # field; bound total directory allocation by the allowed entry count.
    _check(eocd[5] <= limits.max_files * (46 + 3 * 65535), "resource_limit")
    _check(eocd[6] + eocd[5] == size - len(tail) + index)
    return eocd[4], eocd[6], eocd[5]


class _ZipInventory:
    """Read directory records individually; never buffer comments/whole CD."""
    def __init__(self, source, size, limits, cancel, layout):
        count, self.start_dir, directory_size = _zip_directory_bound(source, size, limits)
        end = self.start_dir + directory_size
        source.seek(self.start_dir)
        self.infos = []
        for _ in range(count):
            _cancel(cancel)
            header = source.read(46)
            _check(len(header) == 46)
            fields = struct.unpack("<4s6H3L5H2L", header)
            _check(fields[0] == b"PK\x01\x02" and fields[13] == 0)
            _check(fields[2] <= 45)
            # All files fit the supported limits without ZIP64 directory
            # sentinel sizes. Streaming ZIP64 LOCAL headers are legal.
            _check(all(fields[index] != 0xffffffff for index in (8, 9, 16)))
            raw_name = source.read(fields[10])
            _check(len(raw_name) == fields[10])
            try:
                name = raw_name.decode("utf-8" if fields[3] & 0x800 else "cp437")
            except UnicodeError:
                raise _error() from None
            validate_member_path(name)
            if name != "manifest.json":
                owned_path(layout, name.removeprefix("data/"))
            info = zipfile.ZipInfo(name)
            info.flag_bits, info.compress_type = fields[3:5]
            info.CRC, info.compress_size, info.file_size = fields[7:10]
            info.create_system = fields[1] >> 8
            info.external_attr, info.header_offset = fields[15:17]
            self.infos.append(info)
            # Skip untrusted extras/comments; never retain or execute them.
            source.seek(fields[11] + fields[12], 1)
            _check(source.tell() <= end)
        _check(source.tell() == end)

    def infolist(self):
        return self.infos


@contextmanager
def _zip_inventory(source, size, limits, cancel, layout):
    yield _ZipInventory(source, size, limits, cancel, layout)


def _zip_ranges(source, archive, infos):
    spans, offsets = [], {}
    for info in infos:
        _check(info.filename == info.orig_filename and "\x00" not in info.filename)
        _check(info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
        _check(info.flag_bits & ~0x080e == 0 and not info.flag_bits & 1)
        mode = info.external_attr >> 16
        _check(stat.S_IFMT(mode) in {0, stat.S_IFREG})
        _check(not info.external_attr & (0x10 | 0x400) and not info.is_dir())
        _check(info.header_offset >= 0)
        source.seek(info.header_offset)
        header = source.read(30)
        _check(len(header) == 30)
        values = struct.unpack("<4s5H3L2H", header)
        _check(values[0] == b"PK\x03\x04" and values[2] == info.flag_bits and values[3] == info.compress_type)
        encoded = source.read(values[-2])
        try:
            local_name = encoded.decode("utf-8" if info.flag_bits & 0x800 else "cp437")
        except UnicodeError:
            raise _error() from None
        _check(local_name == info.filename)
        start = info.header_offset + 30 + values[-2] + values[-1]
        end = start + info.compress_size
        _check(end <= archive.start_dir)
        spans.append((info.header_offset, end))
        offsets[info.filename] = start
    spans.sort()
    _check(all(left[1] <= right[0] for left, right in zip(spans, spans[1:])))
    _check(infos[0].header_offset == spans[0][0] == 0)
    return offsets


def _inflate(source, info, offset, ceiling, budget, sink):
    """Count actual zlib output, including bytes beyond a forged file_size.

    ZipExtFile truncates at declared file_size, so use the raw compressed
    stream with bounded max_length instead. Both input/output chunks <=1MiB.
    """
    source.seek(offset)
    remaining, actual, crc = info.compress_size, 0, 0
    digest = hashlib.sha256()
    inflater = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    def emit(data):
        nonlocal actual, crc
        budget.add(len(data))
        actual += len(data)
        _check(actual <= ceiling, "resource_limit")
        crc = zlib.crc32(data, crc)
        digest.update(data)
        sink(data)
    while remaining:
        _cancel(budget.cancel)
        data = source.read(min(remaining, _CHUNK))
        _check(bool(data))
        remaining -= len(data)
        if inflater is None:
            emit(data)
        else:
            while data:
                emit(inflater.decompress(data, _CHUNK))
                _check(not inflater.unused_data)
                data = inflater.unconsumed_tail
    if inflater is not None:
        _check(inflater.eof and not inflater.unused_data and not inflater.unconsumed_tail)
    _check(actual == info.file_size and crc == info.CRC)
    return actual, digest.hexdigest()


def _read_file(path, name, budget):
    content = bytearray()
    ceiling = _file_limit(name, budget.limits)
    with path.open("rb") as source:
        while True:
            _cancel(budget.cancel)
            data = source.read(_CHUNK)
            if not data:
                break
            budget.add(len(data))
            content.extend(data)
            _check(len(content) <= ceiling, "resource_limit")
    return content


def _json_rows(name, content, cancel):
    rows = _json(content)
    _check(type(rows) is list, "invalid_json")
    seen = set()
    customer_text = {"id", "name", "country", "city", "email", "website", "phone", "notes", "status", "source"}
    for row in rows:
        _cancel(cancel)
        if name == "crawler_seen.json":
            if type(row) is str:
                _check(_text(row, required=True), "invalid_json")
                continue
            _check(type(row) is dict, "invalid_json")
            if set(row) == {"domain", "first_seen_at"}:
                _check(_text(row["domain"], required=True), "invalid_json")
            else:
                _check(set(row) == {"source", "place_key", "name", "country", "domain", "website", "phone", "first_seen_at"}, "invalid_json")
                _check(row["source"] == "maps" and _text(row["place_key"], required=True), "invalid_json")
                _check(all(_text(value) for key, value in row.items() if key != "first_seen_at"), "invalid_json")
            _check(_number(row["first_seen_at"], positive=True), "invalid_json")
            continue
        _check(type(row) is dict, "invalid_json")
        required = {"id", "name"} if name == "customers.json" else {"id", "title", "content"}
        _check(required <= set(row), "invalid_json")
        _check(all(_text(row[key], required=key != "content") for key in required), "invalid_json")
        _check(row["id"] == row["id"].strip() and row["id"] not in seen, "invalid_json")
        seen.add(row["id"])
        for key, value in row.items():
            if key == "created_at":
                _check(_number(value, positive=True), "invalid_json")
            elif name == "customers.json":
                _check(key in customer_text and _text(value), "invalid_json")
            elif key == "tags":
                _check(type(value) is list and all(_text(tag) for tag in value), "invalid_json")
            else:
                _check(key in {"id", "title", "content", "source"} and _text(value), "invalid_json")
    return rows


def _sql_tokens(sql):
    # Closed grammar: comments/extra punctuation/functions are not accepted.
    # Preserve string literal case/content, normalize keywords/identifiers.
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\w+|<>|!=|[(),=]", sql)
    residue = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\w+|<>|!=|[(),=]|\s|;", "", sql)
    _check(not residue, "unsupported_schema")
    return tuple(token if token.startswith("'") else token.strip('"').casefold() for token in tokens)


def _table_signature(conn, table):
    columns = {row[1]: tuple(row[2:]) for row in conn.execute(f'PRAGMA table_xinfo("{table}")')}
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
    tokens = _sql_tokens(sql)
    # Column definitions may be reordered after ALTER migration. Compare
    # top-level definitions as a multiset, retaining constraints and defaults.
    start, end = tokens.index("("), len(tokens) - 1
    _check(tokens[end] == ")", "unsupported_schema")
    definitions, current, depth = [], [], 0
    for token in tokens[start + 1:end]:
        if token == "," and depth == 0:
            definitions.append(tuple(current)); current = []
        else:
            current.append(token)
            depth += (token == "(") - (token == ")")
    definitions.append(tuple(current))
    indexes = []
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        name, unique, origin, partial = row[1:]
        details = tuple((item[2], item[3], item[4], item[5]) for item in conn.execute(f'PRAGMA index_xinfo("{name}")'))
        index_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        predicate = ()
        if index_sql:
            index_tokens = _sql_tokens(index_sql)
            if "where" in index_tokens:
                predicate = index_tokens[index_tokens.index("where") + 1:]
        indexes.append((name if origin == "c" else origin, unique, origin, partial, details, predicate))
    _check(not list(conn.execute(f'PRAGMA foreign_key_list("{table}")')), "unsupported_schema")
    return columns, sorted(definitions), sorted(indexes)


def _schema(conn):
    objects = list(conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"))
    tables = {name for kind, name, _, _ in objects if kind == "table"}
    _check("emails" in tables, "unsupported_schema")
    business = tables - {"sqlite_sequence"}
    # Explicit supported snapshots: earliest seven-column memory-only, or
    # all six current business tables. Partial/middle versions are refused.
    early = business == {"emails"}
    _check(early or business == set(_TABLE_COUNTS), "unsupported_schema")
    for kind, name, table, sql in objects:
        _check(kind in {"table", "index"} and (table in business or name == "sqlite_sequence"), "unsupported_schema")
        if kind == "table":
            _check(sql and not re.search(r"\bvirtual\b", sql, re.I), "unsupported_schema")
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.execute(_EMAIL_EARLY if early else _EMAIL_CURRENT)
        if not early:
            reference.execute("CREATE UNIQUE INDEX uq_emails_workflow ON emails(workflow_id) WHERE workflow_id <> ''")
            reference.executescript(_OUTREACH_DDL + _FOLLOWUP_DDL)
        _check(tables == {row[0] for row in reference.execute("SELECT name FROM sqlite_master WHERE type='table'")}, "unsupported_schema")
        for table in sorted(tables):
            _check(_table_signature(conn, table) == _table_signature(reference, table), "unsupported_schema")
    return business


def _normalized(channel, recipient):
    return recipient.strip().casefold() if channel == "email" else re.sub(r"\D", "", recipient)


def _database(path, customer_ids, counts, cancel):
    # immutable prevents WAL/journal discovery and any sidecar writes; this
    # API is for completed, isolated snapshots, never a running database.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)) as conn:
        conn.enable_load_extension(False)
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024**2)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 64 * 1024)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        allowed_pragmas = {"table_xinfo", "index_list", "index_xinfo", "foreign_key_list", "integrity_check"}
        def authorize(action, first, second, database, source):
            if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_PRAGMA and first in allowed_pragmas:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        conn.set_authorizer(authorize)
        steps, cancelled = 0, False
        deadline = time.monotonic() + 30
        def query_budget():
            nonlocal steps, cancelled
            steps += 1000
            cancelled = bool(cancel())
            return int(cancelled or steps > 50_000_000 or time.monotonic() > deadline)
        conn.set_progress_handler(query_budget, 1000)
        try:
            tables = _schema(conn)
            _check([tuple(row) for row in conn.execute("PRAGMA integrity_check")] == [("ok",)], "invalid_database")
            columns = {table: {row[1]: row[2] for row in conn.execute(f'PRAGMA table_xinfo("{table}")')} for table in tables}
            workflows, active_jobs, active_plans = set(), set(), set()
            records, deleted, plans = {}, {}, {}
            max_ids = {}
            for table in sorted(tables):
                _cancel(cancel)
                maximum = 0
                for row in conn.execute(f'SELECT * FROM "{table}" NOT INDEXED'):
                    _cancel(cancel)
                    row = dict(row)
                    counts[_TABLE_COUNTS[table]] += 1
                    for key, value in row.items():
                        if value is None:
                            # Only known nullable/default-valued legacy fields
                            # may be NULL, as allowed by the source schema.
                            nullable = {"country", "product", "score", "language", "format", "workflow_id",
                                        "sender_account_snapshot", "subject", "message_id", "provider_reference",
                                        "error_code", "error_summary", "opened_at", "submitted_at", "replied_at",
                                        "previous_plan_id", "closed_at"}
                            _check(key in nullable, "invalid_database")
                            continue
                        expected = columns[table][key]
                        _check((expected == "TEXT" and type(value) is str) or
                               (expected == "INTEGER" and type(value) is int) or
                               (expected == "REAL" and _number(value)), "invalid_database")
                    if "id" in row:
                        _check(type(row["id"]) is int and row["id"] > 0, "invalid_database")
                        maximum = max(maximum, row["id"])
                    for key in {"created_at", "updated_at", "contacted_at", "opened_at", "submitted_at", "replied_at", "closed_at"} & row.keys():
                        _check(row[key] is None or _number(row[key], positive=True), "invalid_database")
                    workflow = row.get("workflow_id")
                    if workflow:
                        identity = (table, workflow)
                        _check(identity not in workflows, "invalid_database")
                        workflows.add(identity)
                    if table.startswith("customer_followup_"):
                        _check(row["customer_id"] in customer_ids, "invalid_database")
                        _check(_text(row["request_id"], required=True, maximum=128), "invalid_database")
                        if "customer_name_snapshot" in row:
                            _check(_text(row["customer_name_snapshot"], required=True, maximum=500), "invalid_database")
                    if table == "customer_followup_records":
                        _check(row["channel"] in _FOLLOWUP_CHANNELS and row["outcome"] in _OUTCOMES, "invalid_database")
                        _check(_text(row["content"], required=True, maximum=4000), "invalid_database")
                        _check(row["request_id"] not in records, "invalid_database")
                        records[row["request_id"]] = row["customer_id"]
                    elif table == "customer_followup_deleted_requests":
                        _check(row["request_id"] not in deleted, "invalid_database")
                        deleted[row["request_id"]] = row["customer_id"]
                    elif table == "customer_followup_plans":
                        _check(row["status"] in _PLAN_STATUSES and _text(row["action"], required=True, maximum=500), "invalid_database")
                        try:
                            _check(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["due_date"]) is not None, "invalid_database")
                            date.fromisoformat(row["due_date"])
                        except ValueError:
                            raise _error("invalid_database") from None
                        _check((row["status"] == "active") == (row["closed_at"] is None), "invalid_database")
                        if row["status"] == "active":
                            _check(row["customer_id"] not in active_plans, "invalid_database")
                            active_plans.add(row["customer_id"])
                            counts["active_plans"] += 1
                        plans[row["id"]] = row
                    elif table in {"outreach_jobs", "contact_suppressions"}:
                        _check(row["channel"] in {"email", "whatsapp"}, "invalid_database")
                        recipient = row["recipient"] if table == "outreach_jobs" else row["normalized_recipient"]
                        _check(_text(recipient, required=True) and recipient == _normalized(row["channel"], recipient), "invalid_database")
                        if table == "outreach_jobs":
                            providers = {"qq_smtp", "gmail_smtp"} if row["channel"] == "email" else {"whatsapp_link"}
                            _check(row["provider"] in providers and row["status"] in _OUTREACH_STATUSES, "invalid_database")
                            _check(row["channel"] != "whatsapp" or row["status"] not in {"sending", "accepted", "unknown"}, "invalid_database")
                            _check(all(_text(row[key], required=True) for key in {"customer_id", "customer_name_snapshot", "body"}), "invalid_database")
                            if row["status"] in _ACTIVE:
                                identity = (row["customer_id"], row["channel"])
                                _check(identity not in active_jobs, "invalid_database")
                                active_jobs.add(identity)
                if table != "customer_followup_deleted_requests":
                    max_ids[table] = maximum
            _check(not records.keys() & deleted.keys(), "invalid_database")
            for plan in plans.values():
                previous = plan["previous_plan_id"]
                if previous is not None:
                    _check(previous in plans and previous < plan["id"] and
                           plans[previous]["customer_id"] == plan["customer_id"] and
                           plans[previous]["status"] == "superseded", "invalid_database")
            sequences = set()
            for row in conn.execute("SELECT name,seq FROM sqlite_sequence"):
                _check(row["name"] in max_ids and row["name"] not in sequences, "invalid_database")
                _check(type(row["seq"]) is int and max_ids[row["name"]] <= row["seq"] < 2**63 - 1, "invalid_database")
                sequences.add(row["name"])
            _check(all(not maximum or table in sequences for table, maximum in max_ids.items()), "invalid_database")
        except sqlite3.Error:
            if cancelled or cancel():
                raise BackupCancelled() from None
            raise _error("invalid_database") from None


def _inspect(folder, *, includes_photos, limits, cancel):
    _limits(limits)
    _check(type(includes_photos) is bool)
    layout = resolve_layout(folder)
    folder = layout.data_dir.parent
    _check(folder.is_dir())
    for child in folder.iterdir():
        _cancel(cancel)
        _checked_path(child)
        _check(child.name in {"data", "manifest.json"})
    # A snapshot is a strict business-only tree, not a live runtime directory.
    # Inspect even excluded photo stores so links/private files cannot hide.
    if layout.data_dir.exists():
        aliases, pending, ordinary_files = {}, [layout.data_dir], 0
        while pending:
            _cancel(cancel)
            current = pending.pop()
            for child in current.iterdir():
                _cancel(cancel)
                relative = child.relative_to(layout.data_dir).as_posix()
                path = owned_path(layout, relative)
                key = member_path_key("data/" + relative)
                _check(key not in aliases or aliases[key] == relative)
                aliases[key] = relative
                photo = relative.split("/")[0] in PHOTO_DIRECTORIES
                _check(includes_photos or not photo)
                if path.is_dir():
                    _check(photo)
                    pending.append(path)
                else:
                    _check(path.is_file())
                    _file_limit("data/" + relative, limits)
                    ordinary_files += 1
                    _check(ordinary_files <= limits.max_files, "resource_limit")
    budget = _Budget(limits, cancel)
    metadata = None
    manifest_path = folder / "manifest.json"
    # resolve_layout checks all ancestors, but manifest is outside data_dir.
    _checked_path(manifest_path)
    if manifest_path.exists():
        metadata = _metadata(_json(_read_file(manifest_path, "manifest.json", budget)), includes_photos)
    counts = {key: 0 for key in COUNT_KEYS}
    items = {key: False for key in ITEM_KEYS}
    customer_ids = set()
    files = 1 if metadata else 0
    for name, key in _JSON_ITEMS.items():
        path = owned_path(layout, name)
        if path.exists():
            _check(path.is_file())
            files += 1
            rows = _json_rows(name, _read_file(path, "data/" + name, budget), cancel)
            items[key], counts[key] = True, len(rows)
            if name == "customers.json":
                customer_ids = {row["id"] for row in rows}
    path = owned_path(layout, "tradewind_memory.db")
    if path.exists():
        _check(path.is_file())
        files += 1
        # Do not retain a database-sized buffer just to account bytes.
        size = 0
        with path.open("rb") as source:
            while data := source.read(_CHUNK):
                size += len(data); budget.add(len(data))
                _check(size <= limits.max_db_bytes, "resource_limit")
        items["memory"] = True
        _database(owned_path(layout, "tradewind_memory.db"), customer_ids, counts, cancel)
    if includes_photos:
        aliases = {}
        for directory in sorted(PHOTO_DIRECTORIES):
            root = owned_path(layout, directory)
            if not root.exists():
                continue
            _check(root.is_dir())
            items[directory] = True
            pending = [root]
            while pending:
                _cancel(cancel)
                current = pending.pop()
                for child in current.iterdir():
                    relative = child.relative_to(layout.data_dir).as_posix()
                    path = owned_path(layout, relative)
                    key = member_path_key("data/" + relative)
                    _check(key not in aliases or aliases[key] == relative)
                    aliases[key] = relative
                    if path.is_dir():
                        pending.append(path)
                    else:
                        _check(path.is_file())
                        files += 1
                        _check(files <= limits.max_files, "resource_limit")
                        ceiling = _file_limit("data/" + relative, limits)
                        size, header = 0, b""
                        with path.open("rb") as source:
                            while data := source.read(_CHUNK):
                                budget.add(len(data)); size += len(data)
                                _check(size <= ceiling, "resource_limit")
                                if not header: header = data[:12]
                        suffix = path.suffix.lower()
                        signatures = {".jpg": header.startswith(b"\xff\xd8\xff"), ".jpeg": header.startswith(b"\xff\xd8\xff"),
                                      ".png": header.startswith(b"\x89PNG\r\n\x1a\n"), ".gif": header.startswith((b"GIF87a", b"GIF89a")),
                                      ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP"}
                        _check(signatures[suffix])
                        counts["photos"] += 1
    _check(files <= limits.max_files, "resource_limit")
    _cancel(cancel)
    # A metadata-free snapshot describes this inspection, not a guessed file
    # creation date. Existing archive metadata remains unchanged.
    return BackupSummary(FORMAT_VERSION, metadata["app_version"] if metadata else APP_VERSION,
                         metadata["created_at"] if metadata else datetime.now(timezone.utc).isoformat(),
                         includes_photos, items, counts)


def inspect_snapshot(folder: Path, *, includes_photos: bool, limits: BackupLimits) -> BackupSummary:
    """Read a completed snapshot without opening any business initializers.

    Metadata, when present, supplies only version/date. Otherwise use the
    current application version and UTC inspection time. Existence and counts
    always come from content. No absent JSON/database is created or defaulted.
    """
    try:
        return _inspect(Path(folder), includes_photos=includes_photos, limits=limits, cancel=lambda: False)
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
        raise _error() from None


def validate_archive(archive: Path, destination: Path, *, limits: BackupLimits,
                     cancel: CancelCheck, progress: ProgressSink) -> ValidatedArchive:
    """Validate into a NEW exclusively owned destination; remove it on failure.

    Existing destinations are refused, never erased. The caller owns its
    parent/operation registry. ZIP members are data-dir-relative only after
    validating their archive ``data/`` namespace.
    """
    created = False
    destination = Path(destination)
    try:
        _limits(limits)
        _cancel(cancel)
        # Check arbitrary archive/folder paths using Task1's ancestor checks.
        layout = resolve_layout(destination)
        destination = layout.data_dir.parent
        archive = _checked_path(Path(archive))
        _check(archive.is_file())
        if destination.exists():
            raise _error("destination_exists")
        _check(not archive.is_relative_to(destination))
        archive_hash, actual = hashlib.sha256(), 0
        with archive.open("rb") as source:
            while data := source.read(_CHUNK):
                _cancel(cancel)
                actual += len(data)
                _check(actual <= limits.max_archive_bytes, "resource_limit")
                archive_hash.update(data)
            with _zip_inventory(source, actual, limits, cancel, layout) as zipped:
                infos = zipped.infolist()
                _check(bool(infos) and infos[0].filename == "manifest.json")
                _check(len(infos) <= limits.max_files, "resource_limit")
                seen = set()
                for info in infos:
                    key = member_path_key(info.filename)
                    _check(key not in seen)
                    seen.add(key)
                    _check(info.file_size <= _file_limit(info.filename, limits), "resource_limit")
                offsets = _zip_ranges(source, zipped, infos)
                budget = _Budget(limits, cancel)
                content = bytearray()
                _inflate(source, infos[0], offsets["manifest.json"], limits.max_manifest_bytes, budget, content.extend)
                manifest = _json(content)
                files, directories = _manifest(manifest, limits)
                _check(set(files) == {info.filename for info in infos[1:]})
                for info in infos[1:]:
                    _check(files[info.filename]["size"] == info.file_size)
                declared = len(content) + sum(entry["size"] for entry in files.values())
                _check(declared <= limits.max_unpacked_bytes, "resource_limit")
                require_space(destination, declared, limits)
                _cancel(cancel)
                destination.mkdir()  # exclusive: FileExistsError does not own it
                created = True
                layout = resolve_layout(destination)
                # Preserve metadata; inspect_snapshot will recompute its counts.
                _checked_path(destination / "manifest.json")
                with (destination / "manifest.json").open("xb") as target:
                    target.write(content)
                for name in directories:
                    _cancel(cancel)
                    owned_path(layout, name.removeprefix("data/")).mkdir(parents=True, exist_ok=True)
                for info in infos[1:]:
                    _cancel(cancel)
                    relative = info.filename.removeprefix("data/")
                    target = owned_path(layout, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with owned_path(layout, relative).open("xb") as sink:
                        size, digest = _inflate(source, info, offsets[info.filename],
                                                _file_limit(info.filename, limits), budget, sink.write)
                    _check(size == files[info.filename]["size"] and digest == files[info.filename]["sha256"])
                    progress("validate", budget.total, declared)
                summary = _inspect(destination, includes_photos=manifest["includes_photos"], limits=limits, cancel=cancel)
                _check(summary.items_present == manifest["items_present"] and summary.counts == manifest["counts"])
                _cancel(cancel)
                progress("validate", declared, declared)
                return ValidatedArchive(Snapshot(destination, summary), archive_hash.hexdigest())
    except (OSError, sqlite3.Error, zipfile.BadZipFile, zlib.error, ValueError, TypeError,
            KeyError, OverflowError, RecursionError, struct.error):
        raise _error() from None
    finally:
        if created:
            # A returned snapshot is now caller-owned; errors/cancellation never
            # leave a partial directory. Do not follow a substituted junction.
            import sys
            if sys.exc_info()[0] is not None:
                resolve_layout(destination)
                shutil.rmtree(destination)
