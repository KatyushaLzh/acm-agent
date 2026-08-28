# Contributing

感谢你改进 ACM Agent。

## 开发约束

- 保持核心代码兼容 Python 3.10 及以上正式版；不要为核心运行引入 pip/npm 依赖。Unix Dashboard 的可选安全存储依赖必须固定在 `tools/requirements-web-unix.lock`，由项目隔离环境管理。
- 保持网页仅监听回环地址，不削弱令牌、Host、Origin 和请求大小校验。
- SQLite 是运行状态的唯一事实源；文件名不能作为 AC 证据。
- 平台同步失败必须保留最后一次成功快照。
- 题单编辑必须使用事务和 `expected_revision` 冲突保护。
- 不提交真实账号、UID、cookie、token、平台快照、源码答案或 `.acm/`。

Unix Dashboard 安全存储依赖以 `tools/requirements-web-unix.in` 为可审阅源，使用项目固定的 uv 0.12.5 生成跨平台锁。更新依赖后必须重新生成并保留完整条件依赖与 distribution hashes：

```bash
uv pip compile tools/requirements-web-unix.in \
  --output-file tools/requirements-web-unix.lock \
  --universal --python-version 3.10 --generate-hashes \
  --only-binary=:all: --no-annotate --no-header --no-config --upgrade
```

## 提交前检查

```bash
python -m unittest discover -s tests -v
python -m compileall -q tools tests
python -m tools.acm_agent plan check --json
```

测试网络协议时请使用 `tests/fixtures/platforms` 中的脱敏固定夹具，避免让 CI 依赖实时平台。

功能变更应同时更新 README、CLI/API 测试和网页端到端测试。安全相关问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 issue 中附带令牌或个人状态库。
