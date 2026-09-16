# Tradewind LangGraph Generation Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Tradewind 单封开发信/WhatsApp 文案生成改造成可重试、可暂停审批、可跨进程恢复且副作用幂等的最小 LangGraph 工作流。

**Architecture:** 新增独立 `EmailWorkflow` 服务，以 `task_id` 作为 LangGraph `thread_id`，用 SQLite checkpointer 保存只包含基础类型的状态。单封生成经过检索、生成、规则检查、最多一次重试和可选 Judge 后，在现有“加入外联队列”动作处恢复审批并幂等保存；同步入口与批量生成继续走现有 `generate_email()`。

**Tech Stack:** Python 3.13、LangGraph 1.x、langgraph-checkpoint-sqlite 3.x、LangChain OpenAI、FastAPI、SQLite、React 18、TypeScript、unittest/pytest。

**Spec:** `docs/superpowers/specs/2026-09-16-langgraph-generation-workflow-design.md`

## Global Constraints

- 只迁移 `/api/email/start` 单封后台任务；`/api/email` 与 `/api/email/batch` 保持原实现。
- 规则失败最多额外生成一次，`max_attempts` 固定为 `2`。
- 已有关联 `customer_id` 的草稿必须在 checkpoint 中等待“加入外联队列”；未关联客户名单的临时草稿沿用立即保存行为。
- 不改变现有页面布局和主要按钮，不新增重复审批页面。
- `task_id` 同时是 `workflow_id` 与 LangGraph `thread_id`。
- checkpoint 不保存模型对象、回调、数据库连接、异常对象、API Key 或 SMTP 凭据。
- 不使用真实模型或真实网络完成自动化测试。
- 不迁移爬虫、OCR、照片识别、SMTP 发送状态机或批量生成。
- 本计划不构建 EXE/ZIP；只更新 PyInstaller 收集配置并做源码验证。
- 所有数据库变更必须兼容已有 SQLite 文件，旧数据不可丢失。

---

### Task 1: Add LangGraph runtime dependencies and packaging discovery

**Files:**
- Modify: `requirements.txt`
- Modify: `Tradewind.spec`

**Interfaces:**
- Produces: 可导入的 `langgraph` 与 `langgraph.checkpoint.sqlite.SqliteSaver`。
- Produces: PyInstaller 分析阶段可发现 `langgraph` 和 SQLite checkpointer 子模块。

- [ ] **Step 1: Record the dependency contract**

Add these exact direct dependencies after `langchain-openai`:

```text
langgraph>=1.0,<2
langgraph-checkpoint-sqlite>=3,<4
```

- [ ] **Step 2: Install only the new dependencies in the existing Python environment**

Run:

```powershell
python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ "langgraph>=1.0,<2" "langgraph-checkpoint-sqlite>=3,<4"
```

- [ ] **Step 3: Add packaging discovery**

Extend the existing hidden-import setup in `Tradewind.spec`:

```python
for package in ("langgraph", "langgraph.checkpoint.sqlite"):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass
```

Do not build the executable in this task.

- [ ] **Step 4: Verify imports and pinned major versions**

Run:

```powershell
python -c "import importlib.metadata as m; from langgraph.graph import StateGraph; from langgraph.checkpoint.sqlite import SqliteSaver; print(m.version('langgraph')); print(m.version('langgraph-checkpoint-sqlite'))"
```

Expected: both versions print; LangGraph is `1.x`, checkpoint-sqlite is `3.x`, exit code `0`.

- [ ] **Step 5: Commit**

```powershell
git add requirements.txt Tradewind.spec
git commit -m "build: add LangGraph workflow dependencies"
```

---

### Task 2: Make generation-history persistence idempotent

**Files:**
- Create: `tests/test_memory.py`
- Modify: `app/memory.py`

**Interfaces:**
- Produces: `save_email_once(workflow_id: str, customer: str, country: str, product: str, email: str, score: float = 0, language: str = "zh-hant", format_: str = "email") -> int`.
- Preserves: `save_email(customer, country, product, email, score=0, language="zh-hant", format_="email") -> None` for legacy sync and batch paths.

- [ ] **Step 1: Write the failing idempotency test**

Create `tests/test_memory.py` with a temporary database and this behavior:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import memory


class MemoryWorkflowTests(unittest.TestCase):
    def test_save_email_once_reuses_workflow_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            with patch.object(memory, "MEMORY_DB", db):
                first = memory.save_email_once(
                    "wf-001", "Demo Clinic", "HK", "Laser", "Draft", 4, "en", "email"
                )
                second = memory.save_email_once(
                    "wf-001", "Demo Clinic", "HK", "Laser", "Draft", 4, "en", "email"
                )
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE workflow_id='wf-001'"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(first, second)
        self.assertEqual(count, 1)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_memory.py -q
```

Expected: FAIL because `app.memory.save_email_once` does not exist.

- [ ] **Step 3: Add backward-compatible schema migration**

In `_connect()`:

```python
if "workflow_id" not in cols:
    conn.execute("ALTER TABLE emails ADD COLUMN workflow_id TEXT DEFAULT ''")
conn.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_emails_workflow "
    "ON emails(workflow_id) WHERE workflow_id <> ''"
)
conn.commit()
```

Implement `save_email_once()` as one transaction:

```python
def save_email_once(workflow_id: str, customer: str, country: str, product: str,
                    email: str, score: float = 0, language: str = "zh-hant",
                    format_: str = "email") -> int:
    workflow_id = workflow_id.strip()
    if not workflow_id:
        raise ValueError("workflow_id 不能为空")
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO emails "
            "(workflow_id, customer, country, product, email, score, language, format, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (workflow_id, customer, country, product, email[:2000], score,
             language, format_, time.time()),
        )
        row = conn.execute(
            "SELECT id FROM emails WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        conn.commit()
        return int(row[0])
    finally:
        conn.close()
```

- [ ] **Step 4: Add and run the legacy-schema migration test**

Add this migration test:

```python
def test_old_email_table_is_migrated_without_losing_rows(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE emails (id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT NOT NULL, "
            "country TEXT DEFAULT '', product TEXT DEFAULT '', email TEXT NOT NULL, score REAL DEFAULT 0, "
            "language TEXT DEFAULT 'zh-hant', format TEXT DEFAULT 'email', created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO emails (customer, email, created_at) VALUES ('Old', 'Old draft', 1)"
        )
        conn.commit()
        conn.close()
        with patch.object(memory, "MEMORY_DB", db):
            memory.save_email_once("wf-new", "New", "HK", "Laser", "New draft")
        conn = sqlite3.connect(db)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(emails)")}
        rows = conn.execute("SELECT customer FROM emails ORDER BY id").fetchall()
        conn.close()
    self.assertIn("workflow_id", columns)
    self.assertEqual(rows, [("Old",), ("New",)])
```

Run:

```powershell
python -m pytest tests/test_memory.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Run memory consumers**

Run:

```powershell
python -m pytest tests/test_core.py tests/test_history.py -q
```

Expected: PASS with the legacy `save_email()` behavior unchanged.

- [ ] **Step 6: Commit**

```powershell
git add app/memory.py tests/test_memory.py
git commit -m "feat: make generation history idempotent"
```

---

### Task 3: Make outreach creation idempotent by workflow ID

**Files:**
- Modify: `app/outreach_store.py`
- Modify: `tests/test_outreach_store.py`

**Interfaces:**
- Consumes: stable workflow ID from Task 5.
- Changes: `OutreachStore.create_job(*, customer_id: str, customer_name: str, channel: str, provider: str, sender_account: str, recipient: str, subject: str, body: str, workflow_id: str = "") -> dict`.
- Produces: `OutreachStore.get_job_by_workflow_id(workflow_id: str) -> dict | None`.

- [ ] **Step 1: Write the failing same-workflow test**

Add to `OutreachStoreTests`:

```python
def test_same_workflow_returns_one_job_but_changed_payload_conflicts(self) -> None:
    payload = {
        "customer_id": "c-001",
        "customer_name": "Demo Clinic",
        "channel": "email",
        "provider": "gmail_smtp",
        "sender_account": "sender@example.test",
        "recipient": "buyer@example.test",
        "subject": "Demo",
        "body": "Body",
        "workflow_id": "wf-001",
    }
    first = self.store.create_job(**payload)
    second = self.store.create_job(**payload)
    self.assertEqual(first["id"], second["id"])
    with self.assertRaises(OutreachConflict):
        self.store.create_job(**{**payload, "body": "Changed"})
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_outreach_store.py::OutreachStoreTests::test_same_workflow_returns_one_job_but_changed_payload_conflicts -q
```

Expected: FAIL because `create_job()` does not accept `workflow_id`.

- [ ] **Step 3: Add migration and lookup**

After the existing schema script, inspect `PRAGMA table_info(outreach_jobs)`, add `workflow_id TEXT DEFAULT ''` when missing, and create:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_workflow
ON outreach_jobs(workflow_id) WHERE workflow_id <> '';
```

Implement lookup:

```python
def get_job_by_workflow_id(self, workflow_id: str) -> dict | None:
    workflow_id = workflow_id.strip()
    if not workflow_id:
        return None
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT * FROM outreach_jobs WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
    finally:
        conn.close()
    return self._job(row) if row else None
```

Before inserting, return the existing job only when customer, channel, recipient, subject and content hash all match. Raise `OutreachConflict("工作流内容与既有外联任务不一致")` on any mismatch.

- [ ] **Step 4: Add old-database migration coverage**

Add a test that creates the pre-migration `outreach_jobs` and `contact_suppressions` tables using the exact SQL currently present in `_connect()` but without `workflow_id`, inserts one pending row, reopens `OutreachStore`, then asserts:

```python
conn = sqlite3.connect(self.db)
columns = {row[1] for row in conn.execute("PRAGMA table_info(outreach_jobs)")}
count = conn.execute("SELECT COUNT(*) FROM outreach_jobs").fetchone()[0]
conn.close()
self.assertIn("workflow_id", columns)
self.assertEqual(count, 1)
```

- [ ] **Step 5: Run the outreach store suite**

Run:

```powershell
python -m pytest tests/test_outreach_store.py -q
```

Expected: PASS, including the existing active customer/channel uniqueness tests.

- [ ] **Step 6: Commit**

```powershell
git add app/outreach_store.py tests/test_outreach_store.py
git commit -m "feat: make outreach creation workflow-idempotent"
```

---

### Task 4: Extract reusable generation steps without changing legacy behavior

**Files:**
- Modify: `app/email_agent.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- Produces: `retrieve_generation_context(customer: str, country: str, product: str, extra: str, language: str, format_: str, template_record: dict | None = None, company_profile: dict[str, str] | None = None) -> dict`.
- Produces: `generate_draft(context: dict, *, retry_issues: list[str], previous_draft: str, cancel_check=None, stream_callback=None) -> tuple[str, int]`.
- Produces: `check_generation_draft(draft: str, context: dict) -> list[str]`.
- Produces: `judge_generation_draft(draft: str, enabled: bool, cancel_check=None) -> tuple[dict | None, str]`.
- Preserves: `generate_email(customer, country, product, judge=False, verbose=False, extra="", cancel_check=None, stream_callback=None, language="zh-hant", format_="email", template_record=None, company_profile=None) -> dict` output and immediate history save for sync/batch callers.

- [ ] **Step 1: Add characterization tests for the extracted contract**

Extend `tests/test_core.py` to assert:

```python
def test_retry_prompt_contains_previous_issues_and_draft(self) -> None:
    context = {
        "customer": "Glow",
        "country": "HK",
        "product": "Laser",
        "extra": "",
        "language": "en",
        "format": "email",
        "products": [{"title": "Laser", "content": "Diode-808"}],
        "emails": [],
        "company_profile": {"sender_name": "Demo", "company_name": "DemoMed"},
        "templates_used": [],
    }
    fake_llm = unittest.mock.Mock()
    with patch("app.email_agent.build_llm", return_value=fake_llm), \
         patch("app.email_agent._stream_email", return_value=("Fixed", 5)) as stream:
        draft, tokens = generate_draft(
            context,
            retry_issues=["缺少明确行动号召"],
            previous_draft="Old draft",
        )
    prompt = stream.call_args.args[1]
    self.assertIn("缺少明确行动号召", prompt)
    self.assertIn("Old draft", prompt)
    self.assertEqual((draft, tokens), ("Fixed", 5))
```

Also add a test that `judge_generation_draft("Draft", enabled=False)` returns `(None, "disabled")` without building an LLM.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m pytest tests/test_core.py -q
```

Expected: FAIL because the four helper functions are missing.

- [ ] **Step 3: Extract the four helpers**

Move the existing retrieve, prompt construction, signature, rule check and Judge blocks into the named helpers. For retry prompts append this exact bounded repair section:

```text
【上一版修正要求】
以下是确定性规则检查发现的问题：{issues}
请只修正这些问题，保留正确事实，不要虚构新数据。

【上一版草稿】
{previous_draft}
```

Keep `_stream_email()` unchanged so chunk cancellation and token extraction remain compatible.

- [ ] **Step 4: Delegate legacy `generate_email()` to the helpers**

Keep its public signature and final result keys unchanged. It must call the helpers once with empty retry inputs, then call existing `save_email()` exactly once.

- [ ] **Step 5: Verify focused and full legacy tests**

Run:

```powershell
python -m pytest tests/test_core.py tests/test_quality_eval.py -q
```

Expected: PASS; existing template injection, signatures, Judge parsing and offline eval behavior remain unchanged.

- [ ] **Step 6: Commit**

```powershell
git add app/email_agent.py tests/test_core.py
git commit -m "refactor: extract reusable email generation steps"
```

---

### Task 5: Implement the checkpointed LangGraph workflow

**Files:**
- Create: `app/email_workflow.py`
- Create: `tests/test_email_workflow.py`

**Interfaces:**
- Consumes: Task 2 `save_email_once()` and Task 4 generation helpers.
- Produces: `EmailWorkflow.start(workflow_id: str, inputs: dict, context: WorkflowContext) -> dict`.
- Produces: `EmailWorkflow.resume_approval(workflow_id: str, action: str, outreach_job_id: int = 0) -> dict`.
- Produces: `EmailWorkflow.get_task_projection(workflow_id: str) -> dict`.
- Produces: `build_sqlite_workflow(path: Path) -> EmailWorkflow`.

- [ ] **Step 1: Write the failing routing and retry tests**

Create a fake step adapter that records node calls and returns rule issues on the first attempt only. Assert this call sequence:

```python
self.assertEqual(
    steps.calls,
    ["retrieve", "generate:1", "check:1", "generate:2", "check:2", "judge"],
)
```

Use `InMemorySaver`. Start with no `customer_id` so the graph reaches `persist` and `END`. Assert `attempt == 2` and the persist adapter ran once.

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_email_workflow.py -q
```

Expected: FAIL because `app.email_workflow` does not exist.

- [ ] **Step 3: Define serializable state and runtime context**

Use `TypedDict` for checkpointed state and a dataclass for invocation-only context:

```python
class EmailWorkflowState(TypedDict, total=False):
    workflow_id: str
    customer_id: str
    customer: str
    country: str
    product: str
    extra: str
    judge: bool
    language: str
    format: str
    template_record: dict | None
    company_profile: dict
    generation_context: dict
    draft: str
    previous_draft: str
    issues: list[str]
    scores: dict | None
    judge_status: str
    attempt: int
    max_attempts: int
    tokens: int
    started_at: float
    approval_status: str
    outreach_job_id: int
    history_id: int
    result: dict


@dataclass
class WorkflowContext:
    cancel_check: Callable[[], bool] | None = None
    stream_callback: Callable[[str], None] | None = None
    reset_stream_callback: Callable[[], None] | None = None
    now: Callable[[], float] = time.monotonic
```

Keep step dependencies in an injectable `EmailWorkflowSteps` adapter; the production adapter calls Task 4 helpers and `save_email_once()`.

- [ ] **Step 4: Build nodes and bounded conditional edges**

The route after `rule_check` must be exactly:

```python
def after_rule_check(state: EmailWorkflowState) -> str:
    if state.get("issues") and state.get("attempt", 0) < state.get("max_attempts", 2):
        return "generate"
    return "judge"
```

The approval node must call `interrupt()` before any side effect:

```python
decision = interrupt({
    "workflow_id": state["workflow_id"],
    "customer": state["customer"],
    "issues": state.get("issues", []),
})
action = str(decision.get("action", "reject")) if isinstance(decision, dict) else "reject"
return {
    "approval_status": "approved" if action == "approve" else "rejected",
    "outreach_job_id": int(decision.get("outreach_job_id", 0)) if isinstance(decision, dict) else 0,
}
```

Route rejected approvals to `END`; route approved approvals to `persist`.

The `judge` node must also assemble `state["result"]` before the interrupt. The result uses the current public keys plus stable identity:

```python
result = {
    "workflow_id": state["workflow_id"],
    "customer_id": state.get("customer_id", ""),
    "customer": state["customer"],
    "country": state["country"],
    "product": state["product"],
    "email": state["draft"],
    "issues": state.get("issues", []),
    "scores": state.get("scores"),
    "judge_status": state.get("judge_status", "disabled"),
    "tokens": state.get("tokens", 0),
    "time_s": 0.0,
    "language": state["language"],
    "format": state["format"],
    "templates_used": state["generation_context"].get("templates_used", []),
}
```

Initialize `started_at` from the injected clock in `start()`, and use the same clock in `judge` to replace `time_s=0.0` with elapsed time while tests keep it deterministic.

- [ ] **Step 5: Add interrupt-before-persist and restart tests**

Test that a state with `customer_id="c-001"`:

- returns an interrupt;
- has a visible draft in `get_task_projection()`;
- has not called `persist`;
- can be reopened by a new `EmailWorkflow` instance using the same temporary SQLite file;
- resumes with `Command(resume={"action": "approve", "outreach_job_id": 7})` and persists once.

Use `SqliteSaver(sqlite3.connect(path, check_same_thread=False))` for this test.

- [ ] **Step 6: Add duplicate approval and rejection tests**

Assert two resume attempts leave one history record through the real `save_email_once()` implementation. `resume_approval()` must inspect the latest snapshot first: if the workflow is already approved and has no pending node, return the existing projection without invoking `Command` again. Assert `reject` reaches `END` without calling persist.

- [ ] **Step 7: Implement task projection**

Return the existing frontend-compatible shape:

```python
{
    "status": "done",
    "total": 1,
    "done": 1,
    "current": state.get("customer", ""),
    "results": [],
    "result": state.get("result"),
    "error": "",
    "stream": state.get("draft", ""),
}
```

Return `{"status": "not_found"}` when `graph.get_state(config).values` is empty. Include `workflow_id` and `customer_id` inside `result`.

In `build_sqlite_workflow()`, set `LANGGRAPH_STRICT_MSGPACK=true` before constructing `SqliteSaver`, create the parent directory, open SQLite with `check_same_thread=False`, and keep the connection owned by the workflow service for its full lifetime.

- [ ] **Step 8: Run workflow tests**

Run:

```powershell
python -m pytest tests/test_email_workflow.py -q
```

Expected: routing, retry limit, interrupt, restart, rejection and idempotency tests all pass without network access.

- [ ] **Step 9: Commit**

```powershell
git add app/email_workflow.py tests/test_email_workflow.py
git commit -m "feat: add checkpointed LangGraph email workflow"
```

---

### Task 6: Integrate the workflow with FastAPI and the outreach approval action

**Files:**
- Modify: `server.py`
- Create: `tests/test_server_email_workflow.py`
- Modify: `tests/test_server_outreach.py`

**Interfaces:**
- Consumes: Task 5 `EmailWorkflow` service.
- Changes: `EmailRequest.customer_id: str = ""`.
- Changes: `OutreachCreateRequest.workflow_id: str = ""`.
- Preserves: `/api/email/start`, `/api/email/tasks/{id}`, `/api/email/tasks/{id}/cancel`, `/api/outreach/jobs` routes and existing required fields.

- [ ] **Step 1: Write a failing API compatibility test**

Use `TestClient` and a fake workflow service. Assert:

```python
response = client.post("/api/email/start", json={
    "customer_id": "c-001",
    "customer": "Demo Clinic",
    "country": "HK",
    "product": "Laser",
    "language": "en",
    "format": "email",
})
self.assertEqual(response.status_code, 200)
self.assertRegex(response.json()["task_id"], r"^[0-9a-f]{12}$")
```

The fake workflow must record that the same task ID was passed as `workflow_id` and that `customer_id` reached workflow inputs.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest tests/test_server_email_workflow.py -q
```

Expected: FAIL because the server still calls `generate_email()` directly for `/api/email/start`.

- [ ] **Step 3: Add lazy workflow construction**

Add a module-level cache and factory so importing `server.py` does not open a checkpoint database:

```python
_EMAIL_WORKFLOW = None


def _email_workflow():
    global _EMAIL_WORKFLOW
    if _EMAIL_WORKFLOW is None:
        _EMAIL_WORKFLOW = build_sqlite_workflow(DATA_DIR / "tradewind_workflows.db")
    return _EMAIL_WORKFLOW
```

Tests patch `_EMAIL_WORKFLOW` with a fake or temporary workflow.

If `customer_id` is supplied to `/api/email/start`, resolve it with `_find_customer_by_id()` and reject the request when the customer does not exist or its stored name differs from `req.customer`. This prevents a caller from approving a draft against a different customer record.

- [ ] **Step 4: Route only `/api/email/start` through LangGraph**

Keep `_new_task(1)` and the background thread. Inside `execute()` call `workflow.start()` with callbacks:

```python
WorkflowContext(
    cancel_check=lambda: _cancelled(task_id),
    stream_callback=lambda chunk: _append_stream(task_id, chunk),
    reset_stream_callback=lambda: _reset_stream(task_id),
)
```

When `start()` reaches its interrupt, finish the in-memory task with `workflow.get_task_projection(task_id)`. Preserve `GenerationCancelled` and current exception mapping.

- [ ] **Step 5: Add durable task-query fallback**

Change `api_task()` so an absent in-memory task calls `workflow.get_task_projection(task_id)`. Add a test that clears `server.TASKS` after generation and still receives `status="done"`, the draft, `workflow_id` and `customer_id`.

- [ ] **Step 6: Add workflow-aware outreach approval tests**

Extend `tests/test_server_outreach.py` with two tests:

1. A request with `workflow_id="wf-001"` creates one outreach job and calls `resume_approval("wf-001", "approve", job_id)`.
2. Repeating the same request returns the same job ID and leaves one history row and one outreach row.

Also keep one existing request without `workflow_id` to prove old behavior is unchanged.

- [ ] **Step 7: Resume approval after idempotent job creation**

Pass `workflow_id` into `OutreachStore.create_job()`. After obtaining the job, call:

```python
workflow.resume_approval(
    req.workflow_id,
    action="approve",
    outreach_job_id=int(item["id"]),
)
```

If resume fails, return `ok=False`, the existing `item`, and the safe message `草稿已入队，但工作流保存失败，请重试` without raw exception text. A repeated request must reuse the same outreach row and retry only the idempotent resume.

- [ ] **Step 8: Run server workflow and outreach tests**

Run:

```powershell
python -m pytest tests/test_server_email_workflow.py tests/test_server_outreach.py -q
```

Expected: PASS, including restart fallback and duplicate approval cases.

- [ ] **Step 9: Commit**

```powershell
git add server.py tests/test_server_email_workflow.py tests/test_server_outreach.py
git commit -m "feat: connect LangGraph approval to outreach queue"
```

---

### Task 7: Preserve workflow identity across frontend reloads

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/tasks.ts`
- Modify: `frontend/src/tabs/ComposeTab.tsx`

**Interfaces:**
- Consumes: optional `workflow_id` and `customer_id` from Task 6.
- Produces: `api.startEmail()` sends optional `customer_id`.
- Produces: `api.outreach.create()` sends optional `workflow_id`.
- Preserves: existing UI layout and button text.

- [ ] **Step 1: Capture the current compiler baseline**

Run:

```powershell
cd frontend
npm run typecheck
```

Expected: PASS before changes. The project intentionally has no frontend unit-test runner, so this small compatibility adapter is verified by TypeScript, backend API tests and final browser acceptance rather than adding a new framework.

- [ ] **Step 2: Extend TypeScript contracts**

Add to `EmailResult`:

```typescript
workflow_id?: string;
customer_id?: string;
```

Extend request types:

```typescript
startEmail: (p: {
  customer_id?: string;
  customer: string;
  country?: string;
  product?: string;
  extra?: string;
  judge?: boolean;
  language?: Lang;
  format?: OutFormat;
  template_id?: string;
}) => post<{ task_id: string; error?: string }>("/api/email/start", p)
create: (payload: {
  customer_id: string;
  channel: OutreachChannel;
  content: string;
  language: Lang;
  workflow_id?: string;
}) => post<{ ok: boolean; item?: OutreachJob; duplicate?: boolean; error?: string }>(
  "/api/outreach/jobs",
  payload,
)
```

- [ ] **Step 3: Persist only the compose task reference**

In `frontend/src/tasks.ts`, use key prefix `tradewind.task.`. Only `kind === "compose"` writes to `localStorage`; `resumeTask()` reads from memory first and then local storage; `forgetTask()` removes both. Wrap storage calls in `try/catch` so private-browser restrictions do not break task polling.

Do not persist stream text, customer data, generated content or credentials in browser storage.

- [ ] **Step 4: Carry identity through Compose**

When starting generation, include:

```typescript
customer_id: selectedCustomerId || undefined,
```

When receiving a recovered result:

```typescript
if (t.result?.customer_id) setSelectedCustomerId(t.result.customer_id);
```

When adding to outreach:

```typescript
workflow_id: result.workflow_id,
```

Starting a new generation overwrites the stored compose task ID. A `not_found` response clears the stored reference and stops the busy state.

- [ ] **Step 5: Run frontend verification**

Run:

```powershell
npm run typecheck
```

Expected: PASS with no TypeScript errors.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/types.ts frontend/src/api.ts frontend/src/tasks.ts frontend/src/tabs/ComposeTab.tsx
git commit -m "feat: restore pending generation workflow in frontend"
```

---

### Task 8: Document, secure and verify the complete source release

**Files:**
- Modify: `README.md`
- Modify: `OPS-NOTES.md`
- Modify: `tests/test_diagnostics.py`
- Modify: `.secrets.baseline` only if detect-secrets reports a reviewed synthetic fixture

**Interfaces:**
- Documents: actual workflow, scope boundary, recovery behavior and reproducible evidence.
- Verifies: all backend, frontend and secret checks without building distributable artifacts.

- [ ] **Step 1: Update README with the actual workflow**

Add a compact diagram showing:

```text
retrieve → generate → rule_check ──retry once──┐
                         │                     │
                         └→ judge → interrupt → approve → persist
```

State explicitly that batch, crawler, OCR and photo tasks are not LangGraph workflows, and explain that this is a deliberate scope decision.

- [ ] **Step 2: Record operations evidence**

In `OPS-NOTES.md`, record:

- checkpoint database name `tradewind_workflows.db`;
- `task_id == workflow_id == thread_id`;
- maximum two generation attempts;
- approval maps to “加入外联队列”;
- checkpoint content never enters diagnostic exports;
- duplicate approval is protected by unique non-empty `workflow_id` indexes.

Extend `tests/test_diagnostics.py` with a checkpoint database containing the synthetic marker `checkpoint-private@example.test`; export diagnostics and assert neither that marker nor the checkpoint filename appears anywhere in the ZIP payload:

```python
workflow_db = data_dir / "tradewind_workflows.db"
workflow_db.write_text("checkpoint-private@example.test", encoding="utf-8")
response = server.export_diagnostics()
with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
    payload = b"".join(archive.read(name) for name in archive.namelist())
self.assertNotIn(b"checkpoint-private@example.test", payload)
self.assertNotIn(b"tradewind_workflows.db", payload)
```

- [ ] **Step 3: Run focused workflow tests**

Run:

```powershell
python -m pytest tests/test_memory.py tests/test_email_workflow.py tests/test_server_email_workflow.py tests/test_outreach_store.py tests/test_server_outreach.py -q
```

Expected: all focused tests pass.

- [ ] **Step 4: Run the full backend suite**

Run:

```powershell
python -m pytest -q
```

Expected: zero failures. Record the exact pass count in the final handoff; do not copy an older count.

- [ ] **Step 5: Run frontend type checking**

Run:

```powershell
cd frontend
npm run typecheck
```

Expected: exit code `0`.

- [ ] **Step 6: Run repository integrity and security checks**

From the repository root:

```powershell
git diff --check
python -m pre_commit run --all-files
```

Expected: detect-secrets, private-key detection, conflict detection, BOM and line-ending checks all pass. If the all-files line-ending hook touches unrelated legacy files, restore only those hook-created changes and run the checks against the staged change set, as documented in the repository history.

- [ ] **Step 7: Perform one local browser acceptance**

Use the model already configured in Tradewind Settings and verify this sequence:

1. Select a customer and start one draft.
2. Observe stream output and final result.
3. Stop and restart the backend while the draft is awaiting approval.
4. Reload `http://127.0.0.1:8101` and confirm the same draft returns.
5. Click “加入外联队列” once, reload the page so the recovered draft is shown again, click the same action again, and confirm only one outreach job and one history record exist.

Do not send a real email or WhatsApp message during this acceptance.

- [ ] **Step 8: Commit documentation and any final verified adjustment**

```powershell
git add README.md OPS-NOTES.md tests/test_diagnostics.py
git commit -m "docs: record LangGraph workflow guarantees"
```

- [ ] **Step 9: Capture final state without packaging**

Run:

```powershell
git status --short
git log -8 --oneline
```

Expected: no source changes remain. Do not run the EXE or ZIP build scripts in this plan.
