# Value DCA Agent

面向不同用户复用的价值定投 Agent。公共仓库只发布通用策略规则、确定性计算能力和
安全工作流；基金代码、账户、持仓、角色、基准映射及可定投白名单均属于用户本地的
策略实例，不写入公共策略，也不进入 Git 仓库。

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
指数和跟踪该指数的基金是两个独立标的：前者登记为 `INDEX`，后者登记为 `FUND`。
仓库不会为任何真实基金预设角色或定投资格。

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

0.10.2 起，受管理的 `investor_core` 配置会清除旧版交互安装留下的 `tools.include`
白名单，避免新 MCP 工具在升级后被历史配置过滤。工具缺失时，Skill 只报告能力不一致，
不再允许模型替代 Core 推导资金方向、基金拆分或交易草稿。

0.11.0 起，公共策略定义与用户策略实例彻底分离。策略版本只保存通用参数；组合必须显式
绑定策略版本，标的角色、基准映射、投资论点和定投资格保存在该组合自己的实例中。迁移会
保留旧组合的已批准分配参数与角色，但历史持仓默认不获得定投资格，避免公共 Agent 或升级
过程擅自选择具体基金。

周度计划由 Core 确定性拆分到已批准实例白名单中的具体标的。没有合格标的时资金进入保留项，
不会回退到历史持仓、注册标的或模型推荐。计划与交易账本完全分离，状态依次为
`DRAFT`、`FROZEN`、`EXECUTED`，也可进入 `EXPIRED` 或 `SKIPPED`；冻结不代表成交，
只有用户在外部平台真实成交并提供已提交交易记录后，计划才能标记为已执行。

0.12.0 完成大阶段一收口并启动大阶段二。周计划的投后比例只计入真正可执行的候选金额；
保留资金不会虚构仓位变化，含保留或待复核项的计划不能冻结。组合本地的定投资格、代理映射、
投资论点和风险阈值改为 Agent 可用的“草稿—明确确认—提交”流程，默认组合上下文会自动解析，
但任何配置都不得根据净值、持仓或模型观点自动推断。

大阶段二新增来源化 PE/PB 观测、固定方向的历史分位计算、`STRONG/WEAK/NOT_APPLICABLE`
代理约束、确定性规则扫描和卖出建议书。规则只读取当前策略实例中明确批准的阈值；命中只创建
`REVIEW_REQUIRED` 建议书。`APPROVE`、`DEFER`、`REJECT` 都有独立确认记录，其中
`APPROVE` 仍为 `NOT_EXECUTED`，不会创建交易或改变持仓。

0.13.0 完成大阶段二。八类卖出触发和核心工具质量检查均由 Core 读取组合本地的显式规则；
替换、持续跑输、目标完成和工具质量还必须有来源化且已验证的观察记录。卖出建议现在包含赎回费、
资金去向和前后暴露诊断，但仍不代表成交。只有用户在外部平台真实执行后，另行创建并确认关联
`sell_proposal_id` 的 `SELL` 交易，才会改变持仓并将建议标为 `EXECUTED`。该成交同时创建
六个月复盘任务；复盘结果只进入审计记录，不自动修改策略。

0.14.0 启动大阶段三，增加受治理的自动化运行底座。行情同步、风险扫描、周计划草稿、
卖出后到期复盘和系统自检都必须先有组合本地显式批准且启用的自动化策略；未配置或暂停时
只记录 `SKIPPED` 并静默。自动化策略也采用草稿—确认—提交边界，公共仓库不预设用户金额、
投递目标或启用状态。

每个确定性任务以任务名、组合、计划时间和策略版本形成稳定幂等键，并保存心跳、尝试次数、
退避时间和结构化结果。成功结果进入不可变报告事实包，Core 确定性标记 `SILENT` 或
`NOTIFY`；失败进入告警和通知 outbox。`NOTIFY` 只表示等待渠道投递，不代表已经送达。
周计划任务必须使用用户在自动化策略中明确批准的固定新增资金，且只创建 `DRAFT`，不会冻结
计划或创建交易。风险任务命中规则最多生成待复核建议，不会自动批准或卖出。

0.15.0 补齐 Core 策略与 Hermes Cron 之间的调度桥。Core 根据当前活动策略生成唯一的
受管任务清单，Hermes 只对 `value-dca-` 前缀任务执行创建或更新，并在完成后回写实际任务
快照。状态接口会明确区分 `NOT_RECONCILED`、`DRIFT`、`BLOCKED` 和 `IN_SYNC`，因此
“策略已批准”不再被误报成“定时任务已安装”。安装器只把无窗口 Python 桥接脚本复制到当前
Hermes Profile；它不会擅自启用任务、选择基金或填写个人渠道。

脚本不再依赖 `${BUSINESS_DATE}` 或绝对项目路径。Core 会依据已批准策略的时区生成稳定业务
日期；健康检查、行情同步、风险扫描、周计划草稿、卖出复盘和到期重试都走同一受治理入口。
无事件时脚本输出为空，非零退出会保留在 Hermes Cron 历史中。调度快照只证明任务定义和
Gateway 状态一致，不证明渠道消息已经送达。

0.16.0 为本地调度增加确定性的“错过后补跑”。现有 `value-dca-retry-due` 每五分钟先检查
活动策略最近一次应执行的计划窗口，再处理失败重试；因此升级后无需新增第二条恢复 Cron。
计划窗口经过 10 分钟宽限后仍无 Core 运行记录才会被视为漏跑，并且只补最近 7 天内每项策略
的最新一次，避免休眠或长期关机后产生任务风暴。补跑保留原始计划时间、策略版本和幂等身份，
同时兼容 0.15.0 以前按业务日期保存的运行记录。

锁屏不会阻止调度，前提是电脑仍处于唤醒、用户会话和 Hermes Gateway 均在运行。睡眠、休眠、
关机或注销期间本地程序不可能执行；恢复或重新登录后，Gateway 下一次五分钟恢复轮询会检查并
补跑漏掉的最近窗口。补跑只调用既有确定性任务：不会确认草稿、冻结计划、批准卖出建议、提交
交易或启用自动交易。

0.17.0 补齐通知 outbox 的事实边界。待通知内容必须先由外部 Hermes 渠道适配器领取，
领取后状态仅为 `DISPATCHED`，不能称为已送达。每次领取都会生成不可变 attempt 和一次性
回执令牌；只有渠道返回 provider、provider message ID 和匹配令牌后，Core 才记录
`DELIVERED`。失败回执进入 5/15/30/60/120 分钟退避，15 分钟没有回执会记录
`TIMED_OUT` 后重新排队，达到最大次数才进入 `FAILED`。当前 Hermes Cron 的脚本成功和
`origin` stdout 都只属于调度证据；没有渠道回执的 Hermes 版本会保持 `DISPATCHED`，
不会伪造送达。

外部适配器通过以下本地 API 完成严格的两阶段协议：

- `POST /v1/notification-deliveries/claim`：原子领取到期通知并获得 receipt token。
- `POST /v1/notification-deliveries/receipt`：凭渠道证据记录 `DELIVERED` 或 `FAILED`。
- `GET /v1/notification-delivery-attempts`：查看不可变尝试、超时和回执证据。

Investor MCP 只暴露 `automation_delivery_status_list` 与
`automation_delivery_attempt_list` 两个只读工具，Agent 不能领取通知或回写渠道结果。

0.18.0 增加实际 Hermes 渠道执行器。受管的 `value-dca-notification-delivery` 每分钟静默
领取 Core outbox，通过官方 `hermes send` 向明确目标或 Profile 的 home channel 发送，
并把 CLI 退出状态、目标和输出摘要哈希回写为不可变证据。`DELIVERED` 的严格含义是
Hermes 平台适配器接受消息，不代表用户已经阅读；目标无法解析、命令失败或超时都会进入既有
退避重试，不会误报送达，也不会让 Cron stdout 再投递一次。

0.18.1 修复 Windows Hermes Profile 中复制脚本的隔离运行：通知消费者不再依赖项目
虚拟环境中的 `investor_core` 包；便携目标 `origin` 交由 Hermes 使用已配置的平台 home
channel 解析，避免将 Weixin iLink chat ID 误当作地址簿别名。

0.19.0 增加受控通知链路测试：`notification_test_send` 只创建 Core 固定文案的测试消息，
通过真实 outbox、Hermes no-agent 消费者和渠道回执完成端到端验收。测试请求需要精确确认词，
支持幂等重放与 60 秒冷却；渠道会话失效和限流会保留在待重试状态，不会修改持仓、交易或策略。

本版同时增加确定性绩效与周期复盘。`portfolio_performance_get` 从已提交交易和保存的 NAV
计算 Modified Dietz、XIRR，以及满足数据条件时的 TWR；使用策略实例中明确配置的
`benchmark_instrument_id` 计算基准收益和逐标的基准贡献。系统没有闲置现金账本，因此期间
BUY/SELL 明确按外部流入/流出处理，TWR 或基准覆盖不足时返回空值和原因，不做插值或模型补全。

月、季、年复盘作为显式批准的 `MONTHLY_REVIEW`、`QUARTERLY_REVIEW`、`ANNUAL_REVIEW`
自动化策略运行。每次生成不可变绩效快照、带修订号的复盘事实和开放行动项；数据不足时进入
`DATA_BLOCKED`。复盘只记录事实、质量缺口和待人工检查事项，不选择基金、不轮换、不冻结计划、
不批准卖出建议，也不创建交易。

0.20.0 完成大阶段三的剩余底层能力。现金账本采用独立的草稿—确认—提交边界，记录充值、
提现、分红、利息和费用；它不创建投资交易，也不改变持仓。存在完整现金事实时，BUY/SELL
作为账户内部现金移动，绩效使用逐日外部资金流调整后的跨期 TWR；旧数据没有现金账本时仍保留
明确标记的兼容算法，不会合成历史充值或掩盖负现金。

独立官方净值回填使用不可变批次保存来源名称、证据链接、上游谱系、观测时间和逐日净值。
只有 `FUND_MANAGER_OFFICIAL` 或受支持的专业独立谱系可以进入该入口；同日官方数据冲突会
保留为 `SOURCE_ERROR`，不会平均或覆盖。`runtime_mode_get` 由 Core 确定性返回 L0–L3
能力矩阵：L0 为完整事实链，L1 为带质量限制的确定性运行，L2 为账本事实模式，L3 仅保留
安全与健康能力。Agent 不得跨越当前模式补算缺失事实，任何级别都不会启用自动交易。

0.21.0 将产品重心转向复盘和市场发现。`market_research_evidence_record` 以不可变来源、
证据链接、上游谱系和结构化事实保存基金画像、持仓、经理、费率、基准与市场环境资料；
`market_discovery_scan` 只扫描用户明确给出的已注册标的范围，从已保存 NAV 计算 20/60/120
观察期收益、最大回撤、年化波动、时效性和证据覆盖，形成 `OBSERVE`、`REVIEW` 或
`DATA_BLOCKED` 事实包。它不排名基金、不改变定投资格，也不生成买卖交易。

周期复盘现在同时保存期末持仓、基准归因、策略配置完整性、投资论点状态和最近市场发现摘要。
复盘行动项可通过独立草稿与精确确认令牌进入 `ACKNOWLEDGED` 或 `RESOLVED`，历史复盘和决定
均不可变。新增 `WEEKLY_MARKET_DISCOVERY` 自动化任务，但候选范围必须由用户实例明确配置；
公共 Agent 不内置个人基金、热门榜单或默认推荐。

0.22.0 补齐连续观察能力。每次可比的市场发现会与同一组合、同一显式标的范围和同一观察
窗口的上一期结果对比，保存状态迁移、新增/消失的复核标记、研究证据与验证覆盖变化以及
指标差值。`market_discovery_change_list` 可只读取需要关注的事实变化；普通收益与波动变化
不会被擅自解释为买卖信号。

`review_trend_snapshot_build` 将多个不可变月度、季度或年度复盘串联起来，汇总数据质量连续性、
绩效序列、基准与策略治理覆盖、反复出现的行动项、未完成行动积压及其账龄。趋势快照同样
不可变且不生成投资建议。周度市场发现自动化现在只在首次受限事实或状态、标记、证据和验证
覆盖发生变化时请求通知，避免把正常的每日数值漂移包装成告警。

0.23.0 将市场发现推进到完整的人工治理生命周期。每个组合可以通过确认式草稿维护自己的
研究观察池，状态限定为 `CANDIDATE`、`OBSERVING`、`REVIEW_DUE`、`ADOPTED`、
`REJECTED`、`ARCHIVED`；其中 `ADOPTED` 仅表示研究结论被组合所有者接受，绝不自动加入
策略、开启定投或创建交易。公共安装仍不预置任何标的或默认候选。

同一标的、证据类型与来源谱系的新研究事实会和上一份可比证据进行结构化对比，保存新增、
删除和变化字段路径。来源内容变化只是可审计事实，不是行情信号。已解决的复盘行动可以在
二次确认后记录 `COMPLETED`、`PARTIAL`、`NOT_COMPLETED` 或 `NOT_APPLICABLE` 结果以及
证据质量；跨期趋势据此报告结果覆盖率、缺失结果数量、关闭耗时和证据质量分布，但不生成
策略评分、因果解释或自动调参。

同一版本同时收紧风险扫描的诚实性契约。候选规则现在明确区分 `HIT`、
`EVALUATED_NOT_HIT`、`NOT_CONFIGURED`、`DATA_UNAVAILABLE`、`NOT_APPLICABLE` 和
`EXEMPT`，不再把缺少阈值或证据记录成安全的 `NOT_HIT`。组合简报从当前策略实例生成逐标的
配置覆盖，风险扫描默认只返回紧凑摘要；完整不可变规则事实保留在 Core，并通过
`risk_rule_hit_list` 分页、过滤和按需展开。自动化输出也分别保存执行成功与结果数据质量，
避免将单源 `WARNING` 误解为任务执行失败。

0.24.0 把观察池从人工状态记录推进到周期复核。观察条目保存当前观察周期开始时间和最近
完成复核时间；`research_watchlist_review_snapshot_build` 按指定日期生成不可变事实包，
明确区分 `DUE`、`UPCOMING`、`NOT_SCHEDULED` 和 `CLOSED`，并关联最近市场发现、研究证据
覆盖、观察天数和数据质量缺口。到期事实不会自动把条目改成 `REVIEW_DUE`；任何观察池状态
变化仍需用户通过草稿和确认令牌完成。

新增受治理的 `WATCHLIST_REVIEW_DUE` 自动化任务。它只生成到期复核事实包，有到期条目时
才请求通知，不排名、不采用标的、不修改策略、不创建计划、提案或交易。同时修正风险扫描
执行状态契约：成功运行返回 `execution_status=SUCCESS`，投资执行边界单独返回
`trade_execution_status=NOT_EXECUTED`，避免把“没有交易”误解为“没有执行扫描”。
`research_source_contract_get` 同时公开外部研究适配器的字段、来源谱系、幂等和验证边界；
公共安装不预置数据源、标的范围或自动采集任务，连接器只能把真实来源事实交给既有不可变
研究证据入口。

0.25.0 将外部研究接入从单条事实推进到可审计采集运行。外部插件先读取
`research_source_contract_get`，再通过 `research_collection_run_record` 提交一个精确批次；
Core 保存连接器标识、适配器版本、来源谱系、起止时间、清单哈希，以及每条资料的
`RECORDED`、`REPLAYED` 或 `REJECTED` 结果。批次允许部分成功并保留稳定错误码，不能把模型
生成内容伪装成来源资料，也不授权公共 Agent 自动抓取、内置候选、排名或交易。

本版同时增加 `review_quality_snapshot_build`。它从不可变周期复盘、行动决定与结果、研究
采集运行和策略实例时间线生成复盘质量事实，分别报告复盘连续性、数据质量、行动闭环覆盖、
研究来源可追溯性，以及策略实例下完整覆盖的历史复盘。策略上下文只做时间关联，明确标记为
非因果观察；系统不计算“策略得分”，不判断参数优劣，也不自动调参。受治理的
`REVIEW_QUALITY_SNAPSHOT` 自动化只在数据阻塞时请求通知，不改变策略、计划、提案或交易。

0.26.0 将市场发现的来源能力和证据缺口纳入组合本地治理。公共安装仍然不捆绑连接器、
凭证或默认来源；用户可以通过 `research_source_config_draft_create` 创建精确草稿，配置连接器
标识、可提供的证据类型、上游来源谱系和可选的凭证环境变量名。草稿经明确确认后才成为当前
配置，历史版本保持不可变。Core 永远不保存密钥值，也不会因为配置已启用就自动执行抓取。

`research_coverage_snapshot_build` 对用户明确给出的已注册标的、所需证据类型和最大资料年龄
生成不可变覆盖审计，逐项区分 `CURRENT`、`STALE` 和 `MISSING`，并把补采状态区分为
`READY`、`BLOCKED_NO_CONNECTOR` 和 `NOT_NEEDED`。输出中的 collection tasks 只是交给外部
适配器的有界任务描述，不代表任务已经运行；真实采集结果仍须通过
`research_collection_run_record` 回写逐项结果。多个来源谱系仅证明记录了不同谱系，不能自动
证明来源独立或事实正确。

新增受治理的 `RESEARCH_COVERAGE_AUDIT` 自动化。它只对显式配置的标的范围、证据类型和
时效阈值生成覆盖事实包，仅在缺口因没有已批准连接器而阻塞时请求通知；不会启动连接器、
改变观察池、选择基金、调整策略或创建交易。

CLI 仍保留为恢复和诊断入口：

```bash
uv run investor setup init --portfolio-name "个人投资组合" --account-name "默认账户" --platform "支付宝"
uv run investor instrument add FUND001 --name "示例基金" --asset-type FUND
uv run investor strategy assign --portfolio-id <PORTFOLIO_ID> --strategy-key value-dca --strategy-version 1.6 --approved-by <OPERATOR> --reason "显式采用该策略"
uv run investor strategy instrument-configure --portfolio-id <PORTFOLIO_ID> --instrument-code FUND001 --role CORE --contribution-eligible --approved-by <OPERATOR> --reason "批准为本组合定投候选"
```

策略发布、组合绑定和目标比例仍属于受保护的 CLI/运维配置。组合本地的基准映射、代理适用性、
投资论点、定投资格和已支持风险阈值可由 Agent 创建精确草稿，但必须由用户明确确认后提交。

之后可通过 Hermes 使用 `weekly_plan_preview` 预览、使用 `weekly_plan_draft_create` 保存
计划；也可使用 `transaction_draft_create` 记录真实外部成交。计划确认和交易确认是两套
独立边界，任何计划状态都不会自动写入交易账本。

## 当前边界

- `/health` 只验证进程存活；`/ready` 同时验证 SQLite、WAL 和当前迁移版本。
- `investor db migrate` 与 `alembic upgrade head` 使用同一迁移链。
- MCP 按只读、草稿写入和确认写入分级；`OPENING` 是旧持仓基线，`TRADE` 才代表用户在
  外部平台完成的真实交易。
- 公共策略不包含用户标的；注册过、持有过或被标记角色的基金均不会因此自动成为定投标的。
- 周计划只能使用当前组合策略实例中显式批准且 `contribution_eligible=true` 的标的。
- 估值分位只来自已保存的指数 PE/PB 证据；`NOT_APPLICABLE` 不计算，`WEAK` 不能单独触发卖出。
- 风险扫描和卖出建议不执行交易；批准建议也不改变持仓。
- Windows 计划任务只管理 Core 进程和版本更新，不调用任何投决或交易写入工具。
- Hermes Cron 不是 Core 的唯一 supervisor；`core-health-watch` 模板仅用于后续异常通知。
- `skills/value-dca-investor` 是 Hermes Profile 的项目源文件，不是独立交易系统。
- `cron/` 中的示例任务默认禁用；实际定义必须来自当前组合活动策略生成的调度清单。
- 自动化策略、Cron 定义和渠道投递是三层状态；只有活动策略与当前 Hermes 快照均为
  `IN_SYNC` 才能声称业务任务已调度。渠道送达仍需独立回执，适配器接受不等于用户已读。
- 绩效使用当前无现金账本边界下的外部资金流约定；不可用指标保持为空，不能由 Agent 补算。
- 周期复盘及其行动项不属于交易建议，也不会改变策略、计划、提案或持仓。

## 后续开发顺序

1. 继续深化复盘：跨策略实例的可比期间约束、人工决定结果长期对照和质量连续性。
2. 继续深化市场发现：采集任务认领/回执、连接器运行健康与覆盖变化闭环。
3. 大阶段四：通用初始化向导、干净安装验收、备份恢复和迁移工具。
4. 大阶段四收口：14 天连续运行验收、公共分发文档和 V1 发布候选。
