# Tradewind · 信风

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React 18](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=111)](https://react.dev/)
[![Tauri 2](https://img.shields.io/badge/Tauri-2-24C8DB?logo=tauri&logoColor=white)](https://v2.tauri.app/)
[![Secrets Scan](https://github.com/inervers/Tradewind/actions/workflows/secrets-scan.yml/badge.svg)](https://github.com/inervers/Tradewind/actions/workflows/secrets-scan.yml)

一个本地优先的外贸获客与开发信 Agent：从机构发现、联系方式提取和设备线索识别，
一路完成缺品分析、个性化写信、规则自检与可选质量评估。耗时操作统一为可取消、
可追踪的后台任务，并提供浏览器绿色版与 Tauri 桌面端两种交付形态。

> 本仓库用于作品展示和工程交流。仓库内品牌、联系人、号码、邮箱、客户与邮件内容均为
> 合成或匿名化示例；请勿把真实客户资料、API Key、照片或运行日志提交到 Git。

<p align="center">
  <img src="docs/assets/tradewind-compose.png" width="49%" alt="Tradewind 个性化开发信工作区，使用合成演示数据" />
  <img src="docs/assets/tradewind-leads.png" width="49%" alt="Tradewind 本地客户线索池，使用合成演示数据" />
</p>

## 30 秒看懂项目

| 问题 | Tradewind 的回答 |
|---|---|
| 输入是什么 | 目标地区与机构关键词、产品资料、话术模板，以及人工确认后的客户背景 |
| Agent 做什么 | 发现机构 → 抽取联系方式与设备线索 → 分析品类缺口 → 检索产品/话术 → 生成开发信 → 规则自检 → 可选 LLM-as-judge |
| 输出是什么 | 可人工审核的邮件 / WhatsApp 文案、持久化外联队列，以及保存在本机的客户与评估记录 |
| 工程重点 | 任务生命周期、逐条发送确认、状态恢复、多模型路由、爬虫降级、桌面分发、运行数据隔离 |
| 明确不做什么 | 不自动群发、不把模型输出或 SMTP 接受当成交付事实、不上传客户资料、不为了小规模本地数据强接向量库 |

## 可验证的工程证据

| 维度 | 仓库内证据 |
|---|---|
| Agent 链路 | `app/email_agent.py`：本地检索、生成、规则检查、可选 Judge、SQLite 记录 |
| 任务可靠性 | `server.py` + `frontend/src/tasks.ts`：统一 task id、轮询恢复、流式状态与取消 |
| 数据采集 | `app/crawler/`：Maps / Webs 机构发现、联系方式提取、照片设备识别与缺品分析 |
| 多端交付 | FastAPI 静态托管、浏览器绿色版、Tauri 2 + Python sidecar，三种运行形态共用后端 |
| 质量保障 | 后端自动化测试、`eval/fixtures/` 合成评测夹具、TypeScript 检查与 Web / Tauri 两套 Vite 构建 |
| 发布安全 | 本地数据目录隔离、`detect-secrets` pre-commit、GitHub Actions TruffleHog 完整历史扫描 |

## Agent 工作流

```mermaid
flowchart LR
    A[机构发现] --> B[联系方式与设备线索]
    B --> C[缺品分析]
    C --> D[产品与话术检索]
    D --> E[LLM 生成]
    E --> F[规则自检]
    F -->|有问题且未重试| E
    F --> G[可选 LLM-as-judge]
    G --> H[LangGraph 暂停等待人工确认]
    H -->|加入外联队列| I[幂等保存生成历史]
    I --> J[逐条确认发送与本地状态记录]
```

单封写信使用 LangGraph checkpoint 保存 `retrieve → generate → rule_check → judge → interrupt → persist`
状态。规则检查最多触发一次定向重写；选择客户的草稿在“加入外联队列”前不会写入生成历史，
重启后仍可按同一个 task id 恢复。批量写信、爬虫、OCR 和照片识别继续使用原有任务机制，
没有为了技术统一强行迁移到 LangGraph，这是本次最小重构的明确边界。

## 核心功能

| 模块 | 能力 |
|---|---|
| **爬虫**（`app/crawler/`） | Google Maps 搜美容院（medspa/beauty clinic…）→ 滚动批量收集 → 详情页挖邮箱/电话/WhatsApp → 照片视觉识别仪器品牌 → **缺品分析**（"缺什么推什么"）→ 客户名单 CSV |
| **写信**（`app/email_agent.py`、`app/email_workflow.py`） | 本地产品库检索 → 按所选话术模板生成 → 规则自检（零 token，至多重试一次）→ 可选 LLM-as-judge → LangGraph 人工确认与 SQLite checkpoint。批量路径保持原实现 |
| **外联队列** | 生成结果先保存为待审核任务；WhatsApp 仅准备 `wa.me` 链接并由人工确认结果，QQ/Gmail 邮件逐封二次确认后才通过 SMTP 提交；重启后任务仍可恢复 |
| **话术模板** | 历史邮件（脱敏）或文档原文导入；生成时显式选择，正文完整参与提示词，不再由导入模型改写 |
| **照片库** | 按店铺管理爬虫照片，重命名/删除店铺，手动导入多张图片，选择图片重新识别并查看实际 provider/model |
| **文档识别导入** | PDF/图片 → OCR（RapidOCR）→ 产品资料结构化入库；话术资料保留解析/OCR 原文，任务制支持取消 |
| **前端 / 桌面端** | React + Vite 工作区；Tauri 2 桌面壳负责单实例、启动动画、后端 sidecar 生命周期和窗口恢复 |

## 工程设计与技术取舍

| 设计决策 | 原因与边界 |
|---|---|
| 小规模资料使用本地 JSON + 关键词加权检索 | 产品和话术只有几十条，标题、标签和正文可解释打分比远程向量库更容易分发、调试和隔离；数据规模扩大后再评估 Embedding |
| 确定性规则先于 LLM Judge | 长度、签名、禁用表达等问题先用零 token 规则稳定发现；Judge 只负责相关性、清晰度等语义维度，降低成本并保留可复现性 |
| 主语言模型与视觉模型分开路由 | 写信与照片识别的能力、价格和网络条件不同；各 Provider 独立保存配置，单一服务商故障不会让整个采集链路失效 |
| 耗时操作统一任务制 | 邮件、爬虫和 OCR 都返回 task id，由前端轮询状态；切换工作区后仍可恢复，取消时不写入不完整结果 |
| 外联发送采用逐条确认 | WhatsApp 只生成并打开预填链接，QQ/Gmail 只有在用户核对发件账号、收件人、主题和正文后才提交；不做后台群发 |
| SMTP 结果采用保守状态 | `accepted` 只表示邮箱服务器接受，不能证明送达或阅读；提交结果不确定时标为 `unknown`，必须先查发件箱才能决定状态或重试 |
| 采集链路分层降级 | 没有视觉 Key 时仍保留本地文本识别、联系方式提取和缺品推荐；第三方模型不可用不阻断基础流程 |
| 本地优先而非多租户 SaaS | 客户、邮件、照片和 Key 留在用户数据目录，升级程序不覆盖；代价是当前不提供账号、协作和云同步 |

补充实现：Webs 发现优先机构官网结果，不足时合并 Google / Bing；新闻、百科、
目录站和行业无关页面在入库前过滤。签名由系统统一追加，避免模型重复生成或遗漏
联系人信息。更完整的取舍与事故复盘见 [OPS-NOTES.md](OPS-NOTES.md)。

## 建议代码阅读路径

1. `server.py`：FastAPI 任务接口、运行数据边界和静态资源托管。
2. `app/email_agent.py`：检索、生成、自检、Judge 与记忆记录的主链路。
3. `app/crawler/`：机构发现、联系方式抽取、视觉识别与缺品分析。
4. `frontend/src/tasks.ts` 与各工作区：任务恢复、取消和结果呈现。

## 工程边界

- 项目定位是本地单用户工具，不是多租户 SaaS；没有账号、权限和远程数据库。
- LLM 输出、联系方式和缺品建议都需要人工确认，不能直接视为事实或自动发送。
- Google Maps、搜索引擎和第三方模型接口可能因地区、网络、限流或页面结构变化而失效。
- 邮件仅支持 QQ/Gmail 的 SMTP 逐封确认发送；不包含 Gmail OAuth、IMAP 回执、自动回复、定时发送、群发或送达/阅读追踪。
- 工具提供禁止联系名单和退订文案，但不保证任何地区的陌生开发合规；发送者仍需核验当地法律、平台规则和客户授权。
- 仓库未附开源许可证，代码目前仅供查看和评估；如需复用，请先联系仓库维护者。

## 数据与密钥安全

- `.env` 与 `data/config.json` 只保存在本机；`.env.example` 仅列出变量名和占位值。
- QQ 授权码或 Gmail 应用专用密码只写入当前 Windows 用户的凭据管理器；设置接口、诊断包和 Git 文件都不会返回或导出明文凭据。
- 客户名单、SQLite、爬虫照片、诊断日志、OCR 中间文件、输出目录和构建产物均由 `.gitignore` 排除。
- `data/products.json`、`data/emails.json` 与 `packaging/default-data/` 只包含演示数据。使用界面导入业务资料后，不要提交这些文件的运行时变化。
- 提交前由 `detect-secrets` 扫描，GitHub Actions 再用 TruffleHog 检查 push、PR 和完整历史。

## 快速开始

```powershell
pip install -r requirements.txt

# 源码版如需爬虫和文档 OCR
pip install -r requirements-optional.txt
playwright install chromium

# 启动后端（端口 8101）
python run.py

# 构建前端（包含 TypeScript 检查）
cd frontend
npm ci
npm run build:check
```

浏览器打开 `http://127.0.0.1:8101`。首次使用在设置页配置写信与视觉模型；如需邮件发送，再选择 QQ 或 Gmail、填写邮箱地址，并把授权码或应用专用密码保存到 Windows 凭据管理器。删除凭据不会删除已保存的外联记录。

### 外联队列的使用边界

1. 从客户名单选择“准备邮件”或“准备 WhatsApp”，生成后加入外联队列。
2. WhatsApp 会先在本机记录为 `opened`，再打开预填链接；只有用户点“确认已发送”才记为手动已发。
3. QQ/Gmail 邮件发送前再次展示发件账号、完整收件地址和主题；确认后才调用 SMTP。
4. `accepted` 表示 SMTP 服务器接受了消息，不等于送达或已读。`unknown` 表示提交结果无法确认，先登录发件箱核查，确认未发后再重试。
5. “禁止再联系”按渠道和联系方式生效；界面列表只显示掩码目标。邮箱设置切换账号后，旧草稿必须显式改用当前账号并重新审核。

### 客户名单内的跟进

点击客户行的“跟进”，在右侧面板记录沟通时间、渠道、结果与内容，安排下一次联系。
每位客户只有一个有效安排；调整、完成和取消会留在时间线中。“今日跟进”“逾期跟进”
和“未安排跟进”可与 Maps / Webs / 手动来源筛选组合使用，名单同时显示日期、事项与最近联系时间。

- 跟进内容独立保存，不混入仪器备注或生成开发信的上下文，也不会自动改写已发 / 已回复状态。
- 未保存内容在关闭、切换客户或切 Tab 前提醒；请求失败保留输入，重试使用相同请求编号，避免重复保存。
- 删除沟通记录后只保留请求编号与客户编号的防重标记，不保留正文，防止另一窗口的迟到重试重新创建记录；删除客户时一起清理这些标记。
- 完成或取消安排后重新读取当前有效安排，避免旧请求的重试隐藏另一窗口新建的安排；嵌套确认弹窗关闭后恢复页面滚动与键盘焦点。
- 外联时间线只展示已知事实：打开 WhatsApp 不代表已发，SMTP 接受提交不代表送达，回复仍由人工标记。
- 日期按运行后端的本机日期判断；页面聚焦或每分钟刷新摘要，不提供系统通知、定时自动联系或收件箱同步。
- 数据存于当前运行数据目录下的 `tradewind_memory.db` 独立跟进表。删除客户会删除该客户的跟进记录和安排，但保留原有外联审核记录及禁止联系规则。请先备份重要信息。
- 数据库清理失败会显式提示，可点“重试跟进清理”；名单损坏或客户 ID 重复时停止写入和清理，不覆盖原文件。
- 诊断 ZIP 不含跟进正文或安排事项；迁移业务资料请使用设置页的业务备份，勿只复制一份 JSON 而遗漏关联记录。

源码网页验收（先运行前端 `npm run build:check`）：

```powershell
python -m unittest discover -s tests -v
python scripts/test-followup-web.py
python scripts/test-followup-regressions.py
```

浏览器验收使用临时合成数据、随机回环端口和本机 Edge，不占用 8101，不读取真实 Key，不调用模型或 SMTP。
这些源码改动不会自动更新旧绿色包或桌面 sidecar；发行包需另行构建。

### 本地业务备份与恢复（源码网页）

本轮只交付源码网页功能，现有绿色包、EXE 和 Tauri sidecar 未更新。旧包不会因刷新
页面获得新后端。用户先验收源码网页，发行包构建另行确认。

设置页的“业务备份与恢复”与“脱敏诊断包”分别使用。业务 ZIP **不是脱敏文件**，
包含真实业务资料时请按客户数据保管，勿上传到 Git、公开分享或当诊断包发送。
导出包含客户、产品、话术、生成历史、外联队列、禁止联系规则、客户跟进记录与安排、
爬虫去重记录；不含运行配置、API Key、公司/模型/SMTP 设置、Windows 凭据、日志、
checkpoint、localStorage、旧备份或正在运行的任务。

- “包含照片”默认不勾选。勾选后包含 `crawler_photos/` 与 `photo_scan/`；恢复时替换这两个目录。
  不含照片的包恢复时保留当前照片。照片初始数量未知，缓存可能过时；业务字节估算不含照片，
  不代表最终 ZIP 大小或磁盘余量，实际数量以严格预览为准。
- 上传 ZIP 上限 2 GiB，预览有效期 30 分钟。上传/预览不会恢复资料；修改业务资料后旧预览会失效，
  应重新预览。准备恢复前必须成功建立完整救援，准备完成仍未应用，可以取消并保留救援。
- 刷新或重新打开设置页会找回后台仍保留的已完成预览，可继续核对、取消预览或更换 ZIP；
  更换前须确认旧预览取消。上传中及恢复准备中的材料不会作为可复用预览，程序重启后临时预览不保留。
- 准备完成后需由用户关闭并重新启动**同一个运行实例，保持同一数据根目录**。
  仅重新加载网页不能应用恢复。源码、默认绿色版、portable.flag 模式和桌面端数据根互相独立，
  不要为恢复而随意切换启动方式或数据目录。软件不会替用户重启正在运行的实例。
- 恢复会替换包内白名单业务资料，但本机禁止联系规则与备份规则取并集。邮件待处理任务转为
  发件箱待核查状态，WhatsApp 任务需人工复核，不自动续跑或发送；换电脑未配置 Key 时仍可浏览资料，
  写信和发送须另行配置本机模型/邮箱，Key 与 Windows 凭据不会随 ZIP 迁移。
- 恢复后仍开着的旧页面可复制草稿，但不能写入新版本数据，请重新打开页面。旧 checkpoint 不重放。
  如需撤回恢复，下载对应救援 ZIP、重新预览并准备，再按同一数据根手动重启。
- 下载交由浏览器流式保存。取消/失败后可报告并重试，不代表下载成功；本页删除保护只在用户核实
  浏览器下载结束后解除，服务端仍拒绝删除正在传输或用于恢复的救援材料。删除会移除整组救援，
  软件不会自动淘汰救援。

隔离源码验收（使用项目 Python，先构建前端，不安装新依赖）：

```powershell
python -B scripts/test-backup-core.py
python -B -m compileall -q server.py app tests scripts/test-backup-core.py scripts/test-backup-web.py
cd frontend
npm run build:check
cd ..
python -B scripts/test-backup-web.py --group all
python -B scripts/test-backup-core.py --script test-followup-web.py
python -B scripts/test-backup-core.py --script test-followup-regressions.py
```

runner 在应用导入前剥离真实 Key/路径环境，使用自有临时数据根、随机回环端口与网络/凭据防线；
不占用 8101，不读取真实配置、照片或 Windows 凭据，不连接模型、SMTP 或 WhatsApp。
备份浏览器验收分 `safety`、`ui` 与 `all`，合成结果与截图保存在忽略的 `outputs/backup-acceptance/`，
不是发行包或真实业务备份。完整回归及独立复审后仍需用户网页验收，不能替代干净 Windows 人工验收。

### 浏览器绿色版

```powershell
# 构建前端并生成免安装目录
powershell -ExecutionPolicy Bypass -File scripts/build-portable.ps1

# 一次完成安全扫描、测试、评测、构建、校验与解压 smoke
powershell -ExecutionPolicy Bypass -File scripts/run-minimum-release.ps1
```

构建结果包括 `dist/Tradewind-Portable/` 免安装目录、带日期版本号的 ZIP 和对应
SHA256 校验文件。用户完整解压 ZIP 后双击 `Tradewind.exe`，程序会等待
`/api/health` 就绪后打开默认浏览器。重复双击只会重新打开页面，不会再启动后端。

```powershell
# 发布前用临时数据目录启动 EXE，并实测健康接口、文件导入与外联安全状态机
powershell -ExecutionPolicy Bypass -File scripts/test-portable.ps1
```

- 默认数据目录：`%LOCALAPPDATA%\Tradewind\data`，升级程序目录不会覆盖用户数据。
- 将随包的 `portable.flag.example` 重命名为 `portable.flag` 后，数据改存程序目录，适合 U 盘携带。
- 打包只带 `packaging/default-data/` 中的干净初始模板，不包含源码目录下的 Key、客户、照片和运行记录。
- 爬虫复用用户电脑上的 Microsoft Edge 或 Google Chrome，不额外捆绑浏览器。
- 浏览器绿色版与 Tauri 桌面端是两条独立构建链，执行本脚本不会重建桌面 sidecar。
- 设置页可由用户主动导出脱敏诊断包；软件不做后台遥测，不上传 Key、客户内容、邮件、照片或原始日志。
- 诊断包中的外联信息只有任务总数、渠道/状态计数和是否配置邮箱，不含客户、收发件地址、主题、正文、Message-ID、禁止联系明细或凭据。
- `2026.09.14-min` 已在一台独立 Windows 11 家庭版中文 x64（build 26200）电脑完成 ZIP/EXE/测试脚本哈希校验、解压 smoke、浏览器界面、重复启动与进程清理验收；对应源码提交为 `67d3907`。
- 目标电脑可检测到 Python 命令，因此该证据只表述为“独立 Windows 电脑验收”，不夸大为“无 Python 环境”；Tauri 不在本轮验收范围内。

### Tauri 桌面开发

```powershell
cd frontend

# 源码开发：Vite + Tauri；桌面壳会从项目环境启动 Python 后端
npm run desktop:dev

# 只构建桌面前端并检查类型
npm run build:desktop

# 完整发行构建（会先重建体积较大的 Python sidecar）
npm run desktop:build:full
```

当前桌面阶段采用 Tauri 2 + PyInstaller sidecar。应用会等待
`http://127.0.0.1:8101/api/health` 后恢复配置；重复打开会唤醒已有窗口。
桌面运行数据写入 `%LOCALAPPDATA%\com.tradewind.desktop\data`，源码版仍使用项目内 `data/`，两者互不覆盖。

### 命令行直跑

```powershell
# 爬虫（需代理 Verge 7897，Google Maps 国内不可达；香港市场）
python -m app.crawler.maps_hunter --queries "medspa" --country 香港 --max 5 -v

# 官网深挖（邮箱/电话/WhatsApp/社媒/仪器）
python -m app.crawler.webs_hunter --queries "medical aesthetic clinic Hong Kong laser treatment" --max 5 -v

# 单封开发信
python -m app.email_agent "Glow Skin Clinic" --country 香港 --product 激光脱毛仪 --judge -v
```

## 测试与发布前检查

```powershell
# 后端单元测试
python -m unittest discover -s tests -v

# 离线生成质量评测，不消耗 token
python eval/generate_quality_report.py

# 前端类型检查与构建
cd frontend
npm ci
npm run build:check

# 首次安装提交前安全钩子
cd ..
python -m pip install pre-commit detect-secrets
pre-commit install
pre-commit run detect-secrets --all-files
```

评测夹具位于 `eval/fixtures/`，只使用合成数据。便携版发布由
`scripts/run-minimum-release.ps1` 统一执行安全扫描、测试、评测、构建、哈希校验与
解压 smoke；`2026.09.14-min` 已在独立 Windows 11 x64 电脑完成人工界面验收。

## 爬虫视觉识别（缺品分析）

流程：商家照片 → 视觉模型认仪器 → 对比产品库品类画像 → 输出"设备名（缺：品类）"。

- **多服务商**（设置页视觉卡切换）：智谱 GLM、火山豆包和 OpenAI；模型名允许手输，以适应服务商模型迭代。
- 火山默认建议 `doubao-seed-2-0-lite-260428` / `doubao-seed-2-0-mini-260428`，使用 Model ID 直调，不依赖 `ep-xxx` 推理接入点。
- **产品品类画像只用 title+tags**：适应症描述（脱毛/嫩肤）是使用场景不是设备类型，混入会污染对比。
- 没配视觉 key 时自动降级：只做文本仪器检测 + 缺品推荐，爬虫主流程不受影响。
- 爬虫照片可选落盘到 `data/crawler_photos/`，落盘与识别复用同一次下载；照片库也支持人工复核和重新识别。

## 桌面分发

```powershell
cd frontend
npm run desktop:build:full
```

- Tauri 壳与 `tradewind-backend` sidecar 一起分发；sidecar 内含后端、爬虫和可用的 OCR 依赖。
- `config.json`、客户名单、照片和历史记录不进包；首次运行在本机数据目录生成，API Key 不随安装包分发。
- sidecar 构建耗时且产物较大，只有后端依赖或打包代码变化时才重建；普通前端迭代不需要重建。

## 目录结构

```
Tradewind/
├── run.py                 # 网页源码版入口：uvicorn 起 8101 + 自动开浏览器
├── desktop_backend.py     # Tauri sidecar 入口：只起本地 API
├── TradewindBackend.spec  # PyInstaller sidecar 配置
├── server.py              # FastAPI：任务制（邮件/爬虫/导入）、配置、流式输出
├── app/
│   ├── config.py          # 写信/视觉多服务商与代理配置（data/config.json）
│   ├── outreach_store.py  # SQLite 外联队列、状态机与禁止联系名单
│   ├── outreach_smtp.py   # QQ/Gmail 单封 SMTP 提交与安全错误映射
│   ├── credential_store.py # Windows 凭据管理器封装（不回退明文文件）
│   ├── llm.py             # 多服务商 LLM 封装（流式 + 取消）
│   ├── email_agent.py     # 开发信生成（检索→生成→自检→打分→历史日志）
│   ├── memory.py          # SQLite history/evaluation log（不自动回灌）
│   ├── crawler/
│   │   ├── maps_hunter.py     # Google Maps 爬虫（滚动/详情/照片/缺品）
│   │   ├── vision_analyzer.py # 照片视觉识别（OpenAI/智谱，浏览器下载）
│   │   ├── equipment.py       # 品类别名 + 品类画像 + 缺品分析
│   │   └── lead_hunter.py     # DDG 搜官网挖邮箱（轻量备选）
│   └── tools/             # 本地检索 + DDG 兜底
├── frontend/              # React + Vite 前端；src-tauri/ 为桌面壳
├── scripts/               # 数据导入、批量邮件、sidecar 构建等脚本
├── packaging/             # 绿色版干净初始数据与随包使用说明
└── data/                  # 演示产品/话术；客户、配置、日志与运行产物被忽略
```

## 待办

- [x] Google Maps 爬虫（Playwright，滚动批量 + 详情页挖联系方式）
- [x] 照片视觉识别 + 缺品分析（智谱 / 火山豆包 / OpenAI）
- [x] 话术模板 Tab / 文档 OCR 导入 / 多服务商 / 流式 + 真后台取消
- [x] Tauri 桌面壳（单实例、后端自启、启动画面、AppLocalData 数据目录）
- [x] 生成 Browser Portable ZIP 并完成独立 Windows 11 x64 环境验收
- [ ] 生成 Tauri 正式安装包并完成独立 Windows 环境验收（当前范围外）
- [ ] 用户侧私有资料导入验收（仅保存在本机，测试材料先脱敏）
- [x] 审核式外联闭环（WhatsApp 手动确认 + QQ/Gmail 单封 SMTP + 状态/禁止联系）
- [ ] 跨区域批量采集与反爬稳定性验证
