# AGENTS.md — AI 开发规范（Morrow / claude-telegram-bot）

本文件是**本仓库所有 AI 助手与自动化代理的统一行为契约**。在本仓库执行任何操作前必须先阅读本文件。

本文件**不包含任何真实凭据**。所有敏感配置只以**环境变量名称**形式出现。

---

## 1. 项目概览

私有 AI 伴侣系统，单体单进程、单 owner，同时服务三条通道（Telegram / QQ / iOS App），具备长期记忆、持久情绪状态、主动消息、自主日常活动与实时双语语音通话。

- 语言：Python（生产代码下限 3.10；测试套件因 `asyncio.timeout` 需 ≥3.11；本地验证使用 3.12.10）
- 存储：PostgreSQL（生产唯一持久层）
- 部署：Railway（主服务）+ 独立 NapCat 容器（QQ 网关）
- 跟踪文件：63 个（全部为源码 / 测试 / 文档 / 必要配置）

---

## 2. 架构与主要模块

### 2.1 依赖链

```
morrow_runtime.py  ──(先设 MORROW_ONLY=1)──▶ runner_20m.py
（生产入口）                                   │
Procfile: worker: python runner_20m.py ────────┘
                                              ▼
                        runner_strict_bubbles.py ──▶ runner.py ──▶ bot.py
```

### 2.2 模块职责

| 模块 | 职责 |
|---|---|
| `bot.py` | 模型调用（LLMHub，OpenAI 兼容）、历史、世界书、语音转写；含**已废弃的 SQLite 实现** |
| `runner.py` | Telegram 装配、PostgreSQL 记忆接入、命令注册 |
| `runner_strict_bubbles.py` | 严格多气泡切分、主动消息循环 `proactive_loop`、日常 tick 分派 |
| `runner_20m.py` | 主动节奏 20 分钟–3 小时、情绪钩子、平台记录、手机桥接钩子 |
| `morrow_runtime.py` | **生产入口**：禁用 Telegram/QQ，只起 iOS API |
| `ios_api.py` | iOS REST/WS 服务（24 条路由，除 `/health` 外均需 Bearer 鉴权） |
| `memory_pg.py` | 长期记忆 / 收藏 / 关系摘要 / 日记 / 动作回执（PostgreSQL） |
| `emotion_pg.py` | 情绪状态持久化与正则触发器 |
| `continuity.py` | 未完成话题的续接与回访 |
| `proactive_policy.py` | 主动消息纯策略（无 IO） |
| `qq_connector.py` | OneBot v11 私有 WebSocket 客户端（QQ 主控） |
| `qq_gateway.py` | QQ 事件 → 类 Telegram 形状适配（**iOS 通道亦复用**） |
| `phone_bridge.py` | owner 共享空间 + 真实 iOS 工具执行（结构化工具，非字符串协议） |
| `companion_agent.py` | 自主活动引擎 + 心跳（PG 租约防多实例） |
| `morrow_daily.py` | 每日日记 / 小纸条 / 原话收藏（单事务） |
| `morrow_call.py` / `morrow_call_realtime.py` | 通话：HTTP 分轮 / WebSocket 实时双语 |
| `voice_output.py` | TTS：Edge TTS（默认）/ CosyVoice / Fish Audio |
| `morrow_music.py` / `morrow_radio.py` | 音乐检索卡片 / 电台分段 TTS |
| `morrow_services.py` | 浏览、收听/健康上下文、只读网页 MCP |
| `realtime_clock.py` | 自然时钟（生成后再采样时间） |
| `worldbooks.py` / `milestones.py` | 世界书 / 重要日期 |
| `napcat/` | **独立 Railway 服务**：NapCat QQ 网关容器 |

### 2.3 已知的装配特性（务必了解）

本仓库存在**约 25 处 monkey-patch**，横跨 4 个模块：

- `runner.py:22-30,75,199` 覆盖 `bot.*`
- `runner_strict_bubbles.py:90,587,606` 覆盖 `runner.*`
- `runner_20m.py:497,524,556,585-586,608-609,664` 再覆盖 `app.runner.*`

**含义：导入顺序即行为契约。** 不要随意调整这些模块的导入次序，也不要把它们当成可独立运行的脚本。

---

## 3. 运行模式（关键区别）

| | **Morrow 生产模式** | **默认全功能模式** |
|---|---|---|
| 启动 | `python morrow_runtime.py` | `python runner_20m.py` |
| `MORROW_ONLY` | `1`（由入口强制设置） | 未设置 |
| Telegram 轮询 | **不启动** | **启动** |
| QQ OneBot | **不启动** | **启动** |
| iOS API | 启动（需 `IOS_APP_TOKEN`） | 启动（需 `IOS_APP_TOKEN`） |
| 主动消息投递 | 写入 PostgreSQL 共享历史（平台 `ios`） | Telegram / QQ |

**本地开发一律使用 Morrow 模式**，或只运行单元测试。

> ⚠️ **`Procfile` 中的 `worker: python runner_20m.py` 是过时残留**，与生产实际启动命令不一致。生产启动命令以 Railway 服务设置为准。

---

## 4. 生产启动命令

```
python morrow_runtime.py
```

- 该入口在导入前强制设置 `MORROW_ONLY=1`（`morrow_runtime.py:4`）。
- 健康检查路径：`GET /health`（免鉴权）。
- **`IOS_APP_TOKEN` 缺失时 iOS API 不会启动**，`/health` 随之不存在，`morrow_runtime.py` 会抛出 `RuntimeError('Morrow API did not start')` 并退出。该变量是硬性启动依赖。
- **不要在没有明确批准的情况下改动 Railway 的 Start Command 或 Procfile。**

---

## 5. 本地开发环境

| 项 | 值 |
|---|---|
| 仓库根 | `D:\AI-Projects\Bot\claude-telegram-bot` |
| Python 虚拟环境 | `D:\AI-Projects\Bot\.venvs\claude-telegram-bot` |
| 解释器 | `D:\AI-Projects\Bot\.venvs\claude-telegram-bot\Scripts\python.exe`（Python 3.12.10） |
| Git 可执行文件 | `C:\Program Files\Git\cmd\git.exe` |

**注意**：
- `git` **不在系统 PATH 上**。在 Harness/沙箱中必须使用上面的**绝对路径**调用。
- **不要修改系统 PATH 或任何系统环境变量。**
- 虚拟环境位于**仓库之外**，不会被误提交。

---

## 6. Git 规范

1. **禁止直接推送 `main`。** `main` 是生产分支，被推送即触发 Railway 部署。
2. 所有改动在**独立开发分支**上进行，命名前缀：
   - `chore/*` — 工程配置、工具链、文档
   - `fix/*` — 缺陷修复
   - `feat/*` — 新功能
   - `diagnose/*` — 只读诊断
3. 每次改动前先确认分支：`git rev-parse --abbrev-ref HEAD`。
4. 一次分支只做一件事；不要把无关改动混入同一分支。
5. **不得改写 Git 历史**（`reset --hard`、`rebase` 已推送提交、`commit --amend` 已推送提交、`push --force`）。
6. 未经批准**不得**执行：`git push`、创建 PR、合并 PR。

---

## 7. 修改前的代码分析要求

在任何代码改动之前，必须先完成：

1. **读原文**：使用读取工具查看目标文件的实际内容，不凭文件名或记忆推断。
2. **追踪调用关系**：确认该函数/模块被谁导入、被谁调用。注意 monkey-patch 链。
3. **区分三种状态**，并在结论中明确标注：
   - (A) 已实现且已接入可运行入口
   - (B) 代码存在但无入口启动 / 实际不可达
   - (C) 需真实外部环境验证（凭据、真机、线上服务）
4. **提供证据**：每条结论必须给出 `文件:行号`。
5. **不得在缺乏代码证据时声称存在真实 Bug。** 尤其禁止在**未实际执行**的情况下断言「必然报错」「必然崩溃」。
6. 涉及运行路径判断时，**必须实际执行只读命令验证**，不能仅静态推断。

---

## 8. 测试流程

### 8.1 测试套件概况

- 18 个 `test_*.py`，共约 **218** 个用例，**全部基于标准库 `unittest`**（不使用 pytest，`pytest` 也未在 `requirements.txt` 声明）。
- 异步用例使用 `unittest.IsolatedAsyncioTestCase`。
- **无 `conftest.py`**；共享 fixture 通过**测试文件互相导入**实现（如 `test_ios_api.FakeStore`），因此**必须在仓库根目录运行**。

### 8.2 运行方式

```powershell
& "D:\AI-Projects\Bot\.venvs\claude-telegram-bot\Scripts\python.exe" -m unittest discover -s . -p "test_*.py" -v
& "D:\AI-Projects\Bot\.venvs\claude-telegram-bot\Scripts\python.exe" -m unittest test_ios_api -v
& "D:\AI-Projects\Bot\.venvs\claude-telegram-bot\Scripts\python.exe" -m py_compile bot.py runner.py runner_20m.py runner_strict_bubbles.py ios_api.py
```

### 8.3 分批执行纪律

**不要一次性运行全部测试。** 按风险分批：

| 批次 | 范围 | 前置条件 |
|---|---|---|
| 1 | 纯逻辑用例（无 socket、无 DB） | 无 |
| 2 | 使用 `aiohttp TestServer` 本地回环的用例 | 确认无本地代理拦截回环 |
| 3 | **动态 exec 生产模块**的用例 | **必须在网络隔离下运行** |

**暂缓运行（需批准）**：`test_romance.py`、`test_proactive.py`、`test_realtime_clock.py` —— 这三个文件会动态 `exec` 生产模块（`runner_20m.py`、`runner_strict_bubbles.py`、`bot.py`），理论失效爆炸半径最大。

### 8.4 测试前后核验

```powershell
& "C:\Program Files\Git\cmd\git.exe" status --porcelain
& "C:\Program Files\Git\cmd\git.exe" status --porcelain --ignored=matching
```

测试**不应**产生任何文件改动或残留产物。若发现新增文件（`__pycache__` 除外），立即中止并报告。

### 8.5 测试失败处理

**只分析原因，不擅自修改生产代码。** 报告失败用例、断言内容、以及根因推断（附证据），等待批准后再修。

---

## 9. 代码审查与验收流程

1. 改动后先跑**批次 1** 测试，通过后再跑相关专项测试。
2. 提交前自检清单：
   - 是否新增环境变量？→ 更新 `AGENTS.md` 与文档中的变量名清单
   - 是否新增 PostgreSQL 表？→ 补充 schema 说明
   - 是否影响 `/health` 可用性？
   - 是否改动了 monkey-patch 链？
   - 是否留下调试代码、临时脚本或死代码？
3. 提供 `git diff` 供人工验收。
4. 验收通过后才允许进入提交环节。

---

## 10. GitHub Pull Request 与 Railway 发布规范

1. **未经明确批准，不得创建或合并 PR。**
2. **未经明确批准，不得触发任何 Railway 部署**（含 `railway up`、`railway redeploy`，以及通过推送 `main` 间接触发）。
3. PR 需包含：改动目的、影响范围、证据（`文件:行号`）、测试结果、回滚方式。
4. 合并到 `main` 前必须确认：测试通过、无敏感文件混入、文档已同步。
5. 部署后需人工验证 `GET /health` 返回正常。

---

## 11. 安全限制（强制）

### 11.1 凭据

- **禁止输出任何 API Key、Token、密码、DSN 或私钥内容。** 只允许引用**环境变量名称**，值一律写 `<redacted>`。
- 禁止把真实凭据写入代码、注释、文档、测试或提交信息。
- 若在文件中发现疑似真实凭据：**只报告文件路径与行号**，不复制、不回显其内容。

### 11.2 环境变量（仅名称，不含值）

`TELEGRAM_BOT_TOKEN`、`TELEGRAM_OWNER_ID`、`BOT_ACCESS_CODE`、
`LLMHUB_API_KEY`、`LLMHUB_BASE_URL`、`CLAUDE_MODEL`、`MEMORY_MODEL`、`TRANSCRIBE_MODEL`、
`SYSTEM_PROMPT`、`MAX_HISTORY_MESSAGES`、`REQUEST_TIMEOUT_SECONDS`、`STREAM_EDIT_INTERVAL`、
`AUTO_MEMORY_COOLDOWN_SECONDS`、`MEMORY_DB_PATH`、
`DATABASE_URL`、`MORROW_ONLY`、`IOS_APP_TOKEN`、`PORT`、
`COSYVOICE_API_KEY`、`FISH_API_KEY`、`FISH_DEFAULT_VOICE_ID`、`FISH_TTS_MODEL`、
`ELEVENLABS_API_KEY`、`ELEVENLABS_VOICE_ID`、`ELEVENLABS_TTS_MODEL`、`ELEVENLABS_CALL_TTS_MODEL`、
`QQ_OWNER_UIN`、`QQ_AUTO_STATUS`、`NAPCAT_WS_URL`、`NAPCAT_ONEBOT_TOKEN`、`QQ_PAIR_CODE`、`NAPCAT_SELF_UIN`

### 11.3 数据与消息

- **禁止连接真实生产 PostgreSQL。** 本地开发必须使用独立数据库。
- **禁止发送真实 Telegram 或 QQ 消息。** 包括主动消息、QQ 空间说说、个性签名、表情包、语音。
- 禁止调用收费模型或语音 API 做实验性验证。
- 禁止修改生产数据。

---

## 12. 明确禁止事项

以下行为**未经用户明确批准一律禁止**：

1. **擅自运行生产服务**（`morrow_runtime.py`、`runner_20m.py` 或任何会启动 Telegram 轮询 / QQ 连接 / iOS API 的入口）。
2. **连接真实生产 PostgreSQL**。
3. **发送真实 Telegram、QQ 消息**。
4. **输出 API Key、Token 或其他密钥**。
5. **直接推送 `main`**。
6. **未经批准执行 Git push、合并 PR、Railway 部署**。
7. **修改 Windows ACL 或切换完全权限模式**。
8. **未经批准大规模重构现有代码**（跨模块改写、删除 monkey-patch 链、更换数据层、改 PostgreSQL schema）。
9. 改写 Git 历史。
10. 修改系统 PATH 或系统环境变量。
11. 未获批准自行创建文件（报告类产出先在会话中展示）。
12. 自动 commit。

---

## 13. 子代理分工与交叉验证

### 13.1 推荐分工（五路并行）

| 路 | 范围 |
|---|---|
| A | Telegram / QQ 接入层 |
| B | iOS App + iOS API |
| C | AI 模型调用 + 记忆 + 情绪状态 |
| D | 运行时入口 + 主动行为 + 语音通话 + 部署 |
| E | 测试套件 + 开发环境 |

每路的产出必须自包含：目的、结论、证据（`文件:行号`）、以及 (A)/(B)/(C) 三态分类。

### 13.2 交叉验证要求（强制）

1. **任何「高危」结论必须经第二方复核后才能写入最终报告。** 主代理或另一子代理必须独立读取相关代码并确认。
2. 子代理**不得**在未实际执行的情况下断言运行时行为（如「必然抛异常」「必然崩溃」）。此类断言必须标注为「静态推断，未执行验证」。
3. 涉及**计数、路由条数、文件数**的结论必须逐行核算，不接受概略估计。
4. 子代理报告的元信息（如「无法确认是否为 Git 仓库」）若源于工具限制而非事实，必须由主代理用其他手段核实后再定论。
5. 主代理在汇总时必须**明确标注被驳回或修正的子代理结论**，避免错误结论流入后续决策。

---

## 14. 已知陷阱清单

| # | 陷阱 | 证据 |
|---|---|---|
| 1 | `runner_20m.py` 直接运行会启动 Telegram 轮询 + QQ | `runner_20m.py:667-668` → `runner.py:716` |
| 2 | `Procfile` 与生产启动命令不一致 | `Procfile` vs `MORROW.md:9` |
| 3 | `IOS_APP_TOKEN` 缺失导致进程退出（`/health` 随之消失） | `ios_api.py:424-426`、`morrow_runtime.py:18-19` |
| 4 | `morrow_runtime.py:1` 的 docstring 声称「without Telegram polling」，但导入的 `runner_20m` 在 `__main__` 下会轮询 | `morrow_runtime.py:1` |
| 5 | `bot.py` 的 SQLite 实现已被 `memory_pg` 覆盖，属死代码 | `runner.py:22-30` |
| 6 | `bot.py:956` 存在无人调用的遗留 `main()` | `bot.py:956-957` |
| 7 | 记忆整理被硬编码 20 秒截断（与 `REQUEST_TIMEOUT_SECONDS` 不一致） | `memory_pg.py:777-782` |
| 8 | `emotion_pg.prompt_block` 是同步 PG 调用，位于 async 路径 | `runner_20m.py:538` |
| 9 | 异常日志普遍只打印类型名，不含异常消息 | `runner_strict_bubbles.py:541-542`、`ios_api.py:440` 等 |
| 10 | 文档漂移：`MORROW.md:3` 写 `client 0.12.0`，实际 `ios/project.yml:10` 为 `0.1.0` | 已核实 |
| 11 | `git` 不在 PATH，必须用绝对路径 | 本机 PATH 实测 |
| 12 | 根目录 `.gitignore` 的 `*.sqlite` 规则不覆盖本项目实际使用的 `.db` | `bot.py:33` |

---

## 15. 快速参考

```powershell
$git  = 'C:\Program Files\Git\cmd\git.exe'
$py   = 'D:\AI-Projects\Bot\.venvs\claude-telegram-bot\Scripts\python.exe'
$repo = 'D:\AI-Projects\Bot\claude-telegram-bot'

& $git -C $repo status --short --branch
& $git -C $repo rev-parse --abbrev-ref HEAD
& $git -C $repo ls-files
& $py -m unittest test_ios_api -v
```

**本文件不包含任何真实凭据。**
