# ACM Agent

一个以本地网页为主要入口的 ACM/ICPC 训练工作流。它把 Codeforces、洛谷公开做题状态、训练题单、推荐、代码验证、复盘和 Agent 协作统一到本地 SQLite 中。

> A local-first competitive-programming dashboard powered by Python 3.13's standard library. It syncs public Codeforces/Luogu status, recommends practice, manages plans, verifies C++ solutions, and exposes structured workflows for coding agents.

## 特性

- 本地单页仪表盘：今日训练、做题工作台、复盘、设置和多题单管理。
- Codeforces 官方 API 与洛谷公开页面同步；同步失败会保留最后一次成功快照。
- 根据目标 CF rating、题单紧迫度、薄弱专题、复做到期和平台平衡生成可解释推荐。
- 可选 DeepSeek BYOK：确定性候选池内个性化重排、按题持久化流式提示、代码诊断、安全补丁，以及确认式 Markdown 知识归档。
- `accepted`、`attempted`、`local_only` 与“已掌握但未实现”的 `skipped` 严格分离。
- C++17 编译、样例检查、可选 sanitizer 探测、随机对拍和显式 AI 持续对拍，产物全部放入 `.acm/`。
- 原生 HTML/CSS/JavaScript 和 Python 标准库，无 npm、数据库服务或 Python 运行依赖。
- 网页仅监听 `127.0.0.1`，API 使用临时令牌并校验 Host、Origin 和请求体。
- 仓库级 `acm-workflow` skill，让 Agent 通过结构化 API/CLI 操作同一套状态。

## 环境要求

- Python 3.13。
- 现代浏览器。
- 可选：支持 C++17 的 `g++`，仅在编译、样例验证和对拍时需要。
- AI 持续对拍首版仅支持 Windows；需要系统 AppContainer/Job Object 能力。隔离探测失败时会停止，不会降级为普通用户权限执行生成代码。
- 在线同步需要访问 Codeforces 和洛谷的网络连接。

核心运行不需要 `pip install`。

AI 功能同样不需要额外依赖。Windows 用户可在 Dashboard 直接输入 DeepSeek API Key：服务使用当前用户作用域的 Windows DPAPI 加密保存，重启后自动恢复；明文不进入 JSON、SQLite、日志或 API 响应。Linux/macOS 不会退化为明文存储，可使用进程环境变量 `DEEPSEEK_API_KEY`。未显式点击“AI 个性化推荐”或使用 AI 命令时不会产生模型调用费用。

## 使用方式一：网页端 UI

Dashboard 是推荐的日常入口。网页端与 CLI 使用同一份 `.acm/state.db`，页面刷新、关闭浏览器或重启本地服务都不会把 AC、session、题单、AI 对话、对拍 run 等状态拆成另一套数据。

### 启动 Dashboard

Windows 克隆或下载仓库后，可直接双击 `start-acm-web.cmd`，也可以在 PowerShell 中运行：

```powershell
.\acm.ps1 web
```

Linux / macOS：

```bash
chmod +x acm.sh start-acm-web.sh
./start-acm-web.sh
```

任意平台也可以从仓库根目录启动：

```bash
python -m tools.acm_agent web
```

服务默认只监听 `127.0.0.1:8765`；端口被占用时会依次尝试到 `8775`，随后自动打开浏览器。左下角“本地服务”指示灯变为已连接后即可操作。顶部的“同步平台状态”在所有页面都可用，右上角半圆按钮用于切换明暗主题。

### 首次配置

首次打开会显示“连接你的做题记录”：

1. 填写 Codeforces handle 和洛谷数字 UID。
2. 可选填写目标 CF rating；它是推荐难度的优先基准，不是账号当前 rating。
3. 点击“保存并进入仪表盘”。默认会在线验证两个账号，填写错误时不会静默保存。
4. 网络暂时不可用时，可展开“高级选项”选择“离线保存，不验证账号”。在首次成功同步前，推荐依据会明确显示为 `plan_only`，不会把未知平台状态当成未 AC。

之后可在“设置 → 平台账号”修改这些字段。平台 AC 是权威状态；仅存在本地 `.cpp` 文件只会显示为 `local_only`。

### 页面总览

| 页面 | 主要用途 | 常用动作 |
|---|---|---|
| 今日训练 | 同步状态并选择下一题 | 设置推荐模式、来源、题单和数量，开始题目或 Skip |
| 做题工作台 | 管理一次完整 session | 创建源码、验证、AI 对话、持续对拍、结束并复盘 |
| 题单 | 管理多份渐进式或日期题单 | 导入、启停、编辑阶段/题目、补全标签、恢复修订 |
| 复盘 | 查看训练反馈 | 到期复做、薄弱专题、结果/失败分布、撤销 Skip |
| 设置 | 管理本地服务和可选 AI | 修改账号、校验题单、保存 DeepSeek Key、停止服务 |

### 今日训练：同步、推荐和开始题目

1. 点击顶部“同步平台状态”。“数据来源”会分别显示 Codeforces、洛谷的最新快照状态；同步失败时保留最后一次成功数据，不会清空历史。
2. 在“下一组训练”选择推荐模式：
   - “混合模式”同时考虑新题和到期复习；
   - “只做新题”排除所有已 AC 与 active Skip；
   - “只做复习”只返回已经 AC 且到达复做日期的题。
3. 选择来源：“题单与题库均衡”“仅题库”或“仅题单”。展开题单筛选器可限定一份或多份已启用题单，再选择 3/6/9 题。
4. 点击“重新推荐”使用本地确定性排序；配置 DeepSeek 后可点击“AI 个性化”，它只能在合规候选池内重排，失败时自动保留原顺序。
5. 推荐卡会展示“恢复 / 主练 / 上探”位置、难度、有效标签、题单来源、紧迫度、分项得分和推荐理由。可执行：
   - “开始这题”：跳到做题工作台并预填题号；确认后再点击“创建 / 复用源码并开始计时”；
   - “Skip”：仅在未看题解且已经掌握完整正确思路时确认，不创建源码或 attempt，也不计为 AC；
   - “打开题面”：在新标签页打开平台题面。

“推荐依据”横幅会标明当前使用最新同步、缓存快照还是本地题单模式；不要只根据推荐卡是否出现某题来推断 AC。页面下方“进行中的题目”可直接返回 active session。

### 做题工作台：开始、验证和结束 session

#### 1. 开始做题

在“开始做题”中输入 `CF1234A`、`P1000` 或完整题目 URL。点击“创建 / 复用源码并开始计时”后，系统按本地日期创建 `YYYY/M/D/题号.cpp`；同名文件存在时只复用，绝不覆盖。勾选“同时创建暴力和数据生成器”可一并准备本地 `.bf.cpp` 与 `.gen.cpp` 模板。

#### 2. 本地验证

“本地验证”的题号留空时使用当前 active session。可以设置：

- Token 比较或字节精确比较；
- 单个程序超时；
- 随机对拍次数与可复现 seed；
- ASan / UBSan 探测。

将额外样例按同名 `.in/.out` 放入 `.acm/cases/<problem-key>/`。点击“编译并验证”后，“验证输出”会显示编译诊断、逐样例结果、对拍 seed 和失败资产位置。未安装 `g++`、样例缺失或 sanitizer 不受支持都会明确显示，不会伪造通过。

#### 3. AI 持续对拍

这是显式付费且仅 Windows 支持的可选流程。先在“设置 → AI 设置”保存 DeepSeek Key，并确认“正在检查隔离环境”最终通过。展开“AI 持续对拍”后：

1. 勾选总开关，确认授权 AI 准备和安全替换 helper。
2. 选择是否生成 generator、brute、reference，以及是否启用不运行 brute 的 large profile。
3. 按需设置模型、起始 seed、主程序/暴力超时、准备总时限和 helper 生成模式。
4. 选择缓存模式：`Reuse` 复用已认证成果；`Refresh helpers` 保留 contract/recipe 但重做 helper；`Cold` 完全绕过本地生成缓存。
5. 点击“AI 准备并开始持续对拍”。页面会显示隔离检查、契约、各 helper、AI 审查、preflight、安全应用和创建 run 的阶段进度。

开始后可在“持续对拍”卡片中：

- “暂停”：安全停止当前隔离进程树并保留 `next_seed`；
- “继续”：复用已应用 helper，从持久化 seed 接着运行，不再次调用 DeepSeek；
- “结束对拍”：永久完成该 run，之后不能继续，但 helper 与历史仍保留；
- “查看 helper 来源”：查看来源和认证信息；
- “回退 helper”：在哈希仍匹配时恢复应用前版本。

刷新网页会重新附着现有 run；服务异常退出后 run 会标为 `interrupted`，不会在后台自动恢复或继续计费。准备或 preflight 失败时旧 helper 不变，也不会创建 run。

#### 4. AI 做题对话与代码补丁

“AI 做题对话”只在用户主动发送时调用 DeepSeek：

1. 题号默认跟随 active session，也可在多个 active 题目间切换；每题恢复自己的持久会话。
2. 选择“分级提示”“解释疑点”或“代码诊断”。分级提示的 1/2/3 级分别对应反例提问、关键性质、核心转化/伪代码。
3. 点击“读取 / 抓取题面”。自动抓取失败时，展开“题面上下文”粘贴并保存人工题面；人工版本优先，也可恢复自动版本。
4. 输入当前思路或卡点并发送。页面刷新后仍能恢复消息；切题不会把迟到的流式回答串到另一题。
5. 需要修改源码时点击“生成AI修改代码”。确认完整代码和说明注释后，点击“确认应用并验证”；系统先备份原文件，再写入并执行本地验证。验证失败不会自动回滚，可在文件未被再次改动时点击“回退补丁”。
6. “清除本题对话”会归档旧会话并创建空会话，不会删除 token、提示等级、补丁和调用审计；回答生成期间不能清除。

#### 5. 结束与复盘

在“结束与复盘”填写题号、结果、独立思考分钟数、最高提示等级、失败类型和备注，再点击“结束 session”。这里记录的是本次训练事实：本地通过不能自动替代平台 AC；手动选择 AC 则表示你明确确认已通过。

可选的 Markdown 总结默认关闭。启用后先选择或注册目标 `.md`，选择已保存、自动推断、自定义或 AI 判断 schema。Session 会先独立关闭；随后才生成可编辑总结。编辑 Markdown 后必须点击“刷新预览”，确认安全预览无误再“确认写入”；取消或生成失败不会影响已保存的 attempt，目标文件外部变化会阻止覆盖。

### 题单：导入、编辑和标签管理

- “导入 JSON”会先显示校验与差异预览；同 `plan_id` 替换必须额外勾选确认。“下载 v2 模板”可生成可编辑样板。
- 左栏选择题单，右栏可启停题单、编辑标题/说明、增加或调整阶段、题目、顺序、解锁日期和截止日期。修改会形成受版本保护的托管修订，并立即影响下一次推荐。
- 内置题单本体保持只读；网页修改保存在 `.acm/plans/` 覆盖层。“恢复内置版本”只移除覆盖，不删除训练历史。
- “补全标签”只填补缺失标签；“清理标签”会给出可编辑的完整标签预览。两者都必须确认后才写入。若其他标签页已修改题单，页面会提示版本冲突，此时重新加载并重新预览，不会覆盖新版本。
- 删除题目或题单只解除题单关联，不删除 AC、Skip、session、attempt、复做和失败记录。

### 复盘：复做、薄弱项和历史

点击“刷新周复盘”后可查看近七天完成数、平均提示等级、到期复做、薄弱专题、结果分布和失败类型。底部可以：

- 在“已 Skip 题目”中撤销 Skip，使题目重新进入推荐池；
- 按全部、AC、WA、TLE、RE、ABANDONED 或 active 筛选最近 session；
- 根据“到期复做”列表，在今日训练选择“只做复习”生成复习推荐。

薄弱专题使用 attempt 关闭时冻结的有效标签，不会因之后修改题单标签而重写历史。

### 设置：账号、本地服务和 DeepSeek

- “平台账号”修改 Codeforces handle、洛谷 UID 和目标 rating；离线保存会明确跳过账号验证。
- “本地服务”显示版本、监听地址和 SQLite 状态，可校验内置题单；“停止本地服务”会让当前 Dashboard 断开，需要重新运行启动命令才能恢复。
- Windows 的“AI 设置”可保存、替换或清除 DeepSeek API Key。保存后输入框立即清空；Key 使用当前 Windows 用户的 DPAPI 加密，不进入浏览器存储、JSON、SQLite、日志或接口响应。
- 推荐、做题对话、Markdown 总结可以分别选择 Flash/Pro；thinking 与推理强度仅影响对应功能。保存后建议点击“测试连接”确认配置。

### 推荐的网页端训练闭环

```text
同步平台状态
  → 选择模式/来源/题单并生成推荐
  → 开始题目并编辑本地源码
  → 编译、样例验证和可选对拍
  → 按需请求分级提示或预览 AI 补丁
  → 记录结果、用时、提示等级和失败类型
  → 在复盘页处理到期复做与薄弱专题
```

## 使用方式二：命令行 CLI

CLI 适合脚本、终端和 Agent 自动化，行为与 Dashboard 相同。Windows 使用 `.\acm.ps1`，Linux/macOS 使用 `./acm.sh`；也可以统一调用 `python -m tools.acm_agent`。

首次使用：

```powershell
.\acm.ps1 init
```

一次常见训练流程：

```powershell
.\acm.ps1 sync --json
.\acm.ps1 next --count 3 --mode mixed --source-mode balanced --json
.\acm.ps1 start CF1234A --json
.\acm.ps1 verify CF1234A --json
.\acm.ps1 close CF1234A
.\acm.ps1 review week --json
```

主要命令组：

```text
acm init
acm sync [--platform codeforces|luogu|all]
acm status
acm next [--count N] [--mode mixed|new|review] [--source-mode ...] [--ai]
acm start <题号或 URL> [--with-stress]
acm verify [题号] [--debug] [--exact] [--ai-stress]
acm stress status|stop|resume|artifacts|revert
acm ask <题号> [--mode hint|explain|review] [--hint-level 1|2|3]
acm patch preview|apply|revert
acm close <题号>
acm skip|unskip|skipped
acm review week
acm plan list|import|export|enable|disable|delete|template|check
acm plan tags preview|apply
acm knowledge templates|targets|inspect|preview|refresh|apply|revert
acm ai status|test|settings
acm context fetch|show|set <题号>
```

需要供脚本或 Agent 消费时优先加 `--json`，不要解析面向人的终端文本。完整参数可运行：

```powershell
python -m tools.acm_agent --help
python -m tools.acm_agent <子命令> --help
```

CLI 也可以启动同一个 Dashboard：`.\acm.ps1 web`。Linux/macOS 示例中的 `.\acm.ps1` 换成 `./acm.sh`。

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
6. 结束时记录结果、独立思考时间、提示等级、失败类型和备注；实际提示等级会与 AI 历史最高等级取最大值。可选生成 Markdown 总结，先检查并刷新安全预览，再显式确认写入。
7. 在复盘页查看到期复做、近七天结果、薄弱专题与 Skip 列表。

对应 CLI：

```powershell
.\acm.ps1 sync --json
.\acm.ps1 next --count 3 --mode mixed --json
.\acm.ps1 start CF1234A --with-stress --json
.\acm.ps1 verify CF1234A --json
.\acm.ps1 verify CF1234A --ai-stress
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

## Markdown 智能总结

“结束与复盘”中的 Markdown 总结默认关闭。Session 会先独立关闭；只有用户勾选后才会调用 DeepSeek，并根据冻结标签、题面、最终源码、复盘字段和当前未清除的 AI 对话生成知识卡。用户曾点击“清除本题对话”的旧 conversation 仍保留审计，但不会再次发送给模型。总结失败不会回滚 attempt，也不会写入目标文件。

目标可以是固定本地磁盘上的任意 `.md`。发行包根目录随附 `algorithms.md` 和 `tricks.md`；首次读取目标列表时，它们会分别按固定 Algorithms/Tricks schema 自动注册并启用为已保存目标，因此 Dashboard 的 schema 选择器不再重复列出这两个 preset。注册过程不会改写模板内容。其他文件首次注册分为“只读检查 schema”和“确认保存目标”两步；可从已有 H1/H2 与字段标签推断 `summary-schema-v1`、提供自定义 schema，或让 DeepSeek 同时判断 schema。schema 只是字段、标题层级、布局和间距的声明式 JSON，不执行模板脚本。

写入流程固定为：`DeepSeek 结构化生成 → 本地确定性渲染 → 可编辑 Markdown 与安全预览 → 刷新预览 → 确认 apply`。Dashboard 不展示 unified diff；服务端仍保存基线 SHA-256、候选 bytes 和内部 diff 用于审计与安全应用。若目标中存在 `Source` 题号完全相同的唯一条目，只有该旧条目会与本次上下文一并发送给 DeepSeek，由模型语义合并后替换原条目；仅标题相同或模糊相似时按新条目处理，不再要求用户选择。apply 前再次检查路径、UTF-8/BOM、1 MiB 上限和基线，备份到 `.acm/markdown-backups/` 后同目录原子替换。目标被外部编辑时返回 HTTP 409；revert 仅在文件仍等于已应用版本时可用。本机路径、账号、API Key 和整份知识库不会发送给 DeepSeek。

CLI 示例：

```powershell
.\acm.ps1 knowledge templates --json
.\acm.ps1 knowledge inspect D:\notes\algorithms.md --preset algorithms-v1 --json
.\acm.ps1 knowledge targets add D:\notes\algorithms.md --preset algorithms-v1 --json
.\acm.ps1 knowledge preview 42 <target-id> --json
.\acm.ps1 knowledge apply <proposal-id> --expected-revision 1 --json
```

## 验证与对拍

默认编译参数是 `g++ -std=c++17 -O2 -Wall -Wextra`。`.in/.out` 文件按文件名配对，默认按 token 比较，`--exact` 改为字节比较。

如果同一天目录存在 `<ID>.bf.cpp` 和 `<ID>.gen.cpp`，验证器会运行随机对拍。失败输入、两份输出、seed 和命令保存在 `.acm/failures/`。`--debug` 会先探测当前编译器是否支持 ASan/UBSan；不支持时会明确跳过，不会误报 sanitizer 已通过。

### AI 持续对拍（显式可选）

“本地验证”中的 AI 持续对拍默认关闭。启用后，DeepSeek 只接收公开/人工题面与对拍契约，不接收账号、API Key、本机路径或用户主解源码。它独立生成 generator 与小规模 brute，并按 `Codeforces 官方题解 / 洛谷题解 → 博客园 → CSDN → DeepSeek 生成` 查找 reference。来源候选由固定 HTTPS 白名单抓取器按题号确定性筛选，不再额外调用模型选源。洛谷完整 C++ 必须先通过源码安全检查、3 秒静态编译和快速语义审查；整个洛谷 AI 审查阶段共享 28 秒预算，关闭 thinking、禁止重试、输出上限 512 token，并只发送压缩题面、契约和不超过 32000 字符的源码。审查仍覆盖编译缺失、数组容量、分支、下标与输出协议；被拒绝的候选不会作为 reference，通过后保存结构化结果且不由 AI 重写。模型不能请求任意 URL，也不会抓取普通用户提交。

准备过程使用一个从提交到成功/失败落库的单调时钟 deadline，默认 600 秒，可在 Dashboard 或 CLI 设置为 60–1800 秒。默认档在 480 秒强制停止 provider，480–590 秒只用于编译、完整 preflight、应用和持久化，最后 10 秒用于 shutdown 与清理；provider 不得借用本地门禁预算。单次 non-thinking、thinking、audit 分别不超过 120、180、50 秒，thinking 至少保留 30 秒启动窗口，普通生成和 audit 至少保留 8 秒。每次 setup 的累计 provider usage 还有 100000 tokens 成功硬上限：越界立即 fail-closed，后续请求不再启动。默认 `hybrid` fast-first，只在获得明确机器诊断后启用困难修复 thinking；`fast` 完全关闭 thinking，`full_thinking` 保留更高推理预算，900 秒可作为显式慢速档。连接、响应读取、keep-alive、网络重试和 JSON 恢复共享同一绝对 deadline，不会因重试重新获得完整等待窗口。

contract schema v3 结构化保存输入语法、约束、逐字题面证据和可计算 coverage obligation。generator blueprint 与 generator、brute、reference、独立 validator 并行准备；validator 只读题面和 contract，不读取 generator 或用户主解。可信本地 harness 独占 argv、seed、profile-v2、manifest、SHA、records、确定性与资源限制，模型只生成题目相关 adapter。所有 helper 依次经过源码安全检查、本地编译、capability/确定性/smoke、AI audit、定点修复以及官方样例、16 个 small、上下界和 large 完整 preflight；generator 最多修复两次，其他角色最多一次。只有 exact-trio 与 validator 联合认证后才应用 helper。任一环节失败都不会修改旧 helper，也不会创建 run。

schema v12 使用 SQLite 的唯一 `stress_setup` 槽阻止 Dashboard、CLI 或多个进程重复准备，并保存角色 candidate/proof、bundle certification、cache alias 和结构化样例。缓存身份拆成模型/prompt/题目语义的 generation identity，以及源码/compiler/sandbox/样例/协议/门禁的 certification identity；每个成功角色立即落库，兄弟角色失败不会丢失成果，但重新组合后仍必须完成 exact-trio 联合 preflight。`cache_mode=reuse` 正常复用，`refresh_helpers` 复用 contract/recipe 后重生成 helper，`cold` 绕过全部本地读取；旧 `force_regenerate=true` 映射为 `cold`。失败 cold run 不覆盖或失效已有正确 alias，完全相同的 warm setup 保持 0 provider requests、0 tokens。

DeepSeek provider cache 只作为 best-effort 优化：公共 prompt 固定为 system → canonical 题面 → contract assistant JSON → 角色任务，以便完整前缀匹配；实际正确性和 warm 零请求由本地缓存保证。参见官方 [Context Caching](https://api-docs.deepseek.com/guides/kv_cache) 与 [JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 说明。

Dashboard 的准备按钮按九阶段显示进度：隔离检查、契约、generator、brute、reference、AI 静态复核、调试构建与逐 case 预验、安全替换、创建 run。预验失败会保留显示 artifact、profile、case kind 与 seed（可用时），并明确提示旧 helper 未修改、run 未创建。

生成或抓取的程序只在无网络 capability 的 Windows AppContainer 内运行，并由 Job Object 限制进程树、512 MiB 内存及 CPU/墙钟时间。项目会从随仓库发布的可信 C++ 源码按需构建本地 launcher；能力探测或构建失败会安全停止。新任务先验证官方样例，再运行恰好达到合法下界的 small 和恰好达到合法上界的 large，此后按 4:1 调度 small/large。small 输入保持在人能检查、brute 能在 5 秒内通过的规模，并执行主解/brute/reference 三方验证；large 接近题目极限，只执行主解/reference，不运行 brute。small 保持 2 MiB 输入输出限制，large 允许 32 MiB 输入和 16 MiB 程序输出。large 的主解/reference 分歧直接记录为 mismatch；对应固定 `<题号>_brute.out` 会明确标记该 profile 未运行 brute。

运行会持续到发现反例、oracle 冲突、执行故障或用户暂停。Dashboard 的“暂停”保留 `next_seed` 供稍后继续，“结束对拍”会在隔离进程树退出后永久完成该 run 并释放唯一运行锁，但保留 helper 与历史记录。刷新页面后会重新附着；服务重启将运行标为 `interrupted`，不会自动执行。`stopped`、`interrupted`、`mismatch`、`oracle_conflict` 和 `fault` 均可从持久化的 `next_seed` 继续；继续时复用并重新编译已经应用的 helper，不重新调用 DeepSeek，也不会重新生成 helper。累计 case 数不会丢失，速度仅按本轮继续后的增量和本轮时间计算。CLI 前台命令会持续等待，`Ctrl+C` 写入停止请求并等待当前 AppContainer 进程树退出：

```powershell
.\acm.ps1 verify CF1234A --ai-stress
.\acm.ps1 verify CF1234A --ai-stress --no-large
.\acm.ps1 verify CF1234A --ai-stress --prepare-timeout 300
.\acm.ps1 verify CF1234A --ai-stress --generation-mode hybrid
.\acm.ps1 verify CF1234A --ai-stress --prepare-timeout 900 --force-regenerate
.\acm.ps1 stress status
.\acm.ps1 stress stop <run-id>
.\acm.ps1 stress resume <run-id>
.\acm.ps1 stress artifacts <bundle-id>
.\acm.ps1 stress revert <bundle-id>
```

失败资产保存在 `.acm/failures/<problem>/<timestamp-seed>/`，包含输入、三方输出/错误、退出状态、源码哈希、reference 来源、profile、seed 和复现信息。只要当前主解与 reference 输出不同，还会在当前题目源码目录原子写入 `<题号>_input.in`、`<题号>_current.out`、`<题号>_brute.out` 和 `<题号>_reference.out`；固定文件名始终代表最近一次差异，完整历史仍保存在 `.acm/failures/`。Dashboard 的 helper 来源只展示可打开的外部链接。真实网络抓取与原生隔离 smoke test 默认不进入离线 CI；分别设置 `RUN_STRESS_NETWORK_SMOKE=1` 与 `RUN_APPCONTAINER_SMOKE=1` 后执行对应测试。

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
├── markdown-backups/ # 用户确认写入 Markdown 前的知识库备份
└── web-runtime.json  # 当前本地服务端口、PID 和临时令牌
```

应用不保存洛谷 cookie、密码或明文 DeepSeek API Key。AI 推荐只发送最多 90 天/50 次尝试的题号、平台、难度、日期、结果、耗时、提示等级、失败类型、冻结标签、薄弱度和目标 rating，不发送账号、notes、聊天、源码或路径。工作台对话会发送当前题面、有效标签、源码、attempt 与最近对话。用户显式请求 Markdown 总结时，会发送该 closed attempt 的题面、源码、复盘字段和未清除对话、必要的脱敏 schema 片段；仅在 `Source` 题号完全相同时额外发送对应单条旧知识卡用于 AI 合并，不发送目标路径、整份知识库或已清除对话。相关界面会在发送前说明边界。首次公开仓库不包含任何账号、AC、Skip、session、源码答案或平台快照。不要将 `.acm/` 提交到 Git。

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
