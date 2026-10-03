# Customer Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在客户名单内部实现本地沟通记录、一个有效的下次安排、今日/逾期筛选和可核查的联系时间线，并自动测试验收。

**Architecture:** 客户 JSON 保持原结构；在原 SQLite 文件增加独立跟进表，由新存储模块负责事务和幂等。FastAPI 提供客户级接口，React 客户名单使用摘要接口和独立详情面板。外联信息只读投影，不触发发送。

**Tech Stack:** Python 3.13、sqlite3、FastAPI/Pydantic、unittest、React 18、TypeScript、Vite、已有 Playwright 验收能力。

**Spec:** `docs/superpowers/specs/2026-10-03-customer-followup-design.md`

## Global Constraints

- 用户已确认设计并明确要求直接完成后自动验收，因此计划写完后由本会话直接执行，不再停留于计划确认。
- 在当前目录的 `codex/customer-followup` 本地分支实现，不新增 checkout，不提交或推送。
- 不改写现有客户 ID；新增客户使用 `c-` 加 UUID。
- 不自动发信，不读取收件箱，不调用真实模型，不建设遥测，不打包 EXE/ZIP/sidecar。
- 人工跟进内容上限 4000 字符，安排事项上限 500 字符；日期 `YYYY-MM-DD`，沟通时间包含时区。
- 同客户只有一个 `active` 安排；状态 `active/completed/cancelled/superseded`。
- 先观察测试失败，再实现对应行为；所有测试使用临时或隔离的合成数据。
- 现有仪器备注不混入跟进内容；诊断包不导出跟进内容。

## Review Focus

- JSON 损坏/重复 ID：拒绝写入和启动清理，不能把读取失败当成空客户名单。
- 并发/重试：相同请求返回原结果，过期安排 ID 返回冲突，不完成新的安排。
- 客户删除：名单成功写入后清理新跟进表，数据库清理失败显式提示且允许重试。
- 长历史/模糊外联：客户过滤在分页前完成，失败提交时间不标成发送成功。
- 未保存界面：关闭和切 Tab 需确认，失败不清空输入，隐藏客户不进入批量删除。

---

### Task 1: Durable follow-up store

**Files:** Create `app/customer_followup.py`, `tests/test_customer_followup.py`.

**Interfaces:**
- `FollowupStore(db_path: Path)`.
- `add_record(customer_id: str, customer_name: str, *, contacted_at: float, channel: str, outcome: str, content: str, request_id: str) -> dict`.
- `delete_record(customer_id: str, record_id: int) -> None`.
- `set_plan(customer_id: str, customer_name: str, *, due_date: str, action: str, request_id: str, expected_plan_id: int | None) -> dict`.
- `close_plan(customer_id: str, plan_id: int, status: str) -> dict`.
- `active_plan(customer_id: str) -> dict | None`, `summaries(customer_ids: list[str], today: str) -> dict[str, dict]`.
- `timeline(customer_id: str, *, limit: int = 50, offset: int = 0) -> dict`, including only local records/plans.
- `delete_customers(customer_ids: list[str]) -> None`, `prune_orphans(customer_ids: list[str]) -> None`.
- `FollowupConflict`, `FollowupNotFound` exceptions; invalid input raises `ValueError`.

- [x] Write tests asserting persistence/reopen, client request id dedup/conflict, channel/outcome/content/date limits, empty/invalid identities, record ownership, and plan replacement/completion/cancellation.
- [x] Run `python -m unittest discover -s tests -p test_customer_followup.py -v`; expected failure because module/behavior is missing.
- [x] Implement SQLite tables, partial unique active-plan index, `BEGIN IMMEDIATE` mutations, parameterized queries and bounded pagination.
- [x] Add and run two-window stale update/close, deleted record and no-active-plan boundary tests; expected all pass.

### Task 2: Customer-safe APIs and identities

**Files:** Modify `server.py`, `.gitignore`, `app/outreach_store.py`; create `tests/test_server_followup.py`; extend `tests/test_customers.py`.

**Interfaces:**
- `server._read_customers() -> list[dict]`: strict validation, missing file is initial empty list; corrupt/unreadable files raise HTTP 500 without exposing content/path.
- `server._CUSTOMERS_LOCK`: one `threading.RLock` used by customer read-modify-write and follow-up mutations.
- `server._followup_store() -> FollowupStore`, `server.cleanup_orphan_followups() -> dict`.
- HTTP paths exactly as the spec, response shapes: summary `{today, items}`, detail `{plan}`, timeline `{items, has_more, next_offset}`; mutation `{ok: true, item}`.
- Record request `{request_id, contacted_at: datetime-with-timezone, channel, outcome, content}`.
- Plan request `{request_id, due_date, action, expected_plan_id: number|null}`.
- Optional customer filter on `OutreachStore.list_jobs(..., customer_id=None)` preserves old callers.
- `server.customer_followup_timeline(customer_id: str, limit: int = 50, offset: int = 0) -> dict` projects local + outreach known timestamps before pagination.

- [x] Write tests for 404/409/422, foreign record/plan IDs, untrusted Origin, summary due classifications and persistence; run to see missing routes fail.
- [x] Write delete-max-then-create/import tests, corrupted JSON no-write tests, deleted customer cleanup/partial-failure/retry tests; run to see old ID reuse and missing cleanup fail.
- [x] Add request models/validation and route handlers, strict customer reads, lock all customer mutations, assign only new customer IDs with UUID, and preserve existing JSON fields.
- [x] Add only read-only external timeline projection; known timestamps/current-state labels are authoritative, not a fabricated audit trail.
- [x] Test pagination after more than 1000 unrelated outreach jobs, submitted-at failure semantics and identical timestamp tie breaking.
- [x] Add startup safe orphan cleanup; invalid JSON/duplicate IDs never cause cleanup. Ignore `*.db-wal`, `*.db-shm`, `*.db-journal` without deleting them.
- [x] Run store/API/customer/security/outreach tests; expected all pass.

### Task 3: Customer panel and navigation protection

**Files:** Modify `frontend/src/types.ts`, `api.ts`, `tabs/CustomersTab.tsx`, `App.tsx`, `styles.css`; create `components/CustomerFollowupPanel.tsx`, `followup.ts`.

**Interfaces:**
- `FollowupRecord`, `FollowupPlan`, `FollowupSummary`, `FollowupTimelineItem`, `FollowupTimelinePage` match backend response names.
- `api.followups.summary/get/timeline/addRecord/deleteRecord/setPlan/completePlan/cancelPlan`.
- Panel props `{customer: Customer, onClose: () => void, onChanged: () => void, onDirtyChange: (dirty: boolean) => void}`.
- `CustomersTab` optional `onFollowupDirtyChange` registers a guarded tab leave; `App` owns the pending destination and theme confirmation.
- `followup.ts` holds pure label/time/selection helpers and no persistence or remote calls.

- [x] Add automated browser acceptance script before UI implementation, asserting the absent follow-up entry cannot yet be used.
- [x] Implement typed endpoints and helpers, summary batch load and filters, compact customer date/action display and “跟进” entry.
- [x] Implement responsive portal panel: real labels, forms, own scrolling, data load retry, stable request IDs across failed retries, confirmation before deleting a manual record, plan complete/change/cancel.
- [x] Guard unsaved close/switch/Tab navigation, including Escape and browser unload; restore focus, trap keyboard only in the top dialog, keep busy actions from disappearing.
- [x] Refresh on focus/minute boundaries while customer page is mounted; use server classifications and cancel stale customer loads.
- [x] Reset selections on filter change and intersect with currently visible IDs after schedule changes.
- [x] Run `npm run build:check`; expected TypeScript and browser Vite build pass. No Tauri build.

### Task 4: Automated acceptance, regression and documentation

**Files:** Extend `tests/test_diagnostics.py`; create `scripts/test-followup-web.py`; update `README.md`, `OPS-NOTES.md`; write report under ignored `outputs/followup-acceptance/`.

**Interfaces:** Acceptance script uses isolated `TRADEWIND_DATA_DIR`, an unused loopback port and locally available Edge/Chrome; launches only the known temporary service it owns, terminates it in `finally`, and never uses 8101 or user credentials.

- [x] Add diagnostic fixture with private synthetic records/plans; assert ZIP bytes do not contain their markers. Run red/green with the feature implementation.
- [x] Execute browser flows: records, plans, date filters/source intersections, replacement/completion/cancellation, deletion, refresh/restart persistence, required errors, failure keeps drafts, double submission, keyboard and unsaved navigation.
- [x] Test at 375/768/1440 viewport sizes and capture screenshots; assert dialog rectangles stay in the viewport and body does not gain horizontal overflow from the panel.
- [x] Run full `python -m unittest discover -s tests -v`, compile all source, browser build, and browser acceptance; record exact counts/exit codes and any blocked evidence.
- [x] Update use guide and operational boundaries; self-review all changes with requesting-code-review guidance, no unrequested subagent/commit/push.
- [x] Confirm live data/config/templates/photos were not modified; hand off tested source-web result, not a new portable/desktop release.
