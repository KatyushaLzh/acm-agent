# ACM Agent

Python 3.13 标准库驱动的本地训练系统。网页是主要入口，CLI 保留为自动化和无浏览器环境的兼容层。SQLite 是状态事实源；已有 `YYYY/M/D/*.cpp` 只会导入为 `local_only`，不会据此推断 AC。

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
.\acm.ps1 next --ai
.\acm.ps1 plan list
.\acm.ps1 plan import .\my-plan.json
```

`init` 会验证 Codeforces handle 和洛谷数字 UID。只有需要先离线建库时才使用 `--skip-validate`。

## 一次做题闭环

```powershell
.\acm.ps1 next --count 3 --mode mixed
.\acm.ps1 start CF1234A --with-stress
.\acm.ps1 verify CF1234A
.\acm.ps1 close CF1234A
.\acm.ps1 review week
```

样例放在 `.acm/cases/CF1234A/name.in` 与同名 `.out`。默认按 token 比较；`verify --exact` 改为字节比较。存在同目录的 `CF1234A.bf.cpp` 和 `CF1234A.gen.cpp` 时会自动对拍，失败资产保存在 `.acm/failures/`。

所有读取状态的命令都支持 `--json`。推荐输出包含数据新鲜度、位置、总分、每项分数和选择原因；平台同步失败不会删除最后一次成功快照。

网页与 CLI 直接调用同一业务服务层，不通过子进程互相调用。旧版 `.acm/state.db` 会自动升级；从 schema v4/v5 升级前会分别通过 SQLite backup API 保存一次数据库备份。

## DeepSeek BYOK 与 AI 工作台

AI 功能是显式 opt-in：普通 `next` 和原有工作流不会因为检测到密钥而调用模型。Windows Dashboard 的“AI 设置”可直接输入 API Key；服务使用 Windows DPAPI 按当前登录用户加密后保存到 `.acm/deepseek-key.dpapi`，重启后自动恢复。磁盘文件不含明文，密钥不会进入 JSON 配置、SQLite、日志或 API 响应。非 Windows 环境不会退化为明文存储，可继续使用进程环境变量：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
.\acm.ps1 ai status
.\acm.ps1 ai test
.\acm.ps1 next --ai --count 3
```

网页可随时替换或清除已加密凭据。若同时存在 DPAPI 凭据和环境变量，DPAPI 凭据优先；清除后仍可回退到环境变量。

推荐和对话默认使用 `deepseek-v4-flash`，可分别改为 `deepseek-v4-pro`。推荐始终先由确定性引擎生成合规候选，DeepSeek 只重排候选；网络、鉴权、余额、限流、非法 JSON 或越权题号都会回退到原确定性顺序，并保留原有分数、分项和原因。

做题工作台支持按题目持久化的流式对话、1–3 级提示、4 级代码诊断，以及“生成完整候选源码 → 服务端 Diff → 用户确认 → 备份并验证”的补丁流程。切换题号时会恢复该 active attempt 对应的会话，不会把另一道题的消息混入当前面板。“清除本题对话”会归档旧会话并创建新的空会话；旧消息不再参与后续模型上下文，但仍保留提示等级、token 用量和补丁审计。题面自动抓取仅读取 Codeforces 的 `.problem-statement` 或洛谷公开题面字段，不读取题解；失败时可粘贴人工题面，人工版本优先并可显式恢复自动版本。补丁应用前校验受管 `.cpp` 路径与基线 SHA-256，冲突不会覆盖新修改，验证失败也不会自动回滚。

首次发送前，Dashboard 会提示数据出站范围。AI 推荐只发送最多 90 天/50 次尝试的结构化结果与冻结标签，不发送账号、notes、聊天、源码或本地路径；工作台对话会发送当前题面、有效标签、源码、attempt 和最近对话，但不会发送账号、文件路径或 API Key。`reasoning_content` 不展示也不保存。

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

题单使用 `plan.json` v2，可包含任意数量的阶段和题目。网页“题单”页默认只读；进入某个阶段的编辑模式后，可增删题目并修改题号、名称和标签。解锁/截止日期统一属于阶段。编辑即时保存，每个写操作带修订号；冲突时会要求刷新，最近 5 个版本可以恢复。

Codeforces/洛谷题量和占比无需写入题单文件。网页与 API 会从各阶段的主任务实时计算，替换候选不计入分母；增删题目后立即更新。旧题单中的 `platform_target` 可兼容导入，但规范化、导出或保存时会自动移除。

导入文件会作为托管副本写入 `.acm/plans/`。删除题单或从题单删题只解除关联，不会删除平台 AC、session、复做记录或本地源码。内置数据结构题单的仓库源文件始终保持只读。

推荐默认使用 `balanced` 来源：三题中最多两题来自已启用题单，其余从 Codeforces/洛谷题库补充。也可选择 `catalog_only`、`plan_only`，或限定参与推荐的题单。

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

`close` 只在 `.acm/reports/` 生成归档候选。只有明确要求“总结/归档”时，仓库 skill 才会调用现有 `xcpc-summarize`，不会自行修改 `algorithms.md` 或 `tricks.md`。

## 开发验证

```powershell
python -m unittest discover -s tests -v
python -m compileall -q tools tests
.\acm.ps1 plan check
```
