# Tradewind 最小 LangGraph 开发信工作流设计

## 目标

只重构 Tradewind 的单封开发信/WhatsApp 文案生成主链路，用 LangGraph 明确表达状态、条件路由、有限重试、SQLite checkpoint、人工审批和幂等持久化。现有 FastAPI 路由、React 页面布局、流式预览、取消机制与外联发送审核保持兼容。

这次重构的价值不是增加一个框架名，而是形成可以离线测试和在程序重启后恢复的工作流证据。

## 已选方案

采用“有价值的最小重构”，不选择以下两个极端：

- 不只把五个函数机械包装成节点，因为那不能证明暂停恢复、审批或幂等性。
- 不迁移批量生成、爬虫、OCR、照片识别或其他后台任务，因为它们与本次工作流证据无关且会显著扩大风险。

当前产品已经有外联队列，因此不新增一套重复的审批界面。单封生成在草稿完成后由 LangGraph `interrupt()` 暂停；用户点击现有“加入外联队列”即表示批准草稿，并使用同一 `thread_id` 通过 `Command(resume=...)` 恢复工作流。邮件或 WhatsApp 真正发送前仍保留外联队列已有的逐条确认。

## 范围

### 本次包含

- 单封生成入口 `/api/email/start` 使用 LangGraph。
- 节点：`retrieve`、`generate`、`rule_check`、`judge`、`human_approval`、`persist`。
- 规则失败时最多额外生成一次，总生成次数最多两次。
- SQLite checkpoint，使用稳定的 `task_id` 作为 LangGraph `thread_id`。
- 等待审批时关闭并重启程序，仍能恢复草稿。
- 现有“加入外联队列”携带可选 `workflow_id`，恢复对应工作流。
- 历史记录与外联任务都用 `workflow_id` 做幂等控制。
- 离线节点路由、暂停恢复、重复恢复和 API 兼容性测试。
- README、OPS-NOTES、依赖清单和 PyInstaller 收集配置同步更新。

### 本次不包含

- 批量生成迁移；`/api/email/batch` 继续走当前实现。
- 同步兼容入口 `/api/email` 迁移；继续走当前实现。
- 爬虫、OCR、照片识别、SMTP 发送状态机迁移。
- LangSmith、云端 tracing、AgentExecutor、ReAct、工具自主选择或多 Agent。
- 新增人工审批页面、聊天界面、定时任务或自动群发。
- 本轮构建 EXE/ZIP 或执行干净机器验收。

## 工作流

```text
START
  ↓
retrieve
  ↓
generate
  ↓
rule_check
  ├─ 有问题且 attempt < 2 ─→ generate
  └─ 通过或达到上限 ──────→ judge
                                  ↓
                         是否关联客户名单
                         ├─ 否 → persist → END
                         └─ 是 → human_approval (interrupt)
                                      ├─ reject → END
                                      └─ approve → persist → END
```

`rule_check` 只根据现有确定性规则决定是否重试，不让 LLM 自己决定循环。第二次生成会收到第一次规则问题和旧草稿，要求做定向修正。达到上限后即使仍有提示也继续展示，避免无限循环和不可控 token 消耗。

`judge` 保持可选。未启用时写入 `judge_status="disabled"`；解析失败或模型调用失败时写入 `unavailable`，但不阻断草稿展示和审批。

只有从客户名单选中的客户拥有 `customer_id`，才进入外联审批暂停。手工输入但未关联客户名单的临时草稿无法加入外联队列，因此沿用旧行为直接持久化，避免产生永远无法恢复的 checkpoint。

## 状态与运行时上下文

新增 `EmailWorkflowState`，仅保存可序列化数据：

- 标识：`workflow_id`、`customer_id`。
- 输入：客户、国家、产品、补充背景、语言、形态、模板快照、是否启用 Judge。
- 检索结果：产品记录、模板记录和渲染后的上下文。
- 生成状态：`draft`、`attempt`、`max_attempts`、`tokens`、`issues`。
- 评测状态：`scores`、`judge_status`。
- 审批状态：`approval_status`、`outreach_job_id`。
- 输出：现有 `EmailResult` 字段及新增的可选 `workflow_id`、`customer_id`。
- 副作用标记：`history_persisted`。

模型对象、数据库连接、回调函数和异常对象不能进入 checkpoint。`cancel_check`、流式回调、模型构建器和时钟通过 LangGraph `context_schema` 作为单次运行依赖注入。这样离线测试可以使用假模型和内存 checkpointer，SQLite 中只保存基础类型、列表和字典。

## 组件边界

### `app/email_agent.py`

保留现有规则、提示词和同步 `generate_email()` 兼容入口，并把节点需要的纯步骤提取为可单测函数。批量生成继续调用兼容入口，不改变结果结构。

### `app/email_workflow.py`

新增工作流模块，负责：

- `EmailWorkflowState` 与运行时上下文类型。
- 节点和条件边。
- 编译 `StateGraph`。
- `start()`、`resume_approval()`、`get_task_projection()` 三个服务接口。
- 将 checkpoint 状态投影回现有 `TaskState`，避免前端理解 LangGraph 内部结构。

模块不直接拥有 HTTP 逻辑，也不负责外联地址校验或 SMTP 发送。

### `app/memory.py`

为 `emails` 表增量添加 `workflow_id TEXT DEFAULT ''`，并建立只对非空值生效的唯一索引。新增 `save_email_once(workflow_id, ...)`：第一次写入返回新记录，重复恢复返回已有记录，不重复插入。

旧记录保持空 `workflow_id`，无需数据搬迁。

### `app/outreach_store.py`

为 `outreach_jobs` 增量添加 `workflow_id TEXT DEFAULT ''` 和非空唯一索引。`create_job()` 接受可选 `workflow_id`；同一 workflow 的重复请求返回既有任务，而不是创建第二条。现有按客户和渠道限制活动任务的规则继续生效。

### `server.py`

- `/api/email/start` 返回值仍为 `{task_id}`。
- `/api/email/tasks/{task_id}` 优先读取当前进程的流式状态；内存中不存在时，从 checkpoint 生成兼容的任务响应。
- `/api/email/tasks/{task_id}/cancel` 保持当前运行中取消语义。
- `/api/outreach/jobs` 的请求只增加可选 `workflow_id`，旧客户端不传时行为不变。
- 创建外联任务成功后，用同一 workflow ID 恢复 LangGraph 审批节点并执行幂等 `persist`。
- 如果恢复失败，外联任务不会重复创建；用户重试时读取同一任务并再次尝试幂等恢复。

### React

页面布局和主要按钮不变：

- `EmailResult` 增加可选 `workflow_id` 与 `customer_id`。
- 单封生成请求增加可选 `customer_id`。
- “加入外联队列”把 `workflow_id` 原样传给现有接口。
- 任务 ID 保存到浏览器本地存储，使页面或后端重启后还能查询同一个 checkpoint；任务不存在或用户开始新任务时清理旧引用。
- 从恢复结果中还原 `selectedCustomerId`，保证重启后仍显示“加入外联队列”。

## SQLite checkpoint

使用独立的 `tradewind_workflows.db`，放在现有运行数据目录中，不与源码、Git、诊断包或发布模板一起分发。独立数据库避免 LangGraph checkpoint 表与历史/外联事务互相争用。

生产源码使用同步 `SqliteSaver`，连接设置 `check_same_thread=False`；当前后端是同步节点加后台线程，符合本地轻量使用场景。测试默认使用 `InMemorySaver`，重启恢复测试使用临时 SQLite 文件。

依赖明确声明：

- `langgraph>=1.0,<2`
- `langgraph-checkpoint-sqlite>=3,<4`

checkpoint 只允许项目定义的基础状态类型，不保存任意 Python 对象。按照 LangGraph SQLite 安全建议启用严格 msgpack 反序列化配置。完整客户内容只保存在用户本机运行数据目录；诊断导出继续只给计数，不读取 checkpoint 正文。

## 人工审批与副作用顺序

审批节点的 interrupt payload 只包含 JSON 可序列化摘要，不包含 API Key、SMTP 凭据或完整运行时对象。

节点恢复时会从审批节点开头重新执行，因此 `interrupt()` 之前不得发生数据库写入。副作用只放在 `persist` 节点，并使用 `workflow_id` 唯一约束保证重复恢复安全。

`/api/outreach/jobs` 的处理顺序为：

1. 校验客户、渠道、联系方式、禁止联系名单和内容。
2. 以 `workflow_id` 幂等创建或取得外联任务。
3. 使用 `Command(resume={"action": "approve", "outreach_job_id": ...})` 恢复图。
4. `persist` 调用 `save_email_once()`。
5. 返回既有的 `{ok, item}` 响应。

如果第 3～4 步失败，重复同一请求不会新增外联任务；恢复再次执行时也不会新增历史记录。

## 流式输出、取消与错误处理

- `generate` 节点继续使用当前逐 chunk 回调，因此 React 的打字机预览不变。
- 进入第二次生成前清空当前任务的流式缓冲，避免两版草稿拼接。
- 每个 chunk 后继续检查取消标记；取消抛出 `GenerationCancelled`，任务响应仍为 `cancelled`，且不会进入审批或持久化。
- LLM 调用异常映射为现有 `error` 任务状态，不把原始密钥或请求头写进 checkpoint。
- 进程在等待审批时重启，`GET /api/email/tasks/{id}` 从 checkpoint 返回 `done` 和草稿，用户仍可加入队列。
- 本次不承诺在模型流式生成的任意 chunk 中间无损续写；如果进程在 `generate` 节点内退出，只能从最近完成节点重新执行生成。有限重试和幂等持久化保证不会无限循环或重复写库。

## 测试设计

所有新增行为按测试先行实现，不调用真实模型或外部网络。

### 工作流单元/集成测试

- `retrieve → generate → rule_check → judge` 正常路径。
- 首次规则失败时只重试一次，第二次后无论结果都离开循环。
- Judge 关闭、成功、格式错误和异常四种状态。
- 有 `customer_id` 时在 `human_approval` 暂停，暂停前历史表为空。
- 无 `customer_id` 时兼容旧路径直接持久化。
- 使用相同 SQLite 文件重新创建工作流服务后可读取同一草稿。
- `approve` 后写入一次；同一 `Command(resume=...)` 或 HTTP 请求重复执行仍只有一条历史和一条外联任务。
- `reject` 后结束且不写历史。
- 取消发生在生成阶段时不进入审批和持久化。

### API 回归测试

- `/api/email/start` 的必需输入和返回结构保持兼容。
- 当前进程内轮询仍返回流式文本。
- 内存任务丢失后，任务查询可从 checkpoint 恢复草稿。
- 不带 `workflow_id` 的旧式外联创建请求保持原行为。
- 带 `workflow_id` 的队列创建会恢复审批，并对重复请求幂等。
- 批量生成、同步入口、爬虫、OCR、照片和外联发送现有测试全部继续通过。

### 验收命令

```powershell
python -m pytest tests/test_email_workflow.py tests/test_server_email_workflow.py -q
python -m pytest -q
cd frontend
npm run typecheck
```

本轮不以 EXE/ZIP 构建作为完成条件。

## 文档与面试证据

README 用一张简短流程图说明节点、条件路由、interrupt 和恢复。OPS-NOTES 记录以下可复现证据：

- 一次规则失败后重试的测试。
- 一次重启后恢复待审批草稿的测试。
- 一次重复审批仍只写入一条记录的测试。
- 为什么没有把爬虫/OCR 等确定性任务强行迁入 LangGraph。

面试表述聚焦业务保证：LangGraph 管理开发信草稿的状态与恢复，SQLite checkpoint 支持跨进程 HITL，唯一键保证重复恢复不会重复产生副作用；框架本身不是成果。

## 官方接口依据

- LangGraph `interrupt()` 会将状态保存到 checkpointer，并要求用相同 `thread_id` 和 `Command(resume=...)` 恢复。
- interrupt 所在节点恢复时会从节点开头重新执行，所以其之前的副作用必须幂等或后移。
- `SqliteSaver` 适合本地轻量同步工作流；SQLite checkpointer 需要单独安装 `langgraph-checkpoint-sqlite`。
- `context_schema` 用于注入不应进入持久状态的运行期依赖。

参考：

- https://docs.langchain.com/oss/python/langgraph/interrupts
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://reference.langchain.com/python/langgraph/checkpoint/sqlite/SqliteSaver
- https://reference.langchain.com/python/langgraph/graph/state/StateGraph
