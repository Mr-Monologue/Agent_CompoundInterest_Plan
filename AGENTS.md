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

- GitHub 发布授权本身不包含生产部署。Ryan 明确授权具体阶段包含部署后，阶段内沿现有架构、权限及已批准业务规则的常规版本可连续备份、短暂停服、安装、启动及验收，无需逐版确认。
- 当前阶段授权原文、四项范围、完成标准与外部依赖见 `docs/LAUNCH_CHECKLIST.md` 的“2026-09-24 阶段持续部署授权”。阶段交付完成、撤回或实质扩大范围时授权终止，不自动扩展到下一阶段。新会话必须核对原始授权和未完成范围。
- 每次仍须双平台 CI、发布树一致、完整可读备份、安装文件及实际 HTTP 验收；保留用户修改、配置权限、调度来源/时间/参数和业务事实。新增表、索引或可空字段必须经过非破坏性迁移测试。
- 代码回滚前必须验证旧版本与当前数据库兼容；新增迁移不构成兼容证明。未经验证不得回退代码。数据库快照恢复还须证明没有覆盖部署后新增事实，否则请求具体恢复确认。
- 研究映射、策略、风险阈值、模型晋级、计划冻结、正式报告和财务事实仍遵守独立具体确认；阶段部署授权不批准这些操作。
- 破坏性迁移、历史修复、更换架构/权限/账户、云迁移/对外暴露、新服务、新通知渠道或接收者、真实试发、开机未登录服务均不在本次授权内；实际休眠或重启测试须与 Ryan 约定执行窗口。自动获取持仓和交易后置。
- 平台审批及凭据限制不得绕过。confirmation token 只保留在当前待确认生命周期，不写入 Git、日志或长期上下文。
- 完整交付条件与例外见 `docs/DEPLOYMENT_AUTHORIZATION_PROPOSAL.md`（已确认规则，非无限期授权）。

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
- 开发测试使用独立检出和临时数据，生产部署遵循上面的阶段范围及例外确认边界。
- Codex 接入不等于 Hermes Cron/通知已迁移，不自动停用旧任务或添加重复调度。

## 背景与候选架构入口
- 接手先读 `docs/PROJECT_BACKGROUND.md` 与唯一实施清单 `docs/LAUNCH_CHECKLIST.md`；实例稳定约定读 Git 排除的 `.codex/instance-background.md`。
- `docs/architecture/value-dca-adaptive-architecture-v1.7-draft.md` 是候选来源，不是已激活策略或模型；旧运行形态服从 Windows Codex + Core + 后台调度。
- HTTP 预览的候选分配不等于平台可执行安排；未知限额、有效期或交易日历必须明确阻断拆单承诺。
- 阶段 B 研究及风险覆盖见 docs/RESEARCH_NOTEBOOK.md；账户取证优先 040046，独立于研究交付。研究草稿不代替原始买入理由或投资规则确认。

- 基准与相对表现入口见 docs/BENCHMARK_RESEARCH.md；官方比较基准与研究映射分开，历史有效期不得追溯套用新权重，研究批准不触发正式风险动作。
