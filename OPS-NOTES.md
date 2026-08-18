# Tradewind OPS-NOTES

信风（Tradewind）外贸开发信 Agent 运维笔记。记录运行方式、踩坑、架构决策。
端口 **8101**（曾用 8100，冲突后迁移）。源码网页、浏览器绿色版与 Tauri 桌面端三种运行形态。

---

## 1. 运行与构建

```powershell
# 杀端口旧进程（run.py 无热重载，改后端必须重启）
Get-NetTCPConnection -LocalPort 8101 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }

# 启动后端
cd path\to\Tradewind
python run.py

# 构建前端（先做 TypeScript 检查，再由 Vite 构建）
cd frontend
npm run build:check
```

- `python -m py_compile server.py`：**成功时静默无输出**，只有语法错误才打印。别把"没输出"当异常。
- 根路径 404 `{"detail":"Not Found"}` 常见于旧进程占端口或前端尚未构建；先核对 8101 的监听 PID，再检查 `frontend/dist`。

## 2. Tauri 桌面端 + Python sidecar

```powershell
cd frontend

# 桌面开发（使用项目源码后端）
npm run desktop:dev

# 只构建桌面前端
npm run build:desktop

# 重建 Python sidecar + Tauri 发行产物
npm run desktop:build:full
```

- Tauri 2 负责窗口、单实例和 sidecar 生命周期；`desktop_backend.py` 只启动 FastAPI，不打开浏览器。
- Python sidecar 由 `TradewindBackend.spec` 构建，目标复制到 `frontend/src-tauri/binaries/tradewind-backend-x86_64-pc-windows-msvc.exe`。
- 桌面配置 `VITE_API_BASE_URL=http://127.0.0.1:8101`；图片 URL 同样必须经过 `apiUrl()`，否则 Tauri 自定义协议会读错地址。
- 桌面数据根为 `%LOCALAPPDATA%\com.tradewind.desktop\data`，通过 `TRADEWIND_DATA_DIR` 传给后端；日志在同级 `logs/backend.out.log` 与 `backend.err.log`。
- 源码版数据根仍为项目内 `data/`。两种运行形态不会自动合并数据，排查“配置不见了”时先看设置页显示的数据路径。
- `config.json`、客户资料、照片和生成历史不进包。产品/话术模板仅在目标数据文件不存在时初始化，不覆盖用户已有数据。
- 后端已经内置到 sidecar 后，普通 React/CSS 调整只需构建 Tauri 前端；后端代码或 Python 依赖变化才需要重建 sidecar。

### 2.1 桌面启动与关闭约定（2026-08-07）

- 主窗口先显示航线/小船启动画面，同时后台启动 sidecar；前端轮询 `/api/config`，最长等待 60 秒，不再因第一次请求失败误进首次配置页。
- 单实例监听 `127.0.0.1:48101`。再次双击会 `show + unminimize + focus` 已有窗口；旧实例退出清理期间最多等待 5 秒，避免第一次重开被吞。
- sidecar 健康检查固定为 `127.0.0.1:8101/api/health`。若 8101 已被旧 Stage 包占用，桌面壳可能连接到错误后端；排查时必须核对监听 PID 和可执行文件路径。
- 关闭主窗口会终止由当前桌面实例启动的后端进程树；桌面壳不会关闭用户手动启动的源码后端。
- 当前阶段按要求**不重建新 sidecar**；先以源码网页端验证后端功能，确认阶段稳定后再做一次完整发行构建。

## 3. 任务制模式（邮件 / 爬虫 / 文档识别统一）

三类耗时操作统一为同一模式：**POST 创建任务返回 task_id → 前端轮询 GET /api/*/tasks/{id} → POST cancel**。

- 邮件：`/api/email/start`、`/api/email/tasks/{id}`、cancel；流式用 `llm.stream()` + `cancel_check` 每 chunk 检查，取消不落记忆库，批量取消保留已完成。
- 爬虫：`/api/crawler/start` + cancel；源码版单机同一时间只允许一个爬虫任务，避免多个 Chromium 抢占内存和代理。进度通过线程隔离的 sink 写入任务日志，不再重定向进程级 stdout。
- 爬虫数量：页面里的“最多客户数”现在表示目标有效结果数；默认最多检查目标数 3 倍的候选，并按邮箱/电话筛选条件补足。检查点拆细（launch 15s / goto 12s / 滚动 500ms×4 / 详情 500ms×5）。
- 文档识别：`/api/{products,templates}/extract` + `/api/extract/tasks/{id}` + cancel；**OCR 页粒度检查**（每页渲染识别前查 cancel），LLM 总结只能调用前检查（invoke 无法打断）。
- 前端 `tasks.ts` 模块级轮询单例：切 Tab 不中断，`resumeTask` 恢复；泛型化（TaskState/CrawlerState/ExtractTask 各自 fetcher）。

## 4. 数据文件红线（重要事故）

**运行时数据文件（products.json / customers.json / emails.json）禁止整体 write 覆盖。**

事故记录（2026-08-04，公开版已匿名化）：清理示例设备时直接 write 覆盖
`products.json`，导致用户通过界面新增的两条记录丢失，只能从原始资料重新导入。
这些文件会被 UI 持续修改，后续改动必须先读取当前内容，再做增量修改或调用 API，
禁止用开发机上的固定文件整体覆盖。

另一教训：**演示数据与用户数据不能混用**。曾经因为过期演示设备仍留在运行库，
导致检索命中错误卖点，表面上像检索逻辑故障，实际是数据源污染。公开仓库只保留
明确标注的合成样例；运行数据位于本机目录，不作为内置样例回写仓库。

## 5. 架构决策：本地检索，不接 RAGNEXUS（2026-08-04）

曾接入 RAGNEXUS 语义检索（动态源选择：RAGNEXUS 结果相关才优先，否则本地兜底）。**已彻底摘除**，理由：

1. **分发形态冲突**：产品桌面应用分发给非技术用户，不能依赖外部 Docker 服务；发行版没有 RAGNEXUS 可用。
2. **数据量小**：产品/话术几十条，本地关键词打分（标题>标签>内容）已够，语义检索是负资产（易命中无关块，曾污染产品卖点）。
3. **数据隔离**：客户名单敏感，不该进共享知识库。
4. **多数据源事故前科**（RAGNEXUS 自身"718 清零"假象即数据多位置所致）。

Tradewind 仅保留本地关键词检索实现，不再包含 RAGNEXUS 配置或客户端代码。

面试叙事：砍掉远程向量库 → 讲清"什么时候不该接向量库"，是架构取舍的加分故事。

## 6. Windows 环境坑

- **PyMuPDF `pix.save(f.name)` Permission denied**：临时文件被 NamedTemporaryFile 句柄独占（fitz 保存前删旧文件被挡）。修复：**内存管道** `pix.tobytes("png")` → `cv2.imdecode(np.frombuffer(...))` → ndarray 喂 OCR，不落盘。
- **RapidOCR 引擎单例** `_OCR_ENGINE`：模型只加载一次（首次 5-10s），后续秒开；每请求重新加载会拖垮识别。
- **OCR 错误诊断**：`data/ocr_errors.log` 落盘 traceback（沙箱不能跑命令、用户不总贴 toast 原文，靠日志远程诊断）。
- **pydub ffmpeg RuntimeWarning**：markitdown 音频模块依赖检查，与文档/OCR 无关；server.py 顶部 `warnings.filterwarnings("ignore", message="Couldn't find ffmpeg.*")` 静默。
- **curl 被 Verge 代理劫持**：本机验证以浏览器为准。
- **Windows 杀软误报**：未签名测试包可能触发 SmartScreen；先核验构建来源和 SHA256，再决定是否放行。`--onedir` 通常比单文件打包误报少。

## 7. 前端约定

- 禁止外部 CDN（exe 离线运行）。
- 样式变量：`--paper` 暖纸底、`--amber` 强调、`--red` 危险、`--ink/--ink-2` 文字、`--line` 描边、`--ease` 缓动、`--r-sm` 圆角。
- 删除确认统一用 `ConfirmDialog`（替换原生 window.confirm）；危险按钮 `.icon-btn.danger` / `.btn-danger`。
- 读秒统一 `Elapsed` 组件（active 计时、停时归零），识别/生成 busy 卡必备。
- 主题化思路：CSS 变量只能改色，独特设计语言需元素级 DOM 覆盖（沿用 RAGNEXUS 结论）。

## 8. 敏感信息红线

- `data/config.json` 已 git rm --cached + .gitignore；`.env` 同样不入库；key 只存本机。
- 历史邮件入库前必须脱敏，真实客户姓名、邮箱、电话、地址和业务备注统一替换为占位符；客户名单不进 git。
- `data/products.json`、`data/emails.json` 只保留合成演示数据。运行后如出现改动，提交前必须逐条确认，不能把本地业务资料当成样例提交。
- 本地提交由 `detect-secrets` + pre-commit 拦截，GitHub Actions 使用 TruffleHog 扫描 push、PR 和完整历史。
- 推送前至少检查 `git status --short`，确认没有 `config.json`、`.env`、客户文件、日志、照片、诊断包和构建产物。

## 9. 产品/话术检索语义

- 生成链路：产品 → `_local_records(product, 3)`，空则 `_local_records("产品", 3)`（取全部设备 top 3）；话术 → `_local_records("邮件 开发信 模板", 2)`。全本地 JSON 库。
- 产品标签约定：`tags[0]` 固定「产品」→ 兜底检索全命中。
- 话术模板 Tab = 喂样本入口：历史邮件（脱敏）贴进来，生成时参考语气与结构。
- SQLite 生成历史只是 history/evaluation log，不自动参与下一轮生成。历史页仅允许用户确认脱敏后手动晋升到话术模板，防止低质量输出或客户隐私无审查回灌 Prompt。

## 10. 爬虫视觉识别（2026-08-04，后续扩展见 §12）

照片识别链路：`maps_hunter` 收集照片（滚动触发懒加载）→ `vision_analyzer.analyze_photo`（下载 → 视觉模型 → JSON 解析）→ `equipment.gap_analysis` 缺品分析（"缺什么推什么"）。

- **服务商路由**：`config.py` 的 `VISION_PROVIDERS` 管理模型建议、API 地址与代理需求；最初支持 OpenAI/智谱，当前已扩展火山豆包，详见 §12。
- 同一家店最多 4 张照片合并为一次视觉请求；按图片集合+模型+提示词版本缓存，仅缓存成功结果。图片 MIME 按文件签名识别，低置信度和重复设备会在进入缺品分析前过滤。
- Webs 香港模式以 `.hk`、页面香港地址或 `+852` 综合判断，不再漏掉使用 `.com` 的香港公司；静态页面没有联系方式时，受控渲染主页/联系页作为兜底。
- **key 回退链**：openai → vision_api_key → .env → providers.openai.api_key；glm → glm_api_key → .env。
- **图片下载永远走代理，API 调用按服务商**：lh3.googleusercontent.com 国内不通；智谱 API 国内直连。两者不能一刀切（曾 bug：切智谱时把下载代理也关了 → 下载全超时）。
- **图片下载用浏览器网络栈**：`page.request.get()`（Playwright）优先，httpx 兜底。独立 httpx 走代理下 lh3 图频繁 ssl handshake 超时（时好时坏），浏览器同通道稳定得多。
- **限流 429 重试**：智谱免费档人多（1305 访问量过大），sleep 3s 重试一次再放弃。
- **页面刷新重试**：搜索页 30s 无列表且 body 空白 → reload 一次；详情页 12s 还在 Loading → reload 再等 8s。区分"慢加载"与"真风控"，两者都是 Maps 常态。
- **成本**：智谱 Flash 免费；4.6V 一张图 ~1047 token ≈ 0.001 元。OpenAI Luna 一张 ~$0.0007。
- **产品品类画像只用 title+tags**：content 的适应症描述（脱毛/嫩肤）是使用场景不是设备类型，混入会污染品类对比（K8 被误归"激光脱毛"的真实 bug）。`CATEGORY_ALIASES` 统一别名（光治疗→IPL）。
- **OpenAI 无余额 ≠ key 无效**：429 "no credits" 是欠费；ChatGPT Plus 订阅与 API 计费完全独立，Plus 的钱在 API 里一分没有。
- **Google 软风控实况**：同 IP 高频跑同关键词 → 搜索页空白（页面文本空）；换关键词可绕过（词条级标记）；换节点解 IP 级。详情页接口比搜索页先疲劳。冷却 20-30 分钟或次日恢复。

## 11. 爬虫单机优化（2026-08-05）

- 保留 FastAPI + Playwright + httpx，不引入 Scrapy、Redis 或 Celery；当前是本地单用户、每次几十家的规模。
- 同一官网的首页、联系页和服务页复用一个 `httpx.Client` 连接池；仅对 408/429/5xx 与网络异常轻量重试，拒绝非 HTML/XML/纯文本响应和超过 2 MiB 的页面。
- Maps 的产品目录在每次任务开始时读取一次，不再每处理一家店重复读取 `products.json`。
- 离线测试覆盖临时错误重试、响应过滤、邮箱/电话/WhatsApp 解析、Bing 跳转还原、联系方式目标筛选和任务日志隔离。

## 12. 照片复核与视觉服务商（2026-08-06）

- Maps 照片可选落盘到 `data/crawler_photos/{清洗后的店名}/`，识别与保存复用同一次下载；文件扩展名按 magic bytes 判断，不信 URL 后缀。
- 照片库按店铺管理：支持重命名/删除店铺、多图导入、删除单图、选择照片重新识别。手动识别结果打印实际 provider/model，便于核对服务商调用。
- 视觉服务商配置按 provider 独立保存，来回切换不必重新输入 Key；模型下拉只是建议，始终允许手输任意 Model ID。
- 火山豆包当前建议 `doubao-seed-2-0-lite-260428` 或 `doubao-seed-2-0-mini-260428`，项目使用账号级 Key + Model ID 直调，不要求创建 `ep-xxx` 推理接入点。
- 图片先做 URL/内容指纹与近似去重，再按有效性筛选；商家仅有一张照片时要结合候选数、下载失败、去重淘汰和视觉筛选日志判断，不能只看最终落盘数。

## 13. 开发信模板与签名（2026-08-07）

- 单封与批量生成共用 `template_id` 查找链路；显式选择模板后，模板正文以更高字符上限完整进入提示词，缺失模板返回可见错误。
- 话术文档导入保留解析/OCR 原文，不再经过 LLM 总结改写，避免格式和关键措辞丢失。
- 公司资料在设置页单独保存到本机配置。正式邮件追加完整签名；WhatsApp 只追加“联系人｜公司”的紧凑签名；提示词要求模型不要自行生成签名。
- 批量任务完成后切换 Tab 会保留当前结果，直到用户点“清空本批”或启动下一批；运行中切换 Tab 继续轮询。自检问题和 judge 建议在结果展开区显示。
- 客户导入会归一化仪器和缺品推荐并去重；客户表格只显示一份备注详情，缺品推荐与仪器信息合并在同一处。

## 14. 发布前提交边界

- 永不提交：`.env`、`data/config.json`、`data/customers.json`、`data/crawler_photos/`、`data/photo_scan/`、日志、构建后的 sidecar exe。
- `data/products.json`、`data/emails.json` 是可查看的合成样例；若运行过程中被修改，默认不暂存，只有完成人工脱敏后才能更新样例。
- `.agents/` 与 `docs/codex-prompt-*.md` 是本地开发材料，已由 `.gitignore` 排除。
- 旧历史曾包含 `frontend/node_modules`、爬虫错误日志、绝对用户路径和已匿名化前的业务身份。公开前必须使用清理后的历史，不能只提交当前工作区修改。
- 发布前依次运行 `pre-commit run detect-secrets --all-files`、后端测试、离线评测和前端 `build:check`；公开后确认 Secrets Scan workflow 首次完整历史扫描通过。

## 15. 浏览器绿色版（2026-08-07）

- `scripts/build-portable.ps1` 独立构建 `dist/Tradewind-Portable/`，不会重建或覆盖 Tauri sidecar。
- `run.py` 等待 `/api/health` 后再打开默认浏览器；重复双击复用现有服务，8101 被非 Tradewind 程序占用时明确报错。
- 默认数据根为 `%LOCALAPPDATA%\Tradewind`；程序目录存在 `portable.flag` 时改为随目录存储。两种模式数据互相独立。
- 包内初始模板只来自 `packaging/default-data/`，禁止直接打包运行中的 `data/`，避免带出 Key、客户、照片和历史记录。
- 打包版爬虫复用系统 Edge/Chrome，不携带 Chromium。排除 MarkItDown 未使用的 Torch、Transformers、音频识别和数据科学插件后，当前解压目录约 429 MB。
- 内部测试版未做代码签名，跨电脑分发前需预期 Windows SmartScreen 提示；正式对外销售时应补签名证书与干净 Windows 验收。

### 绿色版首轮修复

- MarkItDown 任一可选插件导入失败时，旧逻辑会把 TXT/DOCX 等普通文档错误送进图片 OCR，最终统一报“未识别到内容”。现改为 TXT/Markdown/HTML/DOCX/XLSX/PPTX/PDF 原生解析优先，MarkItDown 仅兜底，扫描型 PDF/图片才走 OCR。
- Qwen 图片请求不再使用统一 40 秒总超时并盲目重试三次：连接 12 秒且最多重试一次，读取等待 90 秒且不重复提交；`qwen3-vl-plus` 显式关闭 thinking，错误回显实际 provider/model 与失败阶段。

### 绿色版交付收尾

- 构建脚本同时生成版本文件、可分发 ZIP 与 SHA256 校验文件，不再依赖人工压缩。
- `scripts/test-portable.ps1` 使用隔离的临时数据目录黑盒启动 EXE，验证健康接口和话术文件导入后自动关闭，不读取用户配置。
- `TRADEWIND_NO_BROWSER=1` 只供自动验收抑制浏览器弹窗，普通用户双击行为不变。
- 当前不建设远程监控或遥测服务。设置页提供用户主动触发的诊断 ZIP，仅包含环境、版本、模型名、数据计数和错误类型/HTTP 状态统计；不包含原始日志或业务内容。
- 绿色版健康检查使用禁用代理的本地 opener，避免对方电脑的 HTTP(S)_PROXY 把 `127.0.0.1` 请求转发出去；首次等待上限为 120 秒，端口已监听时会直接打开页面并提示手动地址。
