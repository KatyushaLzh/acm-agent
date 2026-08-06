# ACM AI Stress Recovery Snapshot

本快照用于恢复 2026-08-06 当前 AI 持续对拍可靠性重构状态。

## 内容

- `workspace/tools`：生产源码。
- `workspace/tests`：测试与 Core 8 fixtures。
- `workspace/.learnings`、`workspace/.agents`：项目约束与已知问题。
- `workspace/2026/8/5/P2596*`：当前 P2596 相关本地文件，仅用于恢复/比对，不得发送给模型。
- `workspace/reports`：r1、r2 Canary 与最新 provider-free 报告。
- `workspace/OPENCODE_AI_STRESS_HANDOFF.md`：可直接交给 OpenCode 的完整接力 prompt。
- `manifest-sha256.csv/json`：完整性清单。

不包含 `.acm/state.db`、`.acm/config.json`、DPAPI credential、web runtime token、临时目录、编译产物缓存或后台进程状态。

## 验证

在恢复目录中执行：

```powershell
$snapshot = 'D:\code\acm-agent\recovery\20260806-ai-stress-handoff'
$workspace = 'D:\path\to\extracted\workspace' # 改为 ZIP 解压后的实际 workspace 路径
$rows = Import-Csv -LiteralPath (Join-Path $snapshot 'manifest-sha256.csv')
$bad = foreach ($row in $rows) {
    $file = Join-Path $workspace $row.relative_path
    if (-not (Test-Path -LiteralPath $file)) {
        [pscustomobject]@{ path = $row.relative_path; status = 'missing' }
    } elseif ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $row.sha256) {
        [pscustomobject]@{ path = $row.relative_path; status = 'hash_mismatch' }
    }
}
$bad
```

无输出表示快照内容与清单一致。

## 恢复原则

1. 先把当前目标工作区再备份一次。
2. 只从 `workspace` 复制需要恢复的源码、测试和文档。
3. 不恢复数据库、credential、runtime token 或旧 run。
4. P2596 主解/helper 恢复前必须逐文件确认，避免覆盖用户更新。
5. 恢复后运行 handoff prompt 中的 py_compile、核心测试和全仓测试。
