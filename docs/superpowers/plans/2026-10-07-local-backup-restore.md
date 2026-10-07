# Tradewind 本地业务备份与恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设置页提供不含凭据的业务快照备份、安全预览和重启恢复，保护客户/跟进关联，并能在恢复失败时回到原数据。

**Architecture:** 归档校验、维护协调和启动恢复分开，全部使用程序拥有的暂存目录与固定业务白名单。维护租约覆盖请求和实际后台线程；恢复采用同卷移位与写前日志，不假设多个文件可以原子替换。前端数据版本防止旧页面操作恢复后的对象。

**Tech Stack:** Windows、Python 3.13、pathlib、sqlite3、zipfile、FastAPI/Starlette、React 18、TypeScript、现有 Playwright/Edge、unittest；无新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-10-07-local-backup-restore-design.md`，用户于 2026-10-07 确认。

**Status:** 用户于 2026-10-07 确认计划，选择分任务实现与逐项独立复审；实现及验收进行中。

## Global Constraints

- 第一轮只交付源码网页功能，不构建 EXE、绿色包、Tauri 或 sidecar；用户验收后另做绿色版发布。
- 设置页入口，照片可选且默认不勾选；不新增 Tab、不改视觉提示词、模型参数或正常发送逻辑。
- 不导出 `.env`、`config.json`、公司/模型/SMTP 设置、Windows 凭据、日志、checkpoint、localStorage、旧备份或运行任务。
- 含照片恢复替换 `crawler_photos/` 和 `photo_scan/`；不含照片恢复保留当前照片。
- 快照恢复替换白名单业务数据，但禁止联系规则取并集；恢复前完整救援成功才可准备，重启生效。
- 上传 ZIP 2 GiB、总解压量 8 GiB、普通文件 20,000 个、单张照片 20 MiB、单份 JSON 32 MiB、单个数据库 512 MiB、清单 8 MiB；空间预算再加 256 MiB 余量。
- 预览暂存有效期 30 分钟、同一时间只允许一个预览；禁止静默截断、自动淘汰救援或恢复后续跑消息。
- 自动测试只用临时根目录、随机回环端口、合成数据；不访问真实凭据、模型、SMTP、WhatsApp 或用户运行数据。
- 现有数据目录和两份 2026-09-15 未跟踪业务设计文档均不覆盖、不暂存；阶段提交使用具体文件，不用 `git add .`。

## Review Focus

1. 用户关闭程序时正处于文件移位与日志落盘之间：下次启动必须完整回滚，数据版本也回滚（任务 5）。
2. 中文店名、NFC/NFD、大小写重名和 Windows junction：合法中文图片往返，路径别名或链接拒绝（任务 1/2）。
3. HTTP 已返回或任务已显示取消，但后台线程仍在写入：备份仍拒绝，不能复制不一致数据（任务 3/6）。
4. 2 GiB 下载和客户端中断：浏览器不把整个 ZIP 转成 Blob，服务端不删除正在下载的材料（任务 4/6/8）。
5. 换电脑未配置 Key、同机恢复后还开着旧表单：资料可查看且无需外部服务；旧表单可复制但不能改新数据（任务 7/8/9）。

## 文件与公共接口地图

新增 `app/backup/`：`models.py` 定义契约/限制，`paths.py` 管理白名单和拥有目录，`validation.py` 严格校验，`maintenance.py` 管理租约/版本，`archive.py` 生成快照与 ZIP，`restore.py` 管理准备/日志/回滚，`service.py` 管理操作/预览/救援生命周期，`http.py` 提供薄路由和 ASGI 门禁。`__init__.py` 不执行初始化，不导入 config、模型、凭据或发送器。

公共类型统一放 `models.py`；以下名称/字段在各任务中不可自行改名：

- `CancelCheck = Callable[[], bool]`；`ProgressSink = Callable[[str, int, int], None]`，对应阶段、完成量、总量。
- `BackupError(code: str, message: str)`、`BackupCancelled`；对外只返回白名单错误码/中文说明，不返回异常原文。
- `BackupLimits(max_archive_bytes=2*1024**3, max_unpacked_bytes=8*1024**3, max_files=20_000, max_photo_bytes=20*1024**2, max_json_bytes=32*1024**2, max_db_bytes=512*1024**2, max_manifest_bytes=8*1024**2, space_margin_bytes=256*1024**2)`；字段均为 int，测试允许注入更小上限。
- `BackupLayout(data_dir: Path, work_dir: Path, rescue_dir: Path, epoch_file: Path)`；目录分别为 `root/data`、`data/.backup-work`、`data/backups`、`data/.data-epoch.json`。
- `BackupSummary(format_version: int, app_version: str, created_at: str, includes_photos: bool, items_present: dict[str, bool], counts: dict[str, int])`。
- `counts` 固定键：`customers/products/templates/crawler_seen/emails/outreach_jobs/suppressions/followup_records/followup_deleted_requests/followup_plans/active_plans/photos`；无该数据项为 0。
- `items_present` 固定键：`customers/products/templates/memory/crawler_seen/crawler_photos/photo_scan`；照片不入包时两个照片项为 false。
- `Snapshot(folder: Path, summary: BackupSummary)`；`ValidatedArchive(snapshot: Snapshot, archive_sha256: str)`；所有 Path 都只供内部使用。
- `EpochState(epoch: str, require_header: bool)`；`RestorePreview(id: str, archive_sha256: str, epoch: str, revision: int, expires_at: float, backup: BackupSummary, current: BackupSummary)`。
- `PreparedRestore(operation_id: str, rescue_id: str, status: Literal['prepared'])`；`RestoreOutcome(status: Literal['none','restored','rolled_back','blocked'], operation_id: str, code: str, counts: dict[str,int])`。
- `OperationView(id: str, kind: Literal['export','preview','prepare'], status: Literal['running','done','cancelled','error'], phase: str, completed: int, total: int, code: str, message: str, preview: RestorePreview | None, prepared: PreparedRestore | None)`；不暴露文件路径。
- `RescueView(id: str, created_at: str, size_bytes: int, active: bool)`；`BackupState(mode: Literal['ready','snapshot','prepare','prepared','blocked'], epoch: str, require_header: bool, revision: int, prepared: PreparedRestore | None, rescues: list[RescueView], report: RestoreOutcome, estimated_bytes: int, photo_count: int)`；字段一致映射到前端类型。

清单路径固定为 `manifest.json` 与 `data/<业务白名单相对路径>`。文件条目记录 `path/size/sha256`，不写显式目录 ZIP 条目；照片空目录由清单 `directories` 列表记录并同样验证。

Python 验证命令在正确 Python 3.13 环境执行，文档下文用 `python` 表示该环境，不能使用 PATH 上的 LibreOffice Python。任务1–6的 Step2/Step4 命令为 `python -m unittest discover -s tests -p '<该任务Test所列文件名>' -v`；有多个测试文件时逐一执行。红灯必须来自本次尚未实现的接口或行为断言，错误Python、现有依赖不可用等环境错误不算红灯；绿灯必须每个命令退出0。在测试或构建后提交前，仅检查并提交该任务文件。

## Task 1: 格式契约、白名单与 Windows 文件路径

**Files:** Create `app/backup/__init__.py`, `app/backup/models.py`, `app/backup/paths.py`; Test `tests/test_backup_paths.py`。

**Interfaces:** `resolve_layout(root: Path) -> BackupLayout`；`validate_member_path(name: str) -> str` 返回规范 POSIX 相对名；`owned_path(layout: BackupLayout, relative: str) -> Path`；`check_memory_path(layout: BackupLayout, configured: Path) -> None`；`require_space(path: Path, additional_bytes: int, limits: BackupLimits) -> None`。

- [ ] **Step 1 — 写失败测试。** `test_limits_match_spec` 断言七个上限的精确值；`test_chinese_photo_path_allowed` 断言 `data/crawler_photos/合成甲店（九龙）/01.jpg` 往返不改名；参数化 `test_unsafe_windows_paths_rejected` 覆盖 `../x`、盘符、UNC、反斜杠、ADS、`CON.jpg`、尾点/空格、控制字符。`test_custom_memory_path_rejected` 检查另一个目录及同目录另一文件都拒绝。`test_junction_in_owned_ancestor_rejected` 创建临时 NTFS junction 或注入重解析属性，确认链接目标无新增文件。`test_space_margin_required` 在恰好不足 256 MiB 余量时拒绝。

```python
assert BackupLimits().max_archive_bytes == 2 * 1024**3
assert BackupLimits().max_files == 20_000
assert validate_member_path('data/crawler_photos/合成甲店（九龙）/01.jpg') == 'data/crawler_photos/合成甲店（九龙）/01.jpg'
```
- [ ] **Step 2 — 跑 `test_backup_paths.py`。** 预期因模块/函数未实现或上述行为缺失失败；保存红灯输出到忽略的 `outputs/backup-acceptance/`。
- [ ] **Step 3 — 实现上述接口及公共类型。** 校验每个已存在的祖先与目标 `lstat`/Windows 重解析属性，不只调用 `Path.resolve()`；路径重复比较使用 Unicode NFC 后 `casefold`。拒绝不属于白名单的恢复目标；清理只能接收程序登记的目录，不接收调用者任意路径。
- [ ] **Step 4 — 重跑同文件。** 全部通过；使用很小的注入上限，不创建 GiB 测试文件。
- [ ] **Step 5 — 独立审查路径保护并提交。** `feat: define safe local backup format and paths`；不访问现有 `data/`。

## Task 2: 严格归档、JSON 与 SQLite 校验

**Files:** Create `app/backup/validation.py`, `tests/backup_helpers.py`; Test `tests/test_backup_validation.py`。

**Interfaces:** `validate_archive(archive: Path, destination: Path, *, limits: BackupLimits, cancel: CancelCheck, progress: ProgressSink) -> ValidatedArchive`；`inspect_snapshot(folder: Path, *, includes_photos: bool, limits: BackupLimits) -> BackupSummary`。前者只能写拥有的 destination，后者不写业务源。

`tests/backup_helpers.py` 定义 `make_business_fixture(root: Path) -> BackupLayout`、`business_digest(layout: BackupLayout) -> dict[str,str]`、`make_archive(layout: BackupLayout, destination: Path, *, photos: bool) -> Path` 和 `open_fixture_server(layout: BackupLayout)` 上下文。夹具只用虚构商家、`.example/.test` 目标、固定日期和小图片；schema 夹具从当前 memory/outreach/followup 表定义产生，先设置临时环境再懒导入业务模块。HTTP 上下文由任务 6 补全，归档夹具不调用尚不存在的生产导出器。

- [ ] **Step 1 — 写失败测试。** `test_valid_archive_preserves_ids_counts_and_empty_directories` 验证所有固定键与序列；`test_missing_and_empty_are_distinct` 验证不存在文件标记而非默认空值。`test_nfc_case_alias_members_rejected`、`test_crc_hash_missing_extra_encrypted_and_future_schema_rejected`、`test_malformed_json_and_duplicate_customer_ids_rejected`、`test_orphan_followup_and_duplicate_active_plan_rejected`、`test_outreach_history_for_deleted_customer_allowed`。`test_sqlite_trigger_view_virtual_table_and_unknown_table_rejected` 断言恶意副作用文件未创建。`test_actual_stream_limits_and_cancel_clean_staging` 检查读入实际字节、文件数与清单上限，超限/取消无残留且源摘要不变。

```python
# 删除 fixture 中 products.json，保留 customers.json=[] 后验证合法清单。
assert validated.snapshot.summary.items_present['products'] is False
assert validated.snapshot.summary.items_present['customers'] is True
assert validated.snapshot.summary.counts['customers'] == 0
assert business_digest(layout) == before_validation
```
- [ ] **Step 2 — 跑 `test_backup_validation.py` 得到红灯。** 不准把无效文件送入现有宽松 `_read_json` 或 `OutreachStore` 初始化器。
- [ ] **Step 3 — 实现严格验证。** ZIP 仅支持 STORED/DEFLATED、非加密普通条目；不使用 `extractall`。先核对清单集合/路径/实际资源，再逐块解压、CRC/哈希验证。SQLite 使用只读 URI、关闭扩展、`trusted_schema=OFF`、语句权限限制和查询预算；检查对象/列/约束/索引/业务枚举/唯一性后查询业务内容，绝不执行上传 SQL。已知缺失旧表/字段仅接受受测、列明的兼容版本；未来或未知 schema 拒绝。
- [ ] **Step 4 — 同文件绿灯。** `inspect_snapshot` 的数量从内容重算，篡改清单数量不能通过。审查枚举对齐 `app/customer_followup.py` 与 `app/outreach_store.py`，不建立旧外联必须引用现存客户的错误限制。
- [ ] **Step 5 — 审查校验器并提交。** `feat: validate business backup archives before restore`。

## Task 3: 维护租约、后台工作票与数据版本

**Files:** Create `app/backup/maintenance.py`; Test `tests/test_backup_maintenance.py`。

**Interfaces:** `MaintenanceCoordinator(layout: BackupLayout)`；`.service(epoch: str | None, *, mutating: bool) -> ContextManager[None]`；`.reserve_worker(kind: str, *, mutating: bool) -> WorkerTicket`；`.reserve_exclusive(kind: Literal['snapshot','prepare']) -> MaintenanceLease`；`.exclusive(kind: Literal['snapshot','prepare']) -> ContextManager[MaintenanceLease]`；`.hold_prepared(operation_id: str) -> None`；`.cancel_prepared(operation_id: str) -> None`；`.version() -> tuple[EpochState,int]`。`WorkerTicket.run(target: Callable[...,None], args: tuple = ()) -> None` 在 finally 释放，`.abort_start() -> None` 处理线程未启动，均幂等。`MaintenanceLease.assert_active() -> None` 验证绑定协调器、代次与未释放状态；`.release() -> None` 幂等。reserve_exclusive 原子预留后可把租约交给备份内部线程，exclusive 是其上下文封装；快照/准备共享同一租约，不嵌套申请独占。

- [ ] **Step 1 — 写失败测试。** `test_active_request_or_worker_rejects_exclusive_without_wait`；`test_worker_reservation_survives_parent_http_completion`；`test_ui_terminal_status_does_not_release_real_worker`；`test_worker_start_failure_releases_ticket`；`test_creation_race_has_one_winner` 使用 Event/Barrier，不靠 sleep。`test_revision_changes_on_possible_partial_write_and_worker_finish`；`test_prepared_blocks_business_but_can_cancel`；`test_epoch_header_optional_before_restore_required_after_restore`；`test_failed_epoch_check_does_not_allocate_lease`。

```python
ticket = coordinator.reserve_worker('photo', mutating=True)
with self.assertRaises(BackupError) as caught:
    with coordinator.exclusive('snapshot'):
        self.fail('worker 尚未退出，不应获得独占')
assert caught.exception.code == 'maintenance_busy'
ticket.abort_start()
```
- [ ] **Step 2 — 跑 `test_backup_maintenance.py` 得到红灯。** 断言恢复版本旧/缺失均抛 `BackupError(code='stale_epoch')`，维护冲突为 `maintenance_busy`，不泄漏占用数。
- [ ] **Step 3 — 实现锁内入口登记。** 工作票必须在 `Thread.start()` 之前登记、覆盖 worker 的实际收尾，不能按任务 status 释放。业务可能写入时进入/退出都推进 revision，失败后也使旧预览失效。正常业务租约可以并发；独占立即成功或失败，不阻塞排队，不强杀线程。
- [ ] **Step 4 — 重跑得到绿灯。** 版本初始 require_header=false；成功恢复后的持久化版本由任务 5 写入，协调器只读取已验证版本文件。prepared 取消后回 ready，但不会改变 epoch。
- [ ] **Step 4a — 验证持久维护交接。** `test_exclusive_exit_retains_prepared_hold`：exclusive 内 hold_prepared 后退出不能回 ready；cancel_prepared 才释放持久维护状态。
- [ ] **Step 5 — 审查竞争与释放路径并提交。** `feat: coordinate backup maintenance and stale data versions`。

## Task 4: 一致快照、照片可选与流式 ZIP

**Files:** Create `app/backup/archive.py`; Test `tests/test_backup_archive.py`。

**Interfaces:** `capture_snapshot(layout: BackupLayout, *, memory_path: Path, includes_photos: bool, destination: Path, limits: BackupLimits, lease: MaintenanceLease, cancel: CancelCheck, progress: ProgressSink) -> Snapshot`；`write_archive(snapshot: Snapshot, destination: Path, *, limits: BackupLimits, cancel: CancelCheck, progress: ProgressSink) -> Path`。调用方持有任务3独占租约，capture 仅 assert_active；prepare 不二次申请租约，避免救援快照死锁。

- [ ] **Step 1 — 写失败测试。** `test_roundtrip_all_business_tables_ids_and_sequences`；`test_default_excludes_photos_config_logs_workflows_and_old_backups` 在各排除文件放不同合成私密标记，断言 ZIP 清单与解压文件均不含它们。`test_wal_committed_rows_captured_without_sidecars`；`test_empty_and_missing_json_recorded_without_live_initialization`；`test_optional_photo_bytes_names_and_empty_stores_preserved`；`test_non_photo_file_is_visible_error_not_silent_skip`；`test_source_changes_abort_snapshot`；`test_cancel_space_error_and_zip_limit_leave_source_unchanged`；`test_compression_runs_after_exclusive_lease_release`。

```python
assert snapshot.summary.includes_photos is False
assert 'data/config.json' not in archive_names
assert not any(name.endswith(('-wal', '-shm', '-journal')) for name in archive_names)
assert 'synthetic-config-private-marker' not in extracted_business_text
```
- [ ] **Step 2 — 跑 `test_backup_archive.py` 得到红灯。** 通过 mock chunk reader 检查单次读 <= 1 MiB，不分配 GiB 内容。
- [ ] **Step 3 — 实现快照与归档。** capture 在独占内记录来源文件集合/状态，严格读 JSON，SQLite 使用只读源 + `Connection.backup()`，仅在独立快照 `VACUUM` 去除自由页残留，再复制可选照片，确认来源未变并验证。未发现源库时只记录缺失，不在真实目录建库。write 先写 manifest，再按规范路径排序分块写业务项、不重新读取 live 目录，先临时文件后发布，失败/取消清理自有材料。估算空间与资源超限必须在结果发布前检查。
- [ ] **Step 4 — 全部绿灯并验证解压。** capture 释放后可以正常业务读写，ZIP 内容仍固定；未知照片文件提示处理，不删除它。
- [ ] **Step 5 — 审查快照一致性并提交。** `feat: export consistent local business snapshots`。

## Task 5: 准备恢复、保守外联与崩溃回滚

**Files:** Create `app/backup/restore.py`; Test `tests/test_backup_restore.py`, `tests/test_backup_crash_recovery.py`。

**Interfaces:** `prepare_restore(layout: BackupLayout, validated: ValidatedArchive, preview: RestorePreview, *, request_id: str, coordinator: MaintenanceCoordinator, lease: MaintenanceLease, memory_path: Path, limits: BackupLimits, cancel: CancelCheck, progress: ProgressSink) -> PreparedRestore`；`cancel_prepared_restore(layout: BackupLayout, operation_id: str, coordinator: MaintenanceCoordinator) -> None`；`apply_pending_restore(layout: BackupLayout, *, memory_path: Path, limits: BackupLimits) -> RestoreOutcome`。prepare 使用调用方已经预留的 prepare 租约，不自行再加独占锁。

私有纯 SQLite helper `apply_contact_safety(restored_db: Path, rescue_db: Path) -> dict[str,int]`，不导入 SMTP、模型、checkpoint 或 credential_store。目录 `backups/<operation_id>/` 保存 rescue.zip、original/ 与固定 recovery.json；所有操作 ID 服务端生成。写前日志仅允许白名单动作与程序派生路径。

- [ ] **Step 1 — 写失败测试。** `test_prepare_requires_current_epoch_revision_valid_preview_and_rescue`；`test_prepare_is_idempotent_and_cancel_keeps_live_files`；`test_rescue_always_contains_current_photos`；`test_prepare_failure_does_not_hold_maintenance_or_publish_partial_rescue`。往返测试断言 config/.env 字节不变、缺失 JSON 写空列表、内存库缺失仍应用本机禁止规则、照片选项行为、旧 checkpoint 与侧文件移走但正常业务表 ID/序列保留。
- [ ] **Step 2 — 添加安全/中断红灯测试。** `test_local_suppression_union_keeps_current_reason_and_blocks_jobs`；`test_restored_email_requires_sentbox_check_and_whatsapp_requires_review` 精确断言四类邮件变 unknown、三类 WA 变 skipped、已有终态不升格。`test_every_journal_boundary_recovers_whole_old_or_committed_new_state` 在 intent 落盘前/后、rename 前/后、新文件发布、epoch 写入、commit 标记前/后逐项注入异常和子进程强制退出，比较业务摘要与 epoch；`test_tampered_journal_path_and_unrecoverable_rollback_fail_closed` 断言外部文件未写且 blocked 不启动正常 API。运行两个文件并保存预期失败。

```python
assert restored_pending_email['status'] == 'unknown'
assert restored_opened_whatsapp['status'] == 'skipped'
assert rollback_outcome.status == 'rolled_back'
assert business_digest(layout) == before_restore
assert layout.epoch_file.read_bytes() == before_epoch
```
- [ ] **Step 3 — 实现 prepare 与 cancel。** 在传入租约内校验 token/epoch/revision/30分钟 TTL，capture_snapshot 复用同一租约生成含照片救援，救援 ZIP 压缩也保持 prepare 独占；不能像普通 export 那样在救援压缩期间放开业务写入。收集禁止规则时只读已完成救援快照。救援再次校验通过、准备日志 fsync/原子发布成功后才 hold_prepared。重复同 request_id/同归档返回原准备，不同内容冲突。取消先持久化注销准备标记，再释放持久维护状态，清理自有待应用暂存但保留已完成救援，不替换 live 数据；注销失败维持 prepared，提示重试。
- [ ] **Step 4 — 实现 apply 与回滚。** 每项操作先持久化 intent（旧文件是否存在、固定动作、来源/目标校验、摘要），再同卷移位、发布新项、持久化 done；处理中所有未知状态按文件存在与摘要确认，不猜测任意路径。连同旧 memory/checkpoint 的 WAL/SHM/journal 和旧 epoch 一起纳入回滚，但不导出它们。先套用禁止联系并集，再保守转队列状态，验证整体关联，最后写新随机 require_header=true 的 epoch 与提交标记。未提交事务下次启动只回滚一次，不自动再次尝试恢复；完整提交后的清理不影响业务成功。所有阶段失败保留救援与唯一回滚材料。
- [ ] **Step 5 — 两文件绿灯后独立审查并提交。** `feat: restore business snapshots with rescue and crash rollback`；恢复中间状态不能报告成功或健康就绪。

## Task 6: 本地操作服务、HTTP 门禁与启动顺序

**Files:** Create `app/backup/service.py`, `app/backup/http.py`; Modify `server.py`, `run.py`, `app/outreach_store.py`, `.gitignore`; Test `tests/test_server_backup.py`, `tests/test_backup_startup.py`, `tests/test_server_security.py`, `tests/test_server_outreach.py`, `tests/test_launcher.py`; Complete `tests/backup_helpers.py` 的 HTTP 上下文。现有以 DormantThread 模拟后台的测试需注入独立 coordinator 并在 tearDown 关闭模拟票，不能让未运行的 fake worker 污染下一测试。

**Interfaces:** `BackupService(layout: BackupLayout, *, memory_path: Path, app_version: str, coordinator: MaintenanceCoordinator, limits: BackupLimits)`；`.state() -> BackupState`；`.start_export(includes_photos: bool) -> OperationView`；`.begin_upload() -> UploadTicket`；`.operation(id: str) -> OperationView`；`.cancel(id: str) -> None`；`.prepare(preview_id: str, epoch: str, request_id: str) -> OperationView`；`.cancel_restore(operation_id: str) -> None`；`.download(id: str, *, rescue: bool) -> ContextManager[Path]`；`.delete_rescue(id: str) -> None`。`UploadTicket.write(chunk: bytes) -> None` 每块计量落盘，`.finish() -> OperationView` 关闭并排队校验，`.abort() -> None` 幂等清理。preview 接收 `Content-Type: application/zip` 原始 File 流，逐块读取 Request.stream；不先让 multipart 解析器缓存无限量文件。备份内部操作线程不登记为业务 worker；它们按操作阶段取得业务独占租约。

`BackupMaintenanceMiddleware(app: ASGIApp, coordinator: MaintenanceCoordinator)` 是纯 ASGI 门禁，业务 lease 保持到响应流最终 body/客户端取消，不用 `call_next` 返回时提前释放。`start_registered_worker(coordinator: MaintenanceCoordinator, kind: str, target: Callable[...,None], args: tuple = ()) -> Thread` 实际调用工作票；线程启动异常用 abort_start 清理。

- [ ] **Step 1 — 写失败 API/启动测试。** 全部 spec 路由成功/错误/Origin/随机 ID 不存在/路径不暴露；预览仅一个、TTL、取消、上传/校验/下载中断、准备响应丢失重试、救援组删除包括原件、活动材料不能删。`test_streamed_response_and_download_hold_ownership_until_close`；`test_old_epoch_rejected_for_all_mutations_before_side_effects` 遍历新增/删除/审批/发送/配置/取消。`test_pending_restore_runs_before_seeding_db_import_and_orphan_cleanup` 子进程监测最早初始化顺序；`test_rolled_back_startup_uses_old_data_once`；`test_blocked_startup_does_not_report_health_ok`。
- [ ] **Step 2 — 写竞争/外联测试并跑红灯。** 在六类 worker（单封、批量、文档、照片、爬虫、SMTP）用可控 Event 让 HTTP 已结束但 worker 未退出，export/prepare 仍拒绝；Thread.start 失败后可再次备份。缺失客户的 send/open 拒绝且 FakeSMTP/WhatsApp 均未调用；历史列表仍可读。门禁请求 lease 与 worker 票同时存在不死锁。

```python
assert old_epoch_save.status_code == 409
assert old_epoch_save.json()['code'] == 'stale_epoch'
assert maintenance_export.status_code == 503
assert unknown_id_download.status_code == 404
assert business_digest(layout) == before_rejected_requests
```
- [ ] **Step 3 — 实现服务与路由。** 固定 HTTP 映射：维护冲突 503、stale epoch/revision/token 或幂等内容冲突 409、格式不合法 422、资源超限 413、磁盘不足 507、不存在操作 404；输出 `{detail, code}`。路由集合采用 spec §8，包括独立 cancel、救援下载/删除。start_export/prepare 先 reserve_exclusive 再启动内部线程，避免响应后其他工作抢先开始；prepare 的同请求幂等查询须在预留租约之前，才能在 prepared 状态重试返回原操作。线程启动失败释放租约，export 捕获完成后释放再压缩，prepare 则保持至持久维护交接。UploadTicket 断开/超量时 abort，校验与当前摘要都成功后才发布 RestorePreview；当前摘要读取也要短暂独占，否则返回可重试维护冲突。download context 通过响应包装器保持到 FileResponse 最终发送/取消，不能在路由返回之前退出；下载占用时取消/删除拒绝。导出未下载完成结果最多保留24小时，重启清理非救援遗留，救援无自动过期。
- [ ] **Step 4 — 接入早期恢复与全部执行入口。** 在 `server.py` 的 config 只读导入后、其他 app 导入/默认模板复制前调用 apply_pending_restore；确认 memory_path 是标准路径。再创建 coordinator/service、安装门禁与薄路由，origin 校验保留，CORS 放行新 header。ASGI 除 `/api/health` 与 `/api/backup/*` 控制接口外都登记业务租约；备份控制接口自行检查状态/版本，不能绕过保护。六处 Thread.start 使用登记函数，保留现有取消/任务结构；初始化/部分写入也推进 revision。blocked outcome 直接阻止正常应用启动；run.py 把导入 app 放进启动错误处理区，仅显示脱敏恢复错误与保留救援说明，不打印私密异常全文。缺失客户检查放 send/open 的业务锁内，不能只前端禁用按钮。
- [ ] **Step 5 — 跑相关新旧 API 测试得到绿灯。** 添加忽略规则 `data/.backup-work/`、`data/backups/`、`data/.data-epoch.json`，不删除已有文件。登记映射漏任何实际线程或响应流就不进入 UI 任务；审查后提交 `feat: integrate safe backup maintenance APIs and startup recovery`。

## Task 7: 前端版本固定、任务恢复清理与可见维护状态

**Files:** Create `frontend/src/dataEpoch.ts`, `frontend/src/components/DataMaintenanceNotice.tsx`; Modify `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/tasks.ts`, `frontend/src/App.tsx`; Test `scripts/test-backup-web.py` 的版本/维护组。

**Interfaces:** `initializeDataEpoch(): Promise<void>` 单页面只初始化一次；`getPageEpoch(): string | null`；`subscribeDataSafety(listener: (state: DataSafetyState) => void): () => void`；`markDataStale(): void`。`DataSafetyState` 为 `{mode:'ready'|'maintenance'|'prepared'|'stale'|'blocked', message:string}`。`resetTaskReferencesForEpoch(epoch: string): void` 清理旧内存/计时器与 compose localStorage，仅新页面初始化时调用，不修改后端记录。

- [ ] **Step 1 — 建立隔离浏览器红灯组。** `test_fresh_page_attaches_epoch_to_json_delete_and_multipart` 检查所有变更路径 header；`test_old_page_keeps_draft_and_never_refreshes_epoch_for_submit` 留跟进/写信草稿后重启恢复，断言输入保留、无副作用；`test_old_compose_task_id_not_resumed_on_new_epoch`；`test_maintenance_error_is_notice_not_empty_data`。使用随机端口的合成服务，不启动8101。
- [ ] **Step 2 — 建立缺 Key 红灯组并运行。** `test_no_key_can_read_customers_settings_and_backup_without_model_calls`：不能因现有 setup 屏挡住全部已恢复资料；首次无 Key 页面仍可进设置/客户，点击生成才提示配置。它是恢复验收需要的窄兼容调整，不删除原首次配置入口。

```python
expect(page.get_by_label('沟通内容', exact=True)).to_have_value('未保存的合成草稿')
assert all(request['epoch'] == page_epoch_before_restore for request in old_page_mutations)
assert not external_requests
assert not resumed_old_task_ids
```
- [ ] **Step 3 — 实现版本固定与错误分流。** App 业务组件挂载前初始化 epoch；api req 合并 header（不可覆盖 multipart boundary），仅 code=stale_epoch 的409标 stale 并拒绝后续修改，不把一般跟进冲突当恢复。maintenance503保持当前数据/草稿而显示通知；GET backup.state 不应重绑定旧页面 epoch。prepared 状态先显示备份控制区，不循环读取已被维护门禁阻止的 config 并误入 setup。新页面比较 localStorage 的任务版本后清理旧引用，同版本保留原 compose 恢复语义；后台轮询恢复正常时不产生空名单。
- [ ] **Step 4 — TypeScript 构建与上述组绿灯。** `npm run build:check`（frontend工作目录）以及 `python scripts/test-backup-web.py --group safety`；未保存跟进防丢稿、运行中切 Tab、既有 setup 流程回归。
- [ ] **Step 5 — 审查跨页保护并提交。** `feat: prevent stale browser actions after data restore`。

## Task 8: 设置页业务备份卡与恢复确认

**Files:** Create `frontend/src/components/BackupSettingsCard.tsx`; Modify `frontend/src/tabs/SettingsTab.tsx`, `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/styles.css`; Test `scripts/test-backup-web.py` 的 UI/下载组。

**Interfaces:** `BackupSettingsCard(): JSX.Element`；api 新增 `backup.getState(): Promise<BackupState>`、`startExport(includesPhotos: boolean): Promise<OperationView>`、`getOperation(id: string): Promise<OperationView>`、`cancelOperation(id: string): Promise<{ok:true}>`、`startPreview(file: File, signal: AbortSignal): Promise<OperationView>`、`prepareRestore(previewId: string, epoch: string, requestId: string): Promise<OperationView>`、`cancelRestore(operationId: string): Promise<{ok:true}>`、`deleteRescue(id: string): Promise<{ok:true}>`；`backupDownloadUrl(id: string, rescue: boolean): string` 使用 apiUrl，供直接浏览器下载。

- [ ] **Step 1 — 写浏览器失败组。** `test_backup_and_diagnostics_are_separate`；`test_photos_default_false_and_download_is_browser_stream_not_blob` 捕获 download 事件并验证ZIP，禁止调用业务备份 `.blob()`；`test_preview_counts_photo_rule_and_confirm_word`：输入“恢复”前提交禁用，取消不变数据；`test_upload_cancel_errors_retries_and_lost_prepare_response` 保留可复用 request_id，避免重复救援；`test_prepared_can_download_rescue_or_cancel_before_restart`。

```python
expect(page.get_by_role('checkbox', name='包含照片', exact=True)).not_to_be_checked()
expect(page.get_by_role('button', name='确认恢复', exact=True)).to_be_disabled()
assert downloaded_zip_names[0] == 'manifest.json'
assert len(rescue_ids_after_retried_prepare) == 1
```
- [ ] **Step 2 — 写响应式/管理失败组并运行。** `test_focus_escape_and_scroll_at_375_768_1440`；`test_completed_rescue_group_delete_confirmation_and_active_delete_denied`；`test_new_machine_notice_explains_reconfigure_keys_and_no_drafts`。上传使用 XMLHttpRequest 的原始 File body 得到真实字节进度，stage进度来自服务端，不显示假百分比；既有文档导入继续 multipart。
- [ ] **Step 3 — 实现卡片及中文提示。** 放在“本地诊断与售后”附近，不合并两个导出按钮。明确“业务备份含客户资料，请勿公开分享”“模型/签名/邮箱设置不在备份内”“未审核草稿请先复制保存”。确认框逐项说明照片替换/保留、禁止规则并集、邮件核查与重启生效；准备后显示手动关闭/重新打开说明，不自动 kill/restart。复用 ConfirmDialog 与滚动锁，不重做其他布局。
- [ ] **Step 4 — 构建与 `--group ui` 全绿。** ZIP使用原生浏览器下载 URL，不分配大型 Blob；UI下载错误由状态和事件显示，下载中材料不能删除。上传中断/取消恢复可操作按钮，危险操作不靠 toast 猜成功。
- [ ] **Step 5 — 审查设置卡并提交。** `feat: add business backup and restore controls to settings`。

## Task 9: 故障验收、既有功能回归与源码交付

**Files:** Create `scripts/test-backup-core.py`; Complete `scripts/test-backup-web.py`; Modify `README.md`, `OPS-NOTES.md`。本任务不修改构建链或产出新包。

**Interfaces:** `python scripts/test-backup-core.py` 先建立临时根目录、剥离真实服务 Key/路径环境变量，再在子进程运行完整 unittest；结果失败返回非零，自动清理自有临时数据。浏览器脚本支持 `--group safety|ui|all`，生成仅含合成信息的 `outputs/backup-acceptance/report.json` 与小屏截图。测试子进程只添加其自有 Origin，并禁止所有非随机回环地址网络请求。

- [ ] **Step 1 — 补完整用户链路与中断用例。** 从客户/跟进/队列/照片 A 导出→改成 B→预览/救援→等待重启→新进程恢复 A（保护例外除外）→另一次使用救援恢复 B。验证目标配置原样、source root/便携 data root 不串、页面仍开着的旧修改拒绝、原始 checkpoint 不重放。`test_unknown_mail_retry_still_requires_sentbox_decision`、`test_restore_with_missing_memory_preserves_local_suppression`、`test_complete_response_disconnect_never_announces_partial_success` 固定关键断言；先跑有意失败的测试确认它们能检出缺陷。

```python
assert restored_customer_ids == original_customer_ids
assert restored_followup_record_ids == original_followup_record_ids
assert config_after_restore == config_before_restore
assert blocked_send_attempts > 0 and real_send_calls == 0
assert external_requests == []
```
- [ ] **Step 2 — 完整回归。** `python scripts/test-backup-core.py`；`python -m compileall -q server.py app tests scripts/test-backup-core.py scripts/test-backup-web.py`；frontend `npm run build:check`；`python scripts/test-backup-web.py --group all`；原 `scripts/test-followup-web.py` 和 `scripts/test-followup-regressions.py`。测试证据记录实际数量/结果，不沿用旧175/27数值或把尚未运行的测试写成通过。
- [ ] **Step 3 — 文档与源码安全检查。** README解释范围、照片、Key跨机、人工重启及非脱敏ZIP；OPS记录维护/修订/epoch、自定义内存库拒绝、错误码、救援管理和崩溃恢复。运行 `git diff --check`、只对当前源码/测试/文档的私密信息检查与提交钩子；核实无 data runtime/binaries/ZIP 被暂存。输出报告列明未读/未改实际业务数据、未联系外部服务、未构建发行包。
- [ ] **Step 4 — 整分支独立代码复审。** 检查任务接口、恢复日志写入顺序、Windows运行侧文件、门禁漏入口、JSON/SQLite关系、数据版本旧请求与下载生命周期。反馈先验证再修复，执行对应红绿测试与受影响回归；阻断项清零才交付。
- [ ] **Step 5 — 保存源码检查点并交用户测试。** `test: verify local backup restore and existing workflows`；只在用户要求时推送，不自动合并 main。用户先测网页备份/恢复；未确认前不重启其运行实例、不构建绿色包。

## 后续绿色版发布门槛（不属于本计划的立即执行任务）

用户确认源码网页验收后，调用项目发布技能与脱敏流程，再更新浏览器绿色版；不得处理 Tauri sidecar。发布 smoke 要在隔离临时根目录执行新 EXE 的业务导出、准备、关闭、重启恢复与救援回滚，检测ZIP/版本/SHA256；最终再做独立 Windows 人工验收。此阶段需要另行确认执行，不能用本次计划批准推定正在使用的数据可以被恢复或覆盖。

## 设计覆盖与计划自查

- Spec §1–3 → 全局约束、任务1/2/4/5/8：业务范围、缺失/空值、照片、设置/草稿排除。
- Spec §4 → 任务1/2/4：路径、schema、资源、空间、实际字节/哈希验证。
- Spec §5 → 任务3/4/6：请求与真实worker租约、一致快照、外部直写检测。
- Spec §6 → 任务5/6/8：预览修订、救援、幂等准备、启动前恢复、崩溃回滚、取消与材料组删除。
- Spec §7 → 任务5/6/7：禁止规则并集、发送核查、无客户发送拒绝、固定页面版本与任务引用清理。
- Spec §8–10 → 任务6–9：接口、设置卡、实际浏览器、离线完整回归、源码验收后才发布。
- 五项 Review Focus 已分别绑定任务里的命名测试；每项任务都有失败测试、绿灯命令和独立可审查结果。
- 类型/方法以后续任务的 Interfaces 为依赖契约；服务端预览30分钟与前端轮询不得另设不同TTL。准备状态和操作状态分别表示，不能混用任务 done 与恢复已经应用。
- 首版历史schema兼容只接受列明并测试的版本；不承诺任意旧包可恢复。实现过程中发现设计必须调整时先回报，不靠吞异常或扩大白名单通过验收。

## 执行方式

- 分任务实现并逐项独立复审（推荐）：按9个任务推进，每个交付都复审，最后整分支复审；复审上下文较多，但业务恢复的风险更早暴露。
- 当前会话连续实现：由主代理完成全部任务，最后一次独立整分支复审；沟通和上下文成本较低，早期任务不单独经过独立复审。

用户已确认采用第一种方式。执行进度由本计划专属 SDD ledger 记录；完成自动测试后再交用户网页验收。
