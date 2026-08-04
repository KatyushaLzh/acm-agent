# Contributing

感谢你改进 ACM Agent。

## 开发约束

- 使用 Python 3.13 标准库；不要为核心运行引入 pip/npm 依赖。
- 保持网页仅监听回环地址，不削弱令牌、Host、Origin 和请求大小校验。
- SQLite 是运行状态的唯一事实源；文件名不能作为 AC 证据。
- 平台同步失败必须保留最后一次成功快照。
- 题单编辑必须使用事务和 `expected_revision` 冲突保护。
- 不提交真实账号、UID、cookie、token、平台快照、源码答案或 `.acm/`。

## 提交前检查

```bash
python -m unittest discover -s tests -v
python -m compileall -q tools tests
python -m tools.acm_agent plan check --json
```

测试网络协议时请使用 `tests/fixtures/platforms` 中的脱敏固定夹具，避免让 CI 依赖实时平台。

功能变更应同时更新 README、CLI/API 测试和网页端到端测试。安全相关问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 issue 中附带令牌或个人状态库。
