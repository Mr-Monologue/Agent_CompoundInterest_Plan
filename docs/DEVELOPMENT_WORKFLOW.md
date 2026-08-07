# Development Workflow

1. 从远端核验并同步长期 `feature` 与最新 `origin/develop`，不得重写历史。
2. 每个版本只承担一个清晰主题，先写业务合同、迁移和验收场景。
3. 实现时复用现有账本、计划、审计、草稿、幂等和确认边界。
4. 运行 Ruff、mypy、完整 pytest、PowerShell 解析、构建和隐私检查。
5. 提交并推送长期 `feature`，按 `feature -> develop -> release -> main` 创建 PR。
6. 每个阶段等待 Ubuntu、Windows CI 通过；失败时读取日志并在当前主题内修复。
7. release 合并后创建版本 Tag 和 GitHub Release；main 只接受 release。
8. 核对 develop、release、main 和 Tag 的 Git tree 完全一致。
9. GitHub 发布完成后停止；生产升级必须另行获得明确授权。
10. 生产升级后再验证 `/ready`、迁移、Hermes MCP、业务数据不变性和最小业务流程。

任何真实投资配置、现金、交易、持仓、申购、取消或更正事实都必须由 Ryan 使用自然语言明确确认。
