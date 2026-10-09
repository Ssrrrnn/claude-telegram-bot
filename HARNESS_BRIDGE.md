# Morrow ↔ DeepSeek Harness：GitHub Issues 任务桥（v1）

此桥接把 ChatGPT 下发的 GitHub Issue 送到**用户自己的 Windows 机器**，
再由用户明确批准执行 DeepSeek Harness。适用于 Morrow Python 后端；
iOS 任务也有路由字段，但须单独准备本地 iOS checkout 和忽略规则。

**当前状态：仅在开发分支内，未合并、未部署。**
运行此脚本不需要启动 Morrow、连接 PostgreSQL 或修改 Railway。
QQ / Telegram 消息平台不在桥接范围。

## 数据流与信任边界

ChatGPT（已连接 GitHub）创建仓库 Issue →
Windows 上的 harness_bridge.py poll/watch →
本地 .morrow-harness/inbox/issue-N-REV.json →
**用户人工确认** →
dsh --profile headless - →
本地 .morrow-harness/results →
**用户审阅结果** → 可选地通过 report 将摘要发回 Issue →
ChatGPT 读取 Issue 继续审查。

- watch/poll **只接收任务，不自动运行模型、终端命令或子代理**。
- run 必须带 --approve-local-run；implement 类型额外要求 --allow-writes。
- Issue **不能指定可执行文件、命令、工作目录或 GitHub API 地址**。
- 仅接收固定私有仓库 Ssrrrnn/claude-telegram-bot 内、
  由 Ssrrrnn 创建、标题以 [Harness] 开头、含精确协议头的开放 Issue；
  GitHub PR、其他作者或不合格 Issue 一律忽略。
- 运行任务前校验工作区确为 checkout，Git 分支不能是 main/master 或 detached。
- 每次 run 都使用 GitHub 认证**重新检查远程 Issue 仍 open、作者和正文修订一致**；
  如果任务在领取后已取消或修改，拒绝执行，先重新 poll。
- 任务正文经标准输入传给 DeepSeek 官方 headless，**不会经 shell 拼接执行**。
- 结果和 Harness stderr **只保存本地忽略目录**，不会自动上传 GitHub。
  私人聊天、密钥、模型推理日志绝对不要粘到 Issue。
- 环境变量 MORROW_HARNESS_TOKEN / GITHUB_TOKEN / GH_TOKEN 不传入
  headless 子进程。注意：如果 Windows 系统里存在其他已登录凭据或
  Git Credential Manager，桥接本身**不能撤销其权限**。
- 任务中的“不写文件”是给 agent 的约束，**不是操作系统级只读隔离**。
  不确认 DSH 沙箱/审批策略前，不建议无人值守自动执行。
- 此工具既不提交代码，也不推送分支、创建/合并 PR、部署 Railway、
  发布 iOS IPA，或向 GitHub 自动回传原始输出。

## 1. 在 Windows 初始化（只做一次）

已有后端位置为 D:\AI-Projects\Bot\claude-telegram-bot。
使用普通 PowerShell，**不要在 Harness 沙箱内操作**：

    $git = 'C:\Program Files\Git\cmd\git.exe'
    $repo = 'D:\AI-Projects\Bot\claude-telegram-bot'
    & $git -C $repo status --short --branch
    & $git -C $repo fetch origin
    & $git -C $repo switch --track -c chore/morrow-harness-github-bridge origin/chore/morrow-harness-github-bridge

仅在工作区干净且本地不存在同名分支时执行 switch。
若已有同名分支、未提交的更改或合并冲突，先停下核对，
**绝不要** reset --hard 或强制推送。

使用现有 Python 3.12：

    $py = 'D:\AI-Projects\Bot\.venvs\claude-telegram-bot\Scripts\python.exe'
    & $py -m unittest test_harness_bridge -v

程序只有 Python 标准库依赖；无需 pip 安装新包。

## 2. 安全连接 GitHub 私有 Issue

方案 A：Windows 已安装并登录 GitHub CLI 时，程序使用 gh auth token
在内存中读取临时认证，**不会打印 Token**。

方案 B：GitHub Fine-grained Personal Access Token：
- 只授权 Ssrrrnn/claude-telegram-bot 这一个仓库。
- 收任务只需 Issues: Read；要上传人工审核的报告需要 Issues: Read and Write。
- 不需要 Contents: Write、Actions、Administration、Secrets 或 Workflow 权限。
- 建议设置短有效期。不将 Token 提交到仓库、聊天、PR、命令历史或文档。

在**同一个 PowerShell 窗口**安全地设置仅当前进程可见的变量：

    $secure = Read-Host 'GitHub fine-grained token' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
      $env:MORROW_HARNESS_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }

该变量只在当前 PowerShell 及子进程有效，关闭窗口后需要重新认证。
请**不要**在截图、日志、聊天里展示凭据值。

## 3. ChatGPT 下发 GitHub Issue 的协议

在后端仓库创建普通开放 Issue，必须以如下格式开头。
标题示例：[Harness] 审查 PR #54 的跨实例防重复投递

    <!-- MORROW-HARNESS-TASK-V1 -->
    Target: backend
    Mode: diagnose

    请读取 AGENTS.md，审查 PR #54 的回执状态机，给出 file:line 证据。
    只读分析，不改代码，不连接生产服务，不提交，不推送。

Target 允许 backend 或 ios；Mode 只允许 diagnose 或 implement。
一个 Issue 就是一项明确任务。Issue 标题、作者、状态、协议格式
不满足条件时，桥接不会接收。

**ChatGPT 已连接 GitHub**，今后可直接由 ChatGPT 创建任务 Issue，
不必逐次粘贴大段文字到 Harness 聊天框。

## 4. 从 GitHub 领取任务（不会调用模型）

    & $py "$repo\harness_bridge.py" poll

查看已领取任务：

    & $py "$repo\harness_bridge.py" show 55

持续领取（此 PowerShell 窗口必须保持运行，Ctrl+C 可停止）：

    & $py "$repo\harness_bridge.py" watch --interval 60

请用真正的 Issue 编号替换 55。程序会打印新增的版本数量，
并把任务记录到 .morrow-harness/inbox/。修改 Issue 正文会创建新版本，
不会覆盖历史版本。所有本地任务与日志均被 .gitignore 排除。

### Windows 一键启动接收器

完成上述 Git 同步后，也可以直接执行开发分支中的 PowerShell 启动脚本：

    & "$repo\start-harness-bridge.ps1"

它使用既有项目 Python 虚拟环境，必要时用隐藏输入读取 GitHub Token，
并持续运行 watch。**不会**调用 Harness、修改代码或自动部署。
如果机器的 PowerShell 策略不允许执行 .ps1，使用上文的 Python watch
命令即可，不需要改变系统执行策略。

## 5. 人工批准后调用 DeepSeek Harness

DeepSeek Harness 官方提供一次性 headless 模式。
检查你电脑能否找到现有 dsh 命令：

    Get-Command dsh -ErrorAction SilentlyContinue

如果已有 dsh：

    & $py "$repo\harness_bridge.py" run 55 --approve-local-run --runner dsh

若已有 Node.js、但没有 dsh 可执行命令，可试官方 npm 启动器：

    & $py "$repo\harness_bridge.py" run 55 --approve-local-run --runner npx

注意：npx 可能联网下载官方 @deepseek-ai/dsh 包；
headless 也可能使用**与现有桌面版不同**的模型配置或 Session，
必须先做一条低风险的只读测试。不要因此擅自修改原有 Harness 配置。

implement 类型额外需要 --allow-writes，并要求本地已在安全开发分支：

    & $py "$repo\harness_bridge.py" run 55 --approve-local-run --allow-writes --runner dsh

运行时还需要当前 PowerShell 会话具有 GitHub Issue 读取认证，
因为桥接会先检查远程任务是否仍有效，不能离线执行已取消的旧任务。

**务必先在 DSH 内确认其 Workspace 沙箱和审批设置。**
headless 是无交互一次性运行，可能不适合需要逐项弹窗批准的开发任务；
不确定时应继续使用桌面端，让 agent 手动读取本地任务文件，
不要启用 headless 实施模式。

执行结果只保存在 .morrow-harness/results/，按 Issue 编号和修订标记命名。
即使 headless 返回 exit 0，也不等于代码、测试或发布已通过，
必须继续进行 Git diff 和 CI 审查。

## 6. 将**人工审阅**的摘要反馈回 GitHub

先在本地创建一个**自行检查过**、不含任何密钥、私人聊天或模型
原始日志的 Markdown 文件，例如：

    $summary = "$repo\.morrow-harness\reviewed-summary.md"
    Set-Content -Path $summary -Value '已完成只读诊断；详情待审核。' -Encoding utf8

只有明确批准上传时，才会发表评论：

    & $py "$repo\harness_bridge.py" report 55 --summary-file $summary --approve-upload

report 需要对指定仓库的 Issues: Write 权限。
它绝不自动上传 headless 的完整 stdout 或 stderr。

## 7. iOS 工作区注意事项

队列统一放在后端 GitHub Issues，iOS 任务设置 Target: ios。

当前版本需要 Windows 上存在名为 companion-ios 的 Git checkout，
通过 --workspace 指定其**真实本地路径**并加 --target ios 领取。
还需要事先让 iOS checkout 忽略 .morrow-harness/，
否则本地任务文件可能出现在 git status 中。不要盲目 git add -A。

该桥只调度任务，不构建 IPA。iOS 发布仍按
companion-ios 的 GitHub Actions → Release → AltStore 流程执行，
且只有收到独立生产发布授权才可合并或发布。

## 8. 安全与故障排除

- 无任务：先确认 Issue 公开状态为 open、作者 Ssrrrnn、
  标题以 [Harness] 开头、协议前四行完全匹配且目标正确。
- HTTP 401/403：检查细粒度 Token 的有效期与 Issues 权限，
  不要将 Token 粘贴到聊天。
- 运行失败：先检查本地结果目录的 .status.json 和 stderr.log，
  仅向外分享人工脱敏后的摘要。
- 工作区当前是 main：run 会拒绝，先新建独立诊断/开发分支。
- Windows 权限错误：停止执行，按原项目安全边界排查；
  不要修改 Windows ACL 或切换全权限模式。
- PR #54 在明确授权前不得合并，也不得部署其 P0 业务修复。
- 监控/轮询并非持续在线云服务：Windows 关机、网络中断、
  PowerShell 关闭或 Token 过期时都不会领取任务。

官方参考：
https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.zh.md
