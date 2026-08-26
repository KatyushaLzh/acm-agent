# ACM Agent

Python 3.13 标准库驱动的本地训练系统。网页是主要入口，CLI 保留为自动化和无浏览器环境的兼容层。SQLite 是状态事实源；已有 `YYYY/M/D/*.cpp` 只会导入为 `local_only`，不会据此推断 AC。

当前发布版本：`6.0.0`。

## 打开本地网页

双击仓库根目录的 `start-acm-web.cmd`，服务会仅监听本机回环地址并自动打开浏览器。也可以在 PowerShell 中运行：

```powershell
.\acm.ps1 web
```

默认使用 `127.0.0.1:8765`；端口占用时会在 `8765`–`8775` 中选择空闲端口。网页提供首次配置、平台同步、今日推荐、做题验证、结束复盘、周复盘，以及多题单导入和编辑。

访问令牌只写入 `.acm/web-runtime.json`，页面读取后会立即从地址栏移除；Windows 下该文件会移除继承 ACL，仅保留当前进程用户与 LocalSystem。若无法收紧权限，服务会拒绝启动。所有 API 同时校验令牌、`Host`、`Origin`、JSON 类型和请求体大小。

## CLI 兼容入口

```powershell
.\acm.ps1 init
.\acm.ps1 sync
.\acm.ps1 status
.\acm.ps1 next
.\acm.ps1 ai status
.\acm.ps1 next --ai --ai-mode gap_fill
.\acm.ps1 next --ai --ai-mode specialization
.\acm.ps1 plan list
.\acm.ps1 plan import .\my-plan.json
```

网页首次保存 Codeforces handle 和洛谷数字 UID 时会先验证并持久化账号，然后直接进入主界面。后台同步卡持续显示平台、阶段、页数/题数、已用时间和主功能可用状态；刷新或重新打开页面会从服务端恢复仍在运行的 job。洛谷公开 AC 会先以短事务写入，完整目录随后以 4 路有界并发抓取并在全部分页通过结构守卫后原子提交；失败时保留上一次完整目录并报告 partial，不会用逐题请求补偿整个失败目录。标签补抓同样使用有界并发，网络或结构失败项按 24 小时至 7 天指数退避；公开题面明确返回空标签时记录为 `tagless`，不进入退避且不把同步降级为 partial。目录失败时本轮最多补抓 10 题。相同账号会复用正在运行的初始化 job，24 小时内的新鲜全局目录不会重复全量抓取。CLI `init` 仍等待同步完成并在 stderr 展示阶段进度。只有需要先离线建库时才使用 `--skip-validate`；该选项不发起同步或标签请求。

## 一次做题闭环

```powershell
.\acm.ps1 next --count 3 --mode mixed
.\acm.ps1 start CF1234A --with-stress
.\acm.ps1 verify CF1234A
.\acm.ps1 close CF1234A
.\acm.ps1 review week
```

样例放在 `.acm/cases/CF1234A/name.in` 与同名 `.out`。默认按 token 比较；`verify --exact` 改为字节比较。未显式选择文件时，存在同目录的 `CF1234A.bf.cpp` 和 `CF1234A.gen.cpp` 才会执行有限本地对拍；Dashboard 也可通过原生选择器指定已有的用户程序、参考程序和生成器。可用 `--stress-iterations` 与 `--seed` 控制次数和种子。输出不一致时，最新 `.stress.in`、`.reference.out` 与 `.user.out` 优先发布到源码旁；生成器、运行时与输出上限错误的详细诊断写入 `.acm/failures/`，源码旁发布失败时也回退到该目录。

所有读取状态的命令都支持 `--json`。推荐输出包含数据新鲜度、位置、总分、每项分数和选择原因；平台同步失败不会删除最后一次成功快照。

网页与 CLI 直接调用同一业务服务层，不通过子进程互相调用。受支持的旧版 `.acm/state.db` 会自动升级到当前 schema v24。涉及破坏性重建的选定迁移会先通过 SQLite backup API 创建并校验相邻的 `state.db.v<source>.bak`；v16→v17 退役持久化 stress 状态只是其中一个历史迁移，当前 v23→v24 迁移同样先保留并校验 v23 快照。已存在但不可读、版本不符或与当前源库不匹配的备份会使迁移失败关闭，不会被覆盖。

## DeepSeek BYOK 与 AI 工作台

AI 功能是显式 opt-in：普通 `next` 和原有工作流不会因为检测到密钥而调用模型。Dashboard 的“AI 设置”可用同一组显示名称、HTTPS Base URL、API Key 字段保存内置 DeepSeek 或 OpenAI-compatible 中转站。Windows 使用当前用户 DPAPI，macOS 使用系统 Keychain，Linux 使用 Freedesktop Secret Service；密钥不会进入 JSON 配置、SQLite、日志、浏览器存储或 API 响应。系统安全存储缺失或锁定时保存会失败关闭，不会退化为明文或文件型 keyring；显式环境变量仍可作为当前进程的临时凭据来源：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
.\acm.ps1 ai status
.\acm.ps1 ai test
.\acm.ps1 next --ai --ai-mode gap_fill --count 3
.\acm.ps1 next --ai --ai-mode specialization --count 3
```

网页可随时替换或清除系统安全凭据。若同时存在安全存储凭据和环境变量，安全存储凭据优先；清除后仍可回退到环境变量。Linux 若提示 Secret Service 不可用，请在当前桌面会话中确认 D-Bus 与系统密钥环已经运行并解锁；程序不会调用 `sudo`、启动 daemon 或代替用户解锁。

内置 DeepSeek 路由的推荐和对话默认使用 `deepseek-v4-flash`，可分别改为 `deepseek-v4-pro`；通过 live conformance 的托管 OpenAI-compatible 连接也可为六个任务 profile 独立选模。`gap_fill`（查漏补缺）从不同 AC 题数较少的知识板块选择，`specialization`（专项强化）从不同 AC 题数较多的知识板块选择。两者始终先由确定性引擎生成合规候选，保留当前题数、训练模式、来源模式和题单过滤；网络、鉴权、余额、限流、非法 JSON 或越权题号会优先触发同模式的 hybrid/确定性回退。只有完整本地业务校验通过时回退结果才可用，否则返回结构化 `unavailable`，不会把空或越界集合伪装成成功。

普通推荐与 AI 推荐共用三段难度分布，按三题一组循环：当前 CF rating +100、Codeforces 与洛谷各最近 50 道不同已解决题目的 CF 等效难度合并平均值、目标 CF rating。AI 最终重排允许每个位置在本槽目标难度正负 100 内波动；范围内“更接近目标”只作为软排序偏好，不再要求必须选择声明板块内绝对最近的候选。候选构造和模式内回退仍使用同一槽位目标排序。响应的 `difficulty_profile` 会给出三个目标与近期样本覆盖情况。

做题工作台支持按题目持久化的流式对话、1–3 级提示、4 级代码诊断，以及“生成带错误说明注释的完整候选源码 → C++ 语法高亮预览 → 用户确认 → 备份并验证”的补丁流程。预览区只显示修改后的完整代码，不展示 unified diff；diff 仍由服务端生成并保留用于审计和安全应用。切换题号时会恢复该 active attempt 对应的会话，不会把另一道题的消息混入当前面板。“清除本题对话”会归档旧会话并创建新的空会话；旧消息不再参与后续模型上下文，但仍保留提示等级、token 用量和补丁审计。题面自动抓取仅读取 Codeforces 的 `.problem-statement` 或洛谷公开题面字段，不读取题解；失败时可粘贴人工题面，人工版本优先并可显式恢复自动版本。补丁应用前校验受管 `.cpp` 路径与基线 SHA-256，冲突不会覆盖新修改，验证失败也不会自动回滚。

首次发送前，Dashboard 会提示数据出站范围。AI 推荐只发送已分类的去重平台 AC 摘要（题号、平台、难度、可用的接受日期和知识板块）与确定性候选；不发送账号、handle、UID、submission ID、raw JSON、语言、notes、聊天、源码、本机路径、API Key 或运行 token。AI 题单导入会发送用户主动输入的文本；整理模式额外发送已识别题目的公共名称和平台原始标签。生成模式不批量发送本地候选摘要：首轮只发送文本、题数、支持平台与 JSON 约束，补题轮次额外发送此前接受/排除的严格 `problem_ids` 和剩余数，不发送拒绝原因、本地完成状态或题库存在性细节。题单导入不发送账号、UID、提交详情、源码、聊天、现有题单、用户标签覆盖、本机路径、API Key 或运行 token；`ai_runs` 记录 `kind=plan_import`、聚合模型用量、轮数和脱敏请求摘要，不保存输入原文、题号或 thinking 内容。工作台对话会发送当前题面、有效标签、源码、attempt 和最近对话，但不会发送账号、文件路径或 API Key。`reasoning_content` 不展示也不保存。

常用 CLI：

```powershell
.\acm.ps1 ai settings --recommend-model deepseek-v4-flash --coach-model deepseek-v4-pro --thinking --reasoning-effort high
.\acm.ps1 context fetch CF1234A
.\acm.ps1 context show CF1234A --json
.\acm.ps1 context set CF1234A --file .\statement.md
.\acm.ps1 ask CF1234A "我这个不变量哪里错了？" --mode hint --hint-level 2
.\acm.ps1 patch preview CF1234A "修复越界" --json
.\acm.ps1 patch apply <proposal-id> --json
.\acm.ps1 patch revert <proposal-id> --json
```

## 多题单

导入、托管和导出的题单使用 canonical `plan.json` v2，可包含任意数量的阶段和题目；仓库内置的旧 v1 题单仍会兼容读取并在托管写入时规范化。网页“题单”页默认只读；进入某个阶段的编辑模式后，可增删题目并修改题号、名称和标签。解锁/截止日期统一属于阶段。编辑即时保存，每个写操作带修订号；冲突时会要求刷新，最近 5 个版本可以恢复。

题单页同时保留“导入 JSON”和“AI 快速导入”。后者先提交异步 job，再进入可编辑预览：

```text
POST /api/jobs/ai/plans/preview
{"mode":"organize","text":"第一阶段：CF1A、P3374"}

POST /api/jobs/ai/plans/preview
{"mode":"generate","text":"生成一份线段树与树状数组强化题单","task_count":12,"include_completed":false}
```

`organize` 会从自然语言、Markdown、题号和官方链接识别最多 200 道 Codeforces/洛谷题。服务端固定允许题目集合，模型只能返回标题、分组主题、截止日期和 `problem_keys` 的严格排列；description、level、note、稳定键与最终 canonical 结构均由本地生成。增加、遗漏、重复题目，网络失败或非法 JSON 会优先产生保持原始顺序的确定性单阶段 fallback；只有该草稿通过完整业务校验才可用。缺少 API Key 时任务直接提示进入设置，不执行降级。

`generate` 默认生成 12 道、上限 30 道，由独立的 `plan_generate` profile 选择模型，固定使用 `reasoning_strength=high`。模型每轮只能返回严格 JSON `{"problem_ids":[...]}`；服务端规范化并去重后，只接受本地题库中存在、没有 active session，并且在默认情况下不是 AC/Skip 的题号，`include_completed=true` 才允许后两类。有效题会跨轮累计，最多请求 5 轮；连续 2 轮没有新增时提前结束，全程不自动联网同步。

生成结果始终由服务端确定性 lowering 为单阶段 canonical plan v2，模型不能生成阶段、URL 或最终题单结构。若 5 轮后有效题仍少于请求数量，job 返回带 `insufficient_valid_problems` error 的可编辑部分草稿，而不是填入无关题；部分草稿的确认导入按钮保持禁用，手工补足题目并重新预览通过后才可导入，也可调整目标或题数重新生成。

成功 job 返回普通题单预览字段（`plan`、`errors`、`warnings`、`assumptions`、`unresolved`、`duplicate`、`current_revision`、`diff`）以及 `ai.run_id/model/usage/mode/fallback`；生成模式还报告脱敏的轮数、已接受题数和停止原因。模型由 `plan_generate` profile 解析；生成模式固定使用 high 推理强度，但 provider reasoning 内容不展示或保存。稳定 `plan_id`、阶段/任务键、官方 URL、公共题名与 canonical plan v2 都由本地服务生成。预览不会创建 `.acm/plans/` 文件或题单数据库记录，但会保留必要的 job、AI run 与缓存审计状态；编辑后的完整草稿必须重新通过 `POST /api/plans/preview`，最后仍以 `POST /api/plans/import` 显式写入。带 `insufficient_valid_problems` error 的部分草稿不能导入。替换同 ID 题单必须确认并携带 `expected_revision`，HTTP 409 后重新预览。

Codeforces/洛谷题量和占比无需写入题单文件。网页与 API 会从各阶段的主任务实时计算，替换候选不计入分母；增删题目后立即更新。旧题单中的 `platform_target` 可兼容导入，但规范化、导出或保存时会自动移除。

导入文件会作为托管副本写入 `.acm/plans/`。删除题单或从题单删题只解除关联，不会删除平台 AC、session、复做记录或本地源码。内置数据结构题单的仓库源文件始终保持只读。

推荐默认使用 `balanced` 来源：已启用题单与 Codeforces/洛谷题库组成普通并集，题单身份、日期和层级不加分、不参与平分优先级，也没有题单数量配额。也可选择 `catalog_only`、`plan_only`，或限定参与推荐的题单。

### 有效题目标签

标签分为三层：平台同步得到的原始标签始终原样保存；训练使用的有效标签会合并原始标签和所有题单中的人工标签，过滤年份、地区/省选、赛事来源、O2/编译选项等元标签，再应用同一道题全局共享的 `suppress` 与 `add` 覆盖；`close` 时会冻结本次 attempt 的有效标签快照。推荐、薄弱项和周复盘只使用有效标签，后续清洗不会改写历史 attempt 的归因。

题单页保留“补全标签”，默认 `fill_missing`，只为当前有效标签为空的题目生成建议；“清理标签”使用 `cleanup`，会同时显示原始标签、当前/建议有效标签、新增、删除和被忽略的元标签。Codeforces 标签来自官方题库快照，洛谷标签来自每道题的公开题面元数据；单题抓取失败只会显示为未解析，不会阻止其他题目生成建议。

预览中的建议可以逐题编辑；`tags` 表示期望的完整有效标签集合，因此空数组表示显式删除全部有效标签。只有用户确认“应用标签”后，系统才会校验题单 `base_revision` 与全局 `override_revision`，以一个新修订写入 `.acm/plans/<plan_id>.json` 并更新全局 add/suppress。任一修订冲突都必须重新加载并生成预览，不能覆盖较新的修改。阶段编辑模式中的标签输入框仍可用于日常手工修改。

Agent 在网页服务未运行时可使用同一事务流程：

```powershell
.\acm.ps1 plan tags preview <plan-id> --output .\.acm\tag-preview.json --json
.\acm.ps1 plan tags preview <plan-id> --mode cleanup --output .\.acm\tag-cleanup-preview.json --json
.\acm.ps1 plan tags apply <plan-id> .\.acm\tag-preview.json --json
```

preview JSON 会携带题单和全局覆盖两个修订号，CLI apply 会自动读取并校验。平台没有公开标签且用户明确要求补充时，仓库 `acm-workflow` skill 允许 Agent 只读取题面和元数据来生成专题标签，再通过上述 apply/API 写回托管题单；不得直接修改内置题单源文件。标签只描述题目专题，不代表 AC。

`close` 始终先独立保存 attempt，不会自动修改知识库。“结束与复盘”可显式启用 AI Markdown 总结：发行树根目录的脱敏 `algorithms.md`、`tricks.md` 示例模板会以固定 schema 自动成为已保存目标，其他 `.md` 可推断或自定义 schema。Dashboard 只显示可编辑 Markdown 与安全渲染预览，不展示 unified diff；确认 apply 前目标保持零修改。若 `Source` 题号完全相同，服务只把对应旧条目发送给所选模型，与本次知识语义合并；标题相似但题号不同则新增条目。服务端继续以 proposal revision、基线哈希、备份、原子替换和 hash-guarded revert 保护写入。

## 开发验证

```powershell
python -m unittest discover -s tests -v
python -m compileall -q tools tests
.\acm.ps1 plan check
```
