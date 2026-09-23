# Value DCA Agent 工程规则

## 分支与发布治理

- 长期分支固定为 `feature`、`develop`、`release`、`main`。
- 变更只能按 `feature -> develop -> release -> main` 推进。
- 禁止创建版本化 feature 或 release 分支，版本定位只使用 Git Tag 和 GitHub Release。
- 每次合并前必须等待 Ubuntu 与 Windows CI 通过；发布后核对 develop、release、main 和 Tag 的 Git tree。
- Ryan 已授权正常源码开发、提交、推送、PR、合并、Tag 和 GitHub Release，无需逐次申请。
- 禁止强制推送、重写已发布历史、丢弃用户修改或提交密钥、Token、数据库和生产事实。

## 投资安全边界

- 系统不得执行真实 BUY、SELL、申购或赎回。
- 策略、现金、交易、持仓和真实外部申购事实必须遵循草稿、用户明确确认、提交。
- 计划冻结、卖出建议批准和卫星信号开放都不等于成交。
- 只有用户在外部完成且再次明确确认的交易事实才能改变账本和持仓。
- 不自动猜测基准映射，不自动卖出，不使用不完整数据补齐金额、净值、份额或日期。
- 未验证或单一来源数据必须保持 WARNING/UNVERIFIED。

## 生产边界

- GitHub 发布授权不包含 Windows/Hermes 生产升级、生产数据库迁移或真实投资数据写入。
- 生产变更前必须获得当前版本的明确授权；只读状态检查无需确认。
- confirmation token 只保留在当前待确认生命周期，不写入 Git、日志或长期上下文。

## 新会话恢复顺序

1. 阅读本文件、`PROJECT_STATUS.md`、`ROADMAP.md`、`release-manifest.json` 和相关发布文档。
2. 核验本地 Git、远端四分支、Tag、PR、CI 和 GitHub Release，不把聊天记录当作唯一事实。
3. 读取被 Git 排除的 `.codex/local-context.md`，只把它视为本机最近快照。
4. 需要生产事实时，只读检查 `/ready`、Investor Core、Hermes MCP 和工作台。
5. 发现差异时以实时 Git/Core 事实为准；无法核实的内容标记 UNKNOWN。

## 面向 Ryan 的沟通

- 使用简体中文，先说结论，再说业务影响和下一步。
- 默认隐藏 UUID、内部 ID、哈希、Token 和原始 JSON；仅在“查看详情”时展示。
- 一次只请求一个主要决定。财务或配置写入必须出现“确认”；模糊的“继续”“可以”不构成授权。
- 只读操作直接执行；普通开发与已授权发布连续完成，只有真实阻塞或必须由 Ryan 决策时暂停。

## Windows 本机 Codex 入口

- Ryan 选择使用本机 Codex 同时处理开发和日常投资操作，不要求 Hermes 中转。
- 接入及排障先读 `docs/CODEX_WINDOWS.md`，使用现有 Core 和默认组合，禁止另建账本。
- 日常事实优先使用现有 Core HTTP 日常入口，先检查健康、就绪、只读默认上下文和工作台；不等待 MCP 修复。
- 自然语言操作遵循 `docs/CODEX_DAILY_OPERATIONS.md`；唯一进度与恢复入口为 `docs/LAUNCH_CHECKLIST.md`。
- MCP 不可用时排查连接、版本、托管服务和脱敏日志，不用直接 SQL 或聊天记忆替代事实。
- 开发测试使用独立检出和临时数据，生产部署仍遵循上面的当前版本确认边界。
- Codex 接入不等于 Hermes Cron/通知已迁移，不自动停用旧任务或添加重复调度。
