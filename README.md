# Value DCA Agent

个人价值定投 Agent 系统的 V1 工程实现。当前仓库处于 Phase 2：在受控账本基础上
增加 canary 准入的基金净值同步、可审计净值快照、数据质量分级和确定性持仓估值，
同时保留交易草稿、显式确认、幂等提交、冲正、持仓重建和受控 MCP 工具。

系统只做研究、计划、记录和复盘，不连接交易接口，也不自动确认金融操作。

## 本地启动

目标生产环境使用 Python 3.11。开发环境临时允许 Python 3.12，但 `investor doctor`
会显示版本降级提示。

```bash
uv sync --python 3.11
uv run alembic upgrade head
uv run investor doctor
uv run investor-core
```

Windows 不再要求用户下载和解压版本包。首次安装或从旧版切换到 GitHub 管理版本时，
在 PowerShell 运行下面的一条命令；引导脚本只安装 GitHub 上标记为 stable 的正式 Release：

```powershell
$p="$env:TEMP\value-dca-bootstrap.ps1"; irm https://raw.githubusercontent.com/Mr-Monologue/Agent_CompoundInterest_Plan/main/bootstrap-windows.ps1 -OutFile $p; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p
```

安装器会升级 `C:\investor\value-dca-agent`，保留已有数据库，并完成 uv/Python、依赖、迁移、
doctor、Hermes Profile、Skill、MCP 注册和健康检查。无人值守升级只会终止本项目自己的
`investor-core`/`investor-mcp` 进程，不关闭 Hermes；下一次工具调用会自动重连。

安装器会创建当前用户的 `ValueDCAInvestorCore` Windows 计划任务。Core 在登录后通过
Windows GUI 子系统宿主静默运行，不创建 PowerShell 控制台窗口；运行器会在 Core 退出后
自动重启。Hermes MCP 在一次
调用发现 Core 不可用时，也会启动该任务、等待 `/ready` 通过并重试原调用。Core 自己维护
滚动日志 `logs\investor-core.log`，托管器生命周期写入 `logs\investor-core-supervisor.log`。
Cron、微信和券商连接仍保持禁用。

安装器同时创建 `ValueDCAAgentUpdate` 隐藏计划任务，每天 04:00 检查 GitHub 最新正式
Release，错过运行时间时在下次开机后补跑。升级前会创建 SQLite 一致性备份和代码回滚快照，
随后依次执行依赖锁定安装、数据库迁移、doctor 和 Core 就绪检查；任一步失败都会尝试恢复
旧代码与旧数据库。自动升级只读取 Release，不直接跟随 `main` 分支。涉及投资规则、确认
边界或不兼容迁移的版本必须在 `release-manifest.json` 标记为需要人工批准。

仓库 CI 对 pull request 和受控分支执行只读测试。版本从 `develop` 合入长期 `release`
分支验证，发布时创建 `v*` 标签，再将 `release` 合入受保护的 `main`。客户端只会看到并
安装正式 GitHub Release，不跟随普通分支提交。

Core 默认监听 `127.0.0.1:8710`：

```bash
curl http://127.0.0.1:8710/health
curl http://127.0.0.1:8710/ready
```

运行测试和静态检查：

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

## Phase 1 业务设置与期初持仓

Hermes 0.3.0 起可通过 `portfolio_create`、`account_create`、`instrument_create` 完成首次
配置；这些工具是幂等配置写入，不移动资金，也不改变持仓。`INDEX` 类型只用于基准与估值，
不能创建交易草稿。实际成交记录必须使用支付宝或其他平台展示的具体基金产品代码。
例如，中证A500的指数代码 `000510` 应登记为 `INDEX`，而富国中证A500ETF发起式
联接A `022463` 应登记为 `FUND`。

Hermes 0.4.0 起，旧持仓使用 `opening_position_draft_create` 创建期初导入草稿。Core 只接收
平台显示的截止日期、总份额与持仓成本，并确定性推导平均成本；草稿不会改变持仓，也不是
`BUY` 交易。用户核对预览并明确确认后，必须使用 `opening_position_draft_commit` 才能写入
`OPENING` 账本事件。一个账户中的同一标的只能在没有其他有效账本事件时导入期初持仓。

0.4.1 起，成本依据必须二选一：平台显示总持仓成本时传 `cost_amount`；平台只显示每份
成本价时传 `average_cost_nav`。后一种情况下系统使用总份额换算账面成本，并按人民币分位
四舍五入；面向用户统一显示为“账面成本（按平台显示的份额和成本价换算）”。

0.4.2 将 IANA `tzdata` 作为 Windows 正式运行依赖，并把 `Asia/Shanghai` 可用性纳入
doctor/readiness。安装器通过锁文件自动安装依赖，不应让 Hermes 临时执行 `pip install`。

0.5.0 起，GitHub Release 成为唯一发布源；Windows 引导安装、每日自动检查、升级前数据库
备份、失败回滚和发布清单策略均进入正式运行契约。个人数据库、`.env`、日志和确认令牌永远
不进入 Git 仓库。

0.5.1 起，Windows Core 和升级器通过无控制台 GUI 宿主运行；Hermes 使用持久化默认投资
上下文自动解析组合与账户。单组合、单账户场景会自动选中，用户无需查看、记忆或重复填写
UUID；只有出现多个候选时才按名称和平台选择一次。

0.5.2 起，升级器会把安装器完整输出写入 `logs\updater.log`，失败回滚会强制重建本地包
入口并恢复计划任务定义。Hermes MCP 改为 Python 模块启动，避免其控制台入口在 Windows
升级时被占用；若只缺少 `investor-core.exe`，下一次 MCP 调用会先修复当前锁定版本的运行
环境，再启动 Core。该自愈只恢复已安装版本，不下载新版本，也不改数据库或持仓。

0.5.3 起，安装器输出通过独立文件捕获，兼容 Windows PowerShell 5.1 将 uv 进度写到
stderr 的行为，不再把正常的 `Resolved ... packages` 信息当作终止错误。升级任务自身正在
运行时，新的无控制台任务定义由独立 finalizer 在当前任务退出后安装，避免自更新冲突。

0.6.0 起，Core 支持不可变的基金净值快照。每条记录保留净值日期、采集时间、来源类型、
来源名称、验证状态、来源引用和内容哈希。`portfolio_valuation_get` 只用已提交份额和已存储
净值确定性计算市值、持有盈亏、收益率及市值权重；任一非零持仓缺少净值或净值超过允许
新鲜度时，组合质量为 `SOURCE_ERROR`，总市值和组合金额结论保持为空。

0.7.0 起，Core 增加锁定版本的 AKShare 开放式基金净值适配器。每次同步前必须先通过
真实函数和字段契约 canary；同步结果保存 provider/library/contract 版本、原始观测摘要哈希、
逐标的结果和运行状态。当前该来源仍按单一聚合源处理为 `WARNING`，不会自动升级为
`VERIFIED`；任一标的失败时同步批次降级为 `SOURCE_ERROR`，且不会为失败标的填充数值。
同步工具默认解析已保存的投资上下文和当前持仓，用户无需提供 UUID 或重复输入基金代码。
若当前 Hermes 会话还连接了 Wind 等专业数据能力或官方来源，Agent 会把其工具返回的同日
净值及证据引用交给 Core 复核。完全一致时记录不可变的 `MATCH` 关系并把该标的估值质量提升
为 `PASS`；不同值记录为 `CONFLICT`，组合质量立即降为 `SOURCE_ERROR`，金额汇总保持为空。
仓库不复制或捆绑任何专业数据供应商的专有实现、凭证或数据。

0.7.1 起，Windows 行情预算默认提高为 60 秒。主适配器直接请求与 AKShare 相同的东方财富
公开基金载荷，限制响应体大小，并只解析净值序列而不执行远端 JavaScript；网络超时由 HTTP
客户端强制结束。Canary 和逐基金结果同时记录下载、解析耗时，便于区分网络、载荷与解析故障。

0.7.2 起，组合概览统一使用 Core 的确定性 `portfolio_brief_get`。返回值明确声明分配目标、
风险规则、卖出规则、周计划和角色修改能力是否可用；未配置或未实现时固定返回
`NOT_AVAILABLE` 及原因码，Agent 不得自行判断“失衡”、触发卖出或建议定投。净值证据同时记录
上游发布方血缘；AKShare、东方财富和天天基金统一属于 `EASTMONEY`，不能互相充当独立验证源。

0.8.0 起，组合概览包含 Core 生成的 `display_text`，Hermes 必须原样返回，不能追加配置评价、
收益形容词、优先级或建议。`instrument_role_update` 支持用户明确指定后的角色修正，并以
`expected_current_role` 防止旧会话覆盖新值；每次实际变更写入审计事件。

0.9.0 起，每个组合保存带版本、审批人、哈希和审计事件的 CORE/SATELLITE 分配策略。
已批准的 v1.6 默认策略为 CORE 65%、SATELLITE 35%，正常容差 10 个百分点，偏离超过
15 个百分点时进入 `TRANSITION_REQUIRED`。组合概览由 Core 确定性输出实际占比、偏离和
过渡状态；过渡只声明“优先使用新增资金、不自动卖出”，不会计算申购金额或提交交易。
金额负号统一显示为 `-¥`，已知市场数据限制统一使用中文。

0.10.0 起，用户明确提供本周新增资金后，`weekly_plan_preview` 会按当前确定性估值和
版本化配置计算 CORE/SATELLITE 舱位资金方向与投后比例。预览只到舱位级，不选择具体基金、
不创建交易草稿、不代表成交；估值不可用或仍有未分配角色时拒绝输出金额分配。

0.10.1 起，Windows 自动更新完成后会在 Hermes Gateway 原本正在运行时执行一次受控
重启，使新版本新增的 Investor MCP 工具对新会话立即可见；未运行的 Gateway 不会被
更新器擅自启动。

CLI 仍保留为恢复和诊断入口：

```bash
uv run investor setup init --portfolio-name "个人投资组合" --account-name "默认账户" --platform "支付宝"
uv run investor instrument add FUND001 --name "示例基金" --asset-type FUND --role CORE
```

之后可通过 Hermes 使用 `transaction_draft_create` 生成真实外部成交的限时记录草稿。只有用户明确提供
该草稿的一次性确认令牌后，`transaction_draft_commit` 才会写入本地账本并重建持仓。

## 当前边界

- `/health` 只验证进程存活；`/ready` 同时验证 SQLite、WAL 和 Phase 2 同步迁移版本。
- `investor db migrate` 与 `alembic upgrade head` 使用同一迁移链。
- MCP 按只读、草稿写入和确认写入分级；`OPENING` 是旧持仓基线，`TRADE` 才代表用户在
  外部平台完成的真实交易。
- Windows 计划任务只管理 Core 进程，不调用任何投决或交易写入工具。
- Hermes Cron 不是 Core 的唯一 supervisor；`core-health-watch` 模板仅用于后续异常通知。
- `skills/value-dca-investor` 是 Hermes Profile 的项目源文件，不是独立交易系统。
- `cron/` 中的任务默认禁用，必须先在目标 Hermes 版本上验证字段契约。

## 后续开发顺序

1. Phase 2 后续：第二校验源或官方回填、连续交易日 canary 和同步 Cron。
2. Phase 3：风险、指数估值和周计划。
3. Phase 4 以后：观察池、重检、卖出建议、组合过渡、绩效和复盘。
