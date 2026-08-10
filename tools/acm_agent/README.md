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
.\acm.ps1 verify CF1234A --ai-stress
.\acm.ps1 verify CF1234A --ai-stress --validator --strict  # validator 完整严格认证，失败不创建 run
.\acm.ps1 verify CF1234A --ai-stress --minimal --unvalidated-large  # Dashboard 默认验证 + 极限大数据
.\acm.ps1 close CF1234A
.\acm.ps1 review week
```

样例放在 `.acm/cases/CF1234A/name.in` 与同名 `.out`。默认按 token 比较；`verify --exact` 改为字节比较。存在同目录的 `CF1234A.bf.cpp` 和 `CF1234A.gen.cpp` 时会自动对拍，失败资产保存在 `.acm/failures/`。

所有读取状态的命令都支持 `--json`。推荐输出包含数据新鲜度、位置、总分、每项分数和选择原因；平台同步失败不会删除最后一次成功快照。

网页与 CLI 直接调用同一业务服务层，不通过子进程互相调用。受支持的旧版 `.acm/state.db` 会自动升级到当前 schema v16；直接打开 v4–v13 数据库时，会在首个受保护迁移前通过 SQLite backup API 创建相邻的版本化 `.bak`，已有备份不会被覆盖。

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

做题工作台支持按题目持久化的流式对话、1–3 级提示、4 级代码诊断，以及“生成带错误说明注释的完整候选源码 → C++ 语法高亮预览 → 用户确认 → 备份并验证”的补丁流程。预览区只显示修改后的完整代码，不展示 unified diff；diff 仍由服务端生成并保留用于审计和安全应用。切换题号时会恢复该 active attempt 对应的会话，不会把另一道题的消息混入当前面板。“清除本题对话”会归档旧会话并创建新的空会话；旧消息不再参与后续模型上下文，但仍保留提示等级、token 用量和补丁审计。题面自动抓取仅读取 Codeforces 的 `.problem-statement` 或洛谷公开题面字段，不读取题解；失败时可粘贴人工题面，人工版本优先并可显式恢复自动版本。补丁应用前校验受管 `.cpp` 路径与基线 SHA-256，冲突不会覆盖新修改，验证失败也不会自动回滚。

首次发送前，Dashboard 会提示数据出站范围。AI 推荐只发送最多 90 天/50 次尝试的结构化结果与冻结标签，不发送账号、notes、聊天、源码或本地路径；工作台对话会发送当前题面、有效标签、源码、attempt 和最近对话，但不会发送账号、文件路径或 API Key。`reasoning_content` 不展示也不保存。

“本地验证”的 AI 持续对拍同样默认关闭。准备总 deadline 默认 600 秒，可配置为 60–1800 秒；默认档在 480 秒关闭 provider、590 秒结束本地门禁，最后 10 秒 shutdown/清理。AI 不再生成 brute，而是优先从白名单来源搜索两份不同 URL、不同源码哈希的完整正确解候选；不足时以互相不可见、也不可见用户源码的独立请求生成满足最大约束的 reference。生成或下载的 artifact 会按机器诊断做有界修复，generator、每份 reference 与 validator 的具体上限由失败类别决定，部分源码安全或样例失败允许第二次修复。AppContainer、官方样例、16-case 变异检查、边界和 large 门禁均保留；Dashboard 的默认 Minimal 会跳过 validator 与 AI audit，并放宽通用 manifest/非-recipe coverage，但本地 recipe coverage 和 seed/output variation 仍执行。任何失败或超时都保留旧 helper 且不创建 run。

validator 是可选的输入认证角色。默认不生成 validator，small 与 large 均运行 `reference_primary/reference_secondary`；两者一致后才能裁决用户解，两者不一致立即 `oracle_conflict`，不自动猜测或修复任一方。Dashboard 提供默认未勾选的“启用 validator（启用该选项会执行完整严格认证，显著提高AI对拍器正确性，但是成功生成率会显著下降）”选项；勾选后会要求 validator 生成、隐藏正负 probe 认证、源码安全、编译、AI audit、机器门禁和联合 preflight 全部成功。修复耗尽后任一步失败都不允许降级，不应用 helper、不创建 run，并统一报告 validator 严格认证失败。CLI 的等价用法是 `verify <ID> --ai-stress --validator --strict`；只有显式使用 `--validator` 且未加 `--strict` 时，才保留 `unvalidated` 降级与 `--unvalidated-large` 选项。新 helper 为 `<ID>.ref1.cpp/.ref2.cpp`，手动 CLI 入口为 `--generator-file/--reference-primary-file/--reference-secondary-file`；旧 `--reference-file/--brute-file` 仅作弃用别名。

Dashboard 的 generator、两份 reference 和新增 Markdown 目标都通过系统原生文件选择器选择，不接受网页文本框手输路径。手动 helper 可以位于工作区外，但必须是现有的本机普通 `.cpp` 文件；服务只读原文件，将副本放入受管 staging 后仍执行源码审查、编译与 AppContainer 门禁。Markdown 选择器可选现有 `.md`，也可指定一个尚不存在的新 `.md`，注册时仍需完成“检查路径/schema → 再次确认保存”。AI 对拍准备不会暂停等待人工审核 contract 或输入修复提示；机器门禁失败时仅使用有界的模型自动修复，最终失败则直接安全停止。

contract schema v3 与 generator blueprint 在源码生成前本地验证证据、seed、覆盖闭合和 large 复杂度。新 bundle 以 `dual_reference_v1` 保存双 reference candidate/proof/联合认证并与旧 `legacy_trio` 缓存隔离；validator 开关也是缓存身份的一部分。严格模式只复用同时具有 validator 源码 artifact、release executable、完整认证记录和 validator preflight 成功证据的 bundle；无 validator、曾降级或历史缓存不能满足严格模式。这里的“零误放”是针对这套现有门禁的 fail-closed 运行保证，不代表数学上证明 AI validator 绝对正确。旧 `.bf.cpp/.ref.cpp` bundle 只用于历史 run 的查看、恢复和回滚。

静态输入 contract 先尝试完全本地的 `generator_recipe/v2`。当前 v2 只识别 `mutable_permutation` 与 `bracket_interval_queries` 两类 wire shape：本地编译器把 contract 的字段、范围、状态机和四个 profile-v2 case 绑定到内置、哈希绑定的 C++17 机器 runtime，不发送 recipe 请求，也不接收模型生成的 executable generator。受支持 v2 contract 的预验失败会 fail-closed，不回退到 AI。其他 contract 才尝试 `generator_recipe/v1`：AI 只选择白名单 `template_id`、serializer、参数绑定和语义目标，本地 composer 内联审计过的 primitive；再不支持才记录 `legacy_ai_cpp:<reason>`。两版都输出 `acm_generate_case(seed, profile, case_kind, out)` ABI；recipe/contract hash、catalog hash 与 composer version 进入 cache/checkpoint/certification identity，模板、license 或 provenance 变化会使缓存失效。v2 只消除 generator 的 provider 请求；contract 提取和两份 reference 的获取仍可能使用网络或模型。

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

`close` 始终先独立保存 attempt，不会自动修改知识库。“结束与复盘”可显式启用 DeepSeek Markdown 总结：发行树根目录经维护者授权公开的 `algorithms.md`、`tricks.md` 题目知识记录会以固定 schema 自动成为已保存目标，其他 `.md` 可推断或自定义 schema。Dashboard 只显示可编辑 Markdown 与安全渲染预览，不展示 unified diff；确认 apply 前目标保持零修改。若 `Source` 题号完全相同，服务只把对应旧条目发送给 DeepSeek，与本次知识语义合并；标题相似但题号不同则新增条目。服务端继续以 proposal revision、基线哈希、备份、原子替换和 hash-guarded revert 保护写入。

## 开发验证

```powershell
python -m unittest discover -s tests -v
python -m compileall -q tools tests
.\acm.ps1 plan check
```
