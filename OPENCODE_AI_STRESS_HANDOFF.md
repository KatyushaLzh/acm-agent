# OpenCode 接力 Prompt：ACM AI 持续对拍可靠性重构

你现在接手 `D:\code\acm` 中 AI 持续对拍的可靠性重构。请直接检查、修改、验证本地源码，不要只给建议。工作区没有可用 Git 历史；不要执行 `git reset`、`git checkout` 或覆盖用户主解。所有付费 provider 调用都必须先得到用户明确许可；普通开发和本地验证必须 provider-free。

## 1. 最终目标

将 AI 自动生成 generator、validator、brute、reference，联合审查、完整 preflight、应用 helper、创建受控持续对拍 run 的链路做成跨题可靠流程，而不是只针对 P2596 打补丁。

核心发布门槛：

- 正确率至少 80%。
- P2596 完整 AI cold 至少 16/20。
- Core 8 每题 3 次，总体至少 20/24，且每题至少 2/3。
- 非法输入或语义错误 bundle 被应用次数为 0。
- P2596 tokens：p50 <= 45k，p95 <= 80k。
- Core 8 tokens：p50 <= 35k，p95 <= 60k。
- 单次 preparation provider tokens 硬上限 100k。
- warm cache 必须严格 0 provider requests / 0 tokens。
- P2596 成功耗时 p50 <= 240 秒、p95 <= 480 秒。
- Core 8 成功耗时 p50 <= 180 秒、p95 <= 360 秒。
- provider-free application-cold 20/20，p95 <= 90 秒，max <= 105 秒，无残留进程或临时目录。

不得为了通过 P2596 而在生产逻辑中硬编码 P2596。题目特化 gold oracle 只能存在于 benchmark/test 层。

## 2. 安全不变量

- AI stress 必须显式 opt-in。
- 模型不能看到用户主解；generator、brute、reference、validator 分支彼此按最小上下文隔离。
- statement 与外部源码都视为不可信数据，不能覆盖系统指令。
- 只允许生成/应用 `.gen.cpp`、`.bf.cpp`、`.ref.cpp` 和内部 validator；不得修改用户主解。
- 所有 helper、reference、用户解必须在无网络 Windows AppContainer + kill-on-close Job Object 中运行。
- sandbox probe 失败必须 fail-closed，禁止退回当前用户权限执行。
- helper 源码安全检查、编译、capability、确定性、smoke、AI audit、完整 preflight、联合认证全部通过后才允许 apply。
- 失败 cold run 不得覆盖已存在的正确 alias/proof。
- 所有付费失败证据必须保留，旧报告不得删除或改写。
- 不复制、显示或持久化 API key、DPAPI credential、runtime token、reasoning_content。

## 3. 当前已实现状态

### 预算与配置

- `tools/acm_agent/config.py`：`CONFIG_VERSION = 7`。
- `tools/acm_agent/storage.py`：`SCHEMA_VERSION = 12`。
- 默认 preparation timeout 为 600 秒，可显式设置 60..1800 秒。
- `tools/acm_agent/stress_budget.py` 的 600 秒累计硬截止：
  - context 20s
  - contract 85s
  - initial_prepare 300s
  - audit_initial 345s
  - repair_1 425s
  - provider hard stop 480s
  - local_validation 590s
  - total/cleanup 600s
- provider 不得借用 480..600 秒的本地验证和清理时间。
- non-thinking / thinking / audit 单请求上限为 120 / 180 / 50 秒。
- non-thinking / thinking / audit 最小启动窗口为 8 / 30 / 8 秒。
- `MAX_PROVIDER_TOKENS_PER_PREPARATION = 100_000`；响应使累计越界或下一请求开始前已达上限都必须抛专用错误。

### Token 上限

- contract 2048，repair 4096。
- validator probe 独立认证 1536，non-thinking。
- generator blueprint/recipe 4096，repair 8192。
- generator 8192，repair 12288。
- validator 6144，repair 8192。
- brute 4096，repair 6144。
- reference 8192，repair 12288。
- artifact audit 1024。
- Luogu 外部 reference audit 512。

### Prompt、policy、preflight identity

当前常量位于 `tools/acm_agent/stress_runtime.py`：

- `STRESS_PREPARATION_CACHE_VERSION = 2`
- `STRESS_CONTRACT_PROMPT_VERSION = 5`
- `STRESS_BLUEPRINT_PROMPT_VERSION = 6`
- `STRESS_ARTIFACT_PROMPT_VERSION = 7`
- `STRESS_PREFLIGHT_VERSION = 5`
- `STRESS_SAFETY_POLICY_VERSION = 2`
- `STRESS_SANDBOX_POLICY_VERSION = 2`
- `STRESS_BLUEPRINT_POLICY_VERSION = 3`

任何 correctness-relevant 行为变化都必须提升相应 identity，不能复用旧 proof。

### Contract schema v3

`tools/acm_agent/stress_ai.py::extract_contract()` 输出结构化 syntax、constraints、evidence、coverage obligations 和隐藏 validator probes。

已实现：

- 安全字段类型别名归一化到 int/float/string/token/char。
- presentation/provenance `source` 字段只允许短字符串并在归一化后丢弃。
- evidence 必须逐字绑定题面；evidence-only repair 只能修改 evidence 和必要引用。
- contract 两次失败的原始 JSON 摘要、SHA 和诊断会保留。
- `state_precondition`、`dependent_bound`、`graph_predicate` 必须提供同 token 数、只差 1..2 token 的正负完整输入。
- `stress_ai.py::_certify_contract_validator_probes()` 使用第二个、看不到任何 helper 源码的独立请求逐条重放，纠正 probe 极性或替换/删除无法证明的 pair。
- 独立认证结果仍须通过本地结构归一化；旧 contract cache 由 prompt v5 失效。

注意：这个独立认证仍然是同 provider/model 的另一请求，不是数学 gold。最终发布统计仍必须依赖模型不可见的 benchmark gold corpus。

### Generator

- 模型只生成题目相关 adapter；可信 harness 独占 main、argv、seed、profile-v2、manifest、SHA、records、确定性和资源限制。
- C++ 优先 code-only completion，repair prompt 只带紧凑 contract、相关 evidence、旧代码和机器诊断。
- Hybrid fast-first：第一次有精确机器诊断的 generator/validator repair 使用 non-thinking；generator 第二次允许 thinking + 从头重写。
- generator 最多修复两次，其他 helper 最多一次。
- blueprint 中自然语言 construction 只是候选策略；本地绑定的 dimensions、records、operation families、coverage obligations 才是权威项。
- 有状态操作必须按最终输出顺序从不可变初态重放，非法参数/顺序在 append 前替换；禁止验证后 shuffle。
- ops 容器是记录数唯一事实源；输出计数必须来自 `ops.size()`。
- record count 机器诊断能指出例如“声明 8 条但发现 9 条 tagged records”。

### Validator

- validator 初始生成、repair 和 audit 都不能看到隐藏 probes。
- `stress.py::_preflight_validator_probes()` 对认证后的 valid/invalid 两侧都执行并记录真值表。
- valid 被拒：`stress_validator_positive_probe_failed`。
- invalid 被接受：`stress_validator_negative_probe_failed`。
- runtime repair diagnostic 只允许 constraint ID、valid_accepted、invalid_accepted；必须删除 probe 原文、SHA、probe ID、seed 和 stderr。
- coverage tags 按集合语义去重；未知或缺失 obligation 仍然 fail-closed。
- benchmark 有 Core 8 独立手写 validator/gold oracle，不使用用户主解。

### Checkpoint、cache 和数据库

数据库 v12 已包含：

- `stress_artifact_candidates`
- `stress_artifact_proofs`
- `stress_bundle_certifications`
- `stress_cache_aliases`
- `problem_samples`

`tools/acm_agent/stress_checkpoint.py` 将缓存身份拆为：

- `generation_identity`：模型、模式、prompt、题目语义、角色内容。
- `certification_identity`：源码、compiler、sandbox、样例、协议、policy、preflight。

角色成功后立即保存 candidate/proof；兄弟角色失败不得丢失已完成角色。不同 checkpoint 组合后仍必须进行 exact-trio 联合 preflight。

Cache API：

- `reuse`：正常复用。
- `refresh_helpers`：复用 contract/blueprint，重新生成 helper。
- `cold`：绕过所有本地读取。
- `force_regenerate=true` 兼容映射为 `cold`。

### CLI 和服务接口

CLI：

```powershell
.\acm.ps1 verify P2596 --ai-stress --cache-mode cold --generation-mode hybrid --prepare-timeout 600 --json
.\acm.ps1 stress status <run_id> --json
.\acm.ps1 stress stop <run_id> --json
.\acm.ps1 stress resume <run_id> --json
.\acm.ps1 stress artifacts <bundle_id> --json
.\acm.ps1 stress revert <bundle_id> --json
```

`--force-regenerate` 等价于 `--cache-mode cold`；二者冲突时必须拒绝。

服务入口：

```python
Service.ai_stress_start(
    problem,
    generate_generator=True,
    generate_brute=True,
    prepare_reference=True,
    large_profile=True,
    preparation_timeout_seconds=600,
    force_regenerate=False,
    cache_mode="reuse|refresh_helpers|cold",
    generation_mode="fast|hybrid|full_thinking",
    model=None,
    seed=None,
    timeout=2.0,
    brute_timeout=5.0,
    compare="token",
    progress_callback=None,
)
```

Web job：`POST /api/jobs/ai/stress/start`。

### Benchmark 接口

- `stress_benchmark.py::run_core8_gold_gate()`：完全 provider-free 的 Core 8 corpus/mutation gate。
- `stress_benchmark.py::run_local_application_cold_batch()`：真实 AppContainer、编译、preflight、临时 apply、16 small + 4 large、清理的 provider-free 冷门禁。
- `stress_benchmark.py::run_live_ai_cold_batch()`：付费、可恢复、每次全新 workspace/空 DB 的 cold batch；每次 attempt 立即追加 `attempts.raw.jsonl`，支持 `resume=True`，计划身份变化时拒绝 resume。
- Live attempt 固定 `cache_mode=cold`、`generation_mode=hybrid`，成功后可检查 warm cache 0/0。
- 报告必须输出 raw JSONL、JSONL、CSV、summary JSON、Markdown、逐 attempt evidence 目录。

## 4. 已完成的验证与报告

### 首轮正式 P2596 20 cold

报告：

`D:\code\acm\.acm\reports\stress-reliability\20260806-151111-final-reliability\`

结果 14/20（70%），未达 80%。主要失败：

- generator 实际 records 与 header 声明不一致。
- blueprint/contract 自然语言 construction 锁死非法状态序列。
- contract schema 漂移（`source`、type aliases）。
- validator coverage tag 重复。
- evidence quote 改写而非题面原文。
- 更严重的是部分“成功” validator 删除/破坏动态状态检查，旧统计只证明事务安全，不能证明语义零误放。

### r2 Canary

报告：

`D:\code\acm\.acm\reports\stress-reliability\20260806-171833-final-reliability-r2\`

结果失败，65.33 秒、22377 tokens、6 requests、0 apply。精确根因：

- contract `vp3.valid_input = Top 3; Insert 3 -1`，实际非法。
- `vp3.invalid_input = Top 3; Insert 3 +1`，实际合法。
- 正确 validator 返回 `ERR_INSERT_TOP`，编排器却把责任归给 validator 并消耗一次 repair。
- runtime 把 `generated_input_excerpt` 原样放进 validator repair prompt，隐藏 probe 发生泄露。
- 报告中的 `model_random_failure` 分类不准确；应为 `contract_probe_semantic_conflict/system_attribution_regression`。

当前源码已经修复此问题：增加源码盲独立 probe 认证、成对真值表和 repair diagnostic 脱敏。尚未进行新的付费 Canary。

### 当前 provider-free 验证

- `python -m unittest discover -s tests`：431 passed，2 skipped。
- `run_core8_gold_gate()`：合法 13/13、非法 21/21、false accepts=0、false rejects=0、mutation kill=100%。
- 新一轮 application-cold：20/20、0 provider requests、0 tokens、p50 44.17s、p95 53.74s、max 65.86s、无残留目录/进程。
- 报告：`D:\code\acm\.acm\reports\stress-reliability\20260806-canary-probe-fix-provider-free\`。

## 5. 当前尚未完成的关键工作

### P0：benchmark gold gate 仍发生在 apply 之后

当前 `stress_benchmark.py::_live_attempt()` 先调用 `StressCoordinator.start()`；`start()` 已在 `stress_runtime.py` 中执行 `manager.apply(staged)`、持久化并创建 run，随后 benchmark 才调用 `_certify_live_validator_with_gold()`。

这能在报告中发现语义错误 validator，但不能严格满足“错误 bundle 被应用次数为 0”。必须修复为：

1. production 仍保持通用，不硬编码 Core 8/P2596。
2. 为内部 runtime 增加非公开、仅 benchmark 注入的 `pre_apply_gate` 或等价 staged certification hook。
3. hook 在完整联合 preflight 和 certification 已成功、但 `manager.apply(staged)` 之前执行。
4. benchmark hook 直接编译/运行 staged validator，使用模型不可见 gold corpus。
5. gate 失败时 discard staged bundle：数据库 applied bundle count=0、run count=0、真实 helper 不变。
6. 不允许通过 public API/CLI 注入任意回调；该 hook 只能是本地 benchmark 内部能力。
7. 添加精确测试，证明 bad validator 的 `applied_bundle_count == 0`，而不是 apply 后再标 unsafe。

建议插入点：`stress_runtime.py` 完整 preflight/certification 后、当前约第 2951 行 `manager.apply(staged)` 前。

### P0：新的付费 Canary 尚未执行

在 P0 pre-apply gold 修复和 provider-free 回归完成后，只执行 1 次 P2596 paid canary：

- 全新临时 workspace。
- 空数据库。
- `cache_mode=cold`。
- 600 秒。
- hybrid。
- 成功必须完成 gold pre-apply、apply、创建 run、16 small + 4 large、停止清理、warm 0/0。
- 如果失败，立即停止，不得直接启动 20 次正式批次。
- 保存完整 evidence，按 contract/generator/validator/brute/reference/audit/preflight/run/cleanup 精确归因。

动态 constraint 相比 r2 预计多 1 次最多 1536 completion tokens 的 non-thinking probe certification 请求；报告不应把这一请求误判为 retry。

### P1：正式发布统计尚未执行

Canary 成功后且得到用户明确付费许可，再运行：

1. P2596 完整 AI cold 20 次。
2. Core 8 每题 3 次；P2596 前三次复用上述结果，总共 41 次独立付费 setup。
3. 每次全新 workspace、空 DB、`cache_mode=cold`。
4. 低于门槛时修复后重新执行完整批次；旧失败不可删除。
5. 批次必须可 `resume=True`，但只在 `batch-plan.json` 完全一致时恢复。

Core 8：P1001、P1111、P3379、P3834、CF380C、P3373、CF1354D、P2596。

### P1：报告分类与审计信息可继续增强

- 区分 `contract_probe_semantic_conflict`、`validator_overreject`、`validator_underreject`、`generator_invalid`，避免统称 model random failure。
- 独立 probe certification 的 request、tokens、耗时应有独立 stage/category。
- 若 certification JSON/语义失败，应保存 bounded raw response SHA/excerpt，不能只保留 usage。
- 所有 provider prompt-capture 测试继续断言 validator 初始/repair/audit 永远不含 probe 原文或 hash。

### P2：成功率、速度与 token 的后续优化顺序

1. 优先减少错误归因和错误 repair，避免为不可修角色付费。
2. 使用本地机器诊断和精确 witness；repair 只传相关 contract/evidence/old code。
3. 初始 helper 并行，明确诊断后才启用 thinking。
4. 每角色 checkpoint，失败角色不拖累已认证兄弟角色。
5. 对重复结构字段做安全规范化，但禁止发明语义。
6. 只有 failure evidence 证明旧架构根本错误时才从头重写。
7. 不得用降低 preflight、删除动态检查、缩小 large、减少 16 small 或绕过 audit 来换成功率。

## 6. 建议执行顺序

1. 阅读：
   - `AGENTS.md`
   - `.agents/skills/acm-workflow/SKILL.md`
   - `.learnings/LEARNINGS.md`
   - `.learnings/ERRORS.md`
   - `.learnings/FEATURE_REQUESTS.md`
   - r1/r2 `gpt-handoff.md`、`evidence.json`、summary/report。
2. 验证 recovery manifest 与当前关键源码 hash，确认没有他人并发改动。
3. 实现 benchmark-only pre-apply gold gate，不改 public authorization boundary。
4. 增加 bad staged validator 0-apply、good validator pass、gate exception cleanup 三类测试。
5. 运行：

```powershell
python -m py_compile tools\acm_agent\stress_ai.py tools\acm_agent\stress.py tools\acm_agent\stress_runtime.py tools\acm_agent\stress_benchmark.py
python -m unittest tests.test_stress_ai tests.test_stress tests.test_stress_runtime tests.test_stress_benchmark tests.test_stress_budget tests.test_stress_storage
python -m unittest discover -s tests
```

6. 运行 `run_core8_gold_gate()` 和至少一次 provider-free application-cold；改动 runtime apply 生命周期后应重新跑完整 20 次。
7. 检查 `%TEMP%\acm-application-cold-*`、相关 stress/compiler/helper 进程、真实 helper hash、真实 DB/config 指纹。
8. 通知用户“已准备付费 Canary”，等待明确许可。
9. Canary 通过后再次请求正式 41 次付费 batch 许可。
10. 最终报告成功率、首轮成功率、修复角色、失败阶段、stage durations、token categories、provider requests、retries、p50/p95/max、warm 0/0、zero semantic misrelease。

## 7. 禁止事项

- 不要删除或覆盖任何既有 reliability report。
- 不要把 r2 Canary 归因为 generator；该 case 来自 contract probe。
- 不要自动用当前 validator 的输出交换 probe 极性；反逻辑 validator 可能成为错误 oracle。极性必须由独立源码盲分支判断。
- 不要把 hidden probe 放入 validator prompt、diagnostic、previous diagnostics 或 audit witness。
- 不要读取或向模型发送用户主解。
- 不要触碰真实 P2596 helper、正式 DB 或正式 run 来做付费验证。
- 不要在用户未授权时启动 provider。
- 不要用缓存命中伪装 cold；cold 必须绕过全部本地读取。
- 不要把一次 Canary 通过当成 80% 发布证据。

## 8. 恢复副本

真实 Git 仓库：

`D:\code\acm-agent\`

独立恢复包目录：

`D:\code\acm-agent\recovery\20260806-ai-stress-handoff\`

其中应包含：

- `acm-ai-stress-recovery.zip` 内的 `workspace\`：当前源码、测试、项目说明、学习记录、P2596 相关本地文件和选定 reliability reports。
- `manifest-sha256.csv` 与 `manifest-sha256.json`：逐文件相对路径、大小和 SHA-256。
- `acm-ai-stress-recovery.zip`：同一快照的压缩包。
- `RECOVERY_README.md`：恢复和验证步骤。

项目源码和 recovery 包直接提交到真实仓库既有的 `main`，不创建额外分支。

恢复时只复制显式源码/测试/文档文件；不要从快照恢复 `state.db`、credential、runtime token 或旧 run 状态。

完成每个阶段后更新 `.learnings`，但在付费批次真正达到 80%/零误放前，`FR-20260806-001` 必须保持 `in_progress`。
