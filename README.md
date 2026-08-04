# ACM Agent

一个以本地网页为主要入口的 ACM/ICPC 训练工作流。它把 Codeforces、洛谷公开做题状态、训练题单、推荐、代码验证、复盘和 Agent 协作统一到本地 SQLite 中。

> A local-first competitive-programming dashboard powered by Python 3.13's standard library. It syncs public Codeforces/Luogu status, recommends practice, manages plans, verifies C++ solutions, and exposes structured workflows for coding agents.

## 特性

- 本地单页仪表盘：今日训练、做题工作台、复盘、设置和多题单管理。
- Codeforces 官方 API 与洛谷公开页面同步；同步失败会保留最后一次成功快照。
- 根据目标 CF rating、题单紧迫度、薄弱专题、复做到期和平台平衡生成可解释推荐。
- 可选 DeepSeek BYOK：确定性候选池内个性化重排、按题持久化流式提示、代码诊断和确认式安全补丁。
- `accepted`、`attempted`、`local_only` 与“已掌握但未实现”的 `skipped` 严格分离。
- C++17 编译、样例检查、可选 sanitizer 探测和随机对拍，产物全部放入 `.acm/`。
- 原生 HTML/CSS/JavaScript 和 Python 标准库，无 npm、数据库服务或 Python 运行依赖。
- 网页仅监听 `127.0.0.1`，API 使用临时令牌并校验 Host、Origin 和请求体。
- 仓库级 `acm-workflow` skill，让 Agent 通过结构化 API/CLI 操作同一套状态。

## 环境要求

- Python 3.13。
- 现代浏览器。
- 可选：支持 C++17 的 `g++`，仅在编译、样例验证和对拍时需要。
- 在线同步需要访问 Codeforces 和洛谷的网络连接。

核心运行不需要 `pip install`。

AI 功能同样不需要额外依赖。Windows 用户可在 Dashboard 直接输入 DeepSeek API Key：服务使用当前用户作用域的 Windows DPAPI 加密保存，重启后自动恢复；明文不进入 JSON、SQLite、日志或 API 响应。Linux/macOS 不会退化为明文存储，可使用进程环境变量 `DEEPSEEK_API_KEY`。未显式点击“AI 个性化推荐”或使用 AI 命令时不会产生模型调用费用。

## 快速开始

### Windows

克隆或下载仓库后，双击 `start-acm-web.cmd`。也可以在 PowerShell 中运行：

```powershell
.\acm.ps1 web
```

### Linux / macOS

```bash
chmod +x acm.sh start-acm-web.sh
./start-acm-web.sh
```

也可以在任意平台从仓库根目录启动：

```bash
python -m tools.acm_agent web
```

服务默认监听 `127.0.0.1:8765`；端口被占用时会依次尝试到 `8775`，随后自动打开浏览器。

首次打开时填写 Codeforces handle、洛谷数字 UID 和可选的目标 CF rating。默认会在线验证账号；高级选项允许离线保存，但在成功同步前推荐会明确标记为 `plan_only`。

## 推荐是如何生成的

推荐难度首先使用你设置的目标 CF rating。未设置时依次回退到当前 CF rating、最近 30 道不同 CF AC 的 rating 中位数，最后使用 1600。

默认的三个位置为：

- 恢复：目标 rating 的 `-200~-100`。
- 主练：目标 rating 的 `0~+100`。
- 上探：目标 rating 的 `+200~+300`。

每道候选题综合题单紧迫度、专题薄弱分、难度距离、复做到期、近期重复和平台比例打分，并在推荐卡中展示分项原因。默认 `balanced` 来源模式下，三道题最多两道来自题单，其余由平台题库补充；也可以选择仅题库或仅题单。

新题推荐排除所有已 AC 和 active Skip 的题。复习模式只选择到期的 AC 题。文件存在只表示 `local_only`，绝不会被当作 AC。

### 可选 DeepSeek 个性化重排

Windows Dashboard 的“设置 → AI 设置”支持输入、替换和清除 API Key。加密凭据保存在忽略提交的 `.acm/deepseek-key.dpapi`，仅相同 Windows 登录用户通常可以在同一台电脑上解密；环境变量仍可作为 CLI 和非 Windows 平台的回退。

`next --ai` 会让确定性引擎先生成 12–24 道合规候选，再由 DeepSeek 在候选池内重排。模型不能恢复 AC/Skip 题、绕过题单/来源/复做到期约束或创造题号；任何网络、鉴权、限流、非法 JSON 或候选校验失败都会保留确定性结果并返回结构化 fallback。

推荐和对话默认使用 `deepseek-v4-flash`，可分别切换为 `deepseek-v4-pro`。推荐关闭 thinking；对话与补丁可开启 thinking 并选择 `high` 或 `max`，但 `reasoning_content` 永不展示或保存。

### 按题保存 AI 对话

AI 工作台以 active attempt 和题目为会话边界。多道题同时处于 active 状态时，在题号输入框切换题目会读取各自的持久对话、题面和补丁状态；页面刷新后仍可继续。切题会中断旧题正在进行的 SSE，并使用题目键与异步 epoch 丢弃迟到响应，避免回答、题面或候选代码串到另一道题。

“清除本题对话”不会物理删除审计事实：服务会在一个 SQLite 事务中归档旧 conversation，并为同一 attempt 创建新的空 conversation。旧消息不再发送给 DeepSeek，但历史最高提示等级、token usage、AI run 和补丁关联仍被保留。存在 pending/streaming 调用时清除返回 HTTP 409，等待回答结束后再操作。

相关接口：

```text
POST /api/ai/conversations
GET  /api/ai/conversations/{id}
POST /api/ai/conversations/{id}/messages
POST /api/ai/conversations/{id}/clear
```

## 一次完整训练

1. 在“今日训练”同步平台状态并生成下一组训练。
2. 从推荐卡开始题目，或在“做题工作台”输入题号/URL。
3. 系统在本地时区创建 `YYYY/M/D/题号.cpp`；已有同名文件会直接复用，不覆盖。
4. 将样例放入 `.acm/cases/<problem-key>/`，使用网页或 CLI 验证。
5. 可在 AI 工作台请求 1–3 级提示、解释疑点或 4 级代码诊断；切换题号会恢复该 active attempt 的独立对话，“清除本题对话”会归档旧会话并保留提示等级与调用审计；补丁会先以 C++ 语法高亮展示带错误说明注释的修改后完整代码，再由用户确认应用。
6. 结束时记录结果、独立思考时间、提示等级、失败类型和备注；实际提示等级会与 AI 历史最高等级取最大值。
7. 在复盘页查看到期复做、近七天结果、薄弱专题与 Skip 列表。

对应 CLI：

```powershell
.\acm.ps1 sync --json
.\acm.ps1 next --count 3 --mode mixed --json
.\acm.ps1 start CF1234A --with-stress --json
.\acm.ps1 verify CF1234A --json
.\acm.ps1 close CF1234A
.\acm.ps1 review week --json
```

Linux/macOS 将 `.\acm.ps1` 换成 `./acm.sh`。

## DeepSeek BYOK

PowerShell 示例：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
.\acm.ps1 ai status
.\acm.ps1 ai test
.\acm.ps1 next --ai --count 3 --json
.\acm.ps1 ask CF1234A "只提示关键性质" --mode hint --hint-level 2
```

API endpoint 固定为 DeepSeek 官方 Chat Completions 地址，模型名有 allowlist，不接受自定义 URL。题面首次对话时自动抓取并缓存 30 天；人工粘贴版本优先，自动刷新不会覆盖。模型生成的完整候选源码必须在实质修复点附近加入中文 C++ 注释，说明原错误和修复原因；Dashboard 只显示修改后的完整代码并做本地语法高亮，不呈现 unified diff。服务端仍生成并保存 diff 用于审计和安全应用，应用时检查受管日期目录、`.cpp` 后缀、基线哈希、NUL 与大小限制；原文件备份到 `.acm/ai-backups/`。验证失败不会自动回滚，只有文件仍等于 AI 应用版本时才能安全 revert。

## 验证与对拍

默认编译参数是 `g++ -std=c++17 -O2 -Wall -Wextra`。`.in/.out` 文件按文件名配对，默认按 token 比较，`--exact` 改为字节比较。

如果同一天目录存在 `<ID>.bf.cpp` 和 `<ID>.gen.cpp`，验证器会运行随机对拍。失败输入、两份输出、seed 和命令保存在 `.acm/failures/`。`--debug` 会先探测当前编译器是否支持 ASan/UBSan；不支持时会明确跳过，不会误报 sanitizer 已通过。

## Skip：已掌握但未实现

当你在未看题解的情况下已经有完整正确思路，并认为没有实现价值时，可以在推荐卡选择 Skip。

- Skip 不创建源码、session、attempt 或复做任务。
- Skip 会从新题推荐中排除，并计入 progressive 题单进度。
- Skip 不是 AC，也不会满足题单中的 AC 替换条件。
- Skip 可在复盘页或 `unskip` 命令中撤销。
- 已 AC 或存在 active session 的题不能 Skip。

## 多题单

题单页支持 JSON 导入、导出、启停、删除、修订恢复和按阶段编辑。Codeforces/洛谷数量及比例从真实任务实时计算，不写入题单文件。

仓库内置完整的[渐进式数据结构题单](training/data-structures-30d/README.md)，没有固定日期。完成当前阶段后自动解锁下一阶段；内置文件只读，网页编辑会在 `.acm/plans/` 创建托管覆盖副本。

```powershell
.\acm.ps1 plan list --json
.\acm.ps1 plan template --output .\my-plan.json
.\acm.ps1 plan import .\my-plan.json --json
.\acm.ps1 plan check --json
```

题目标签可以在网页中手工编辑，也可使用“补全标签”从 Codeforces 官方题库和洛谷公开题面生成预览，确认后才写入新修订。

## Agent 协作

仓库包含 `.agents/skills/acm-workflow`。支持该格式的 Agent 应优先读取网页 JSON API；服务未运行时退回 CLI `--json`。该 skill 强制以下边界：

- 不根据文件名、网页文字或对话猜测 AC。
- 默认盲解，按反例提问、性质提示、核心转化、伪代码、完整代码记录 0–4 级提示。
- 只有用户明确表达“思路完整且无需实现”时才能记录 Skip。
- `close` 只生成归档候选；知识归档是可选外部能力，缺失时不会直接编辑知识索引。

## 本地数据与隐私

所有个人数据位于被忽略的 `.acm/`：

```text
.acm/
├── config.json       # 平台账号与目标 rating
├── state.db          # SQLite 状态库
├── cache/            # 平台缓存
├── plans/            # 导入题单与内置覆盖
├── cases/            # 本地样例
├── build/            # 编译产物
├── failures/         # 对拍失败资产
├── reports/          # 复盘/归档候选
├── ai-backups/       # 用户确认应用 AI 补丁前的源码备份
└── web-runtime.json  # 当前本地服务端口、PID 和临时令牌
```

应用不保存洛谷 cookie、密码或 DeepSeek API Key。AI 推荐只发送最多 90 天/50 次尝试的题号、平台、难度、日期、结果、耗时、提示等级、失败类型、冻结标签、薄弱度和目标 rating，不发送账号、notes、聊天、源码或路径。工作台对话会发送当前题面、有效标签、源码、attempt 与最近对话，并在首次发送前提示。首次公开仓库不包含任何账号、AC、Skip、session、源码答案或平台快照。不要将 `.acm/` 提交到 Git。

## CLI 概览

```text
acm init
acm sync [--platform codeforces|luogu|all]
acm status
acm next [--count N] [--mode mixed|new|review] [--ai] [--model ...]
acm ai status|test|settings
acm context fetch|show|set <题号>
acm ask <题号> [--mode hint|explain|review] [--hint-level 1|2|3]
acm patch preview|apply|revert
acm start <题号或 URL> [--with-stress]
acm verify [题号] [--debug] [--exact]
acm close <题号>
acm skip|unskip|skipped
acm review week
acm plan list|import|export|enable|disable|delete|template|check
```

命令的完整参数可用 `python -m tools.acm_agent --help` 和各子命令 `--help` 查看。所有供 Agent 消费的状态命令都支持结构化 JSON。

## 开发与测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q tools tests
python -m tools.acm_agent plan check --json
```

测试使用固定脱敏夹具，不依赖实时平台。GitHub Actions 会在 Python 3.13 的 Windows 和 Ubuntu 环境执行同样的检查。

## 平台说明

Codeforces 同步使用其官方匿名 API。洛谷没有为本项目使用的公开页面接口提供稳定性承诺，因此解析器包含结构守卫，页面变化时会保留最后一次成功状态并报告失败。本项目与 Codeforces、洛谷均无隶属或官方合作关系。

## License

[MIT](LICENSE)
