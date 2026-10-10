> v0.41.0：医疗主题同类比较见 [PEER_RESEARCH](PEER_RESEARCH.md)，使用 `peers --anchor 003096`，详情增加 `--code 009163 --details`。仅只读归档研究，默认说明未获取新证据。

> v0.40.0：默认“复核我的持仓”改为首次基线差异复核。当前入口、显式观察保存和限制见 [BASELINE_DELTA_REVIEW](BASELINE_DELTA_REVIEW.md)。以下旧版描述保留历史；完整当前报告使用 holdings-research-current。

# Codex 日常投资操作入口（v0.33.0）

Core 是唯一账本和业务规则来源。Codex 负责识别意图、补齐用户事实、调用下列本机 HTTP 入口并用自然语言展示。HTTP 已可用时不等待 MCP；不重建账本，不运行数据库 CLI 代替查询。

## 固定入口

Windows 已安装目录中运行 `.venv\Scripts\investor-assistant.exe`；或同一 Python 运行 `-m investor_core.daily_client`。默认 Core 为 `http://127.0.0.1:8710`，没有数据库路径参数、自动启动、升级或同步行为。

| 用户说法 | 固定命令/流程 | 返回内容与边界 |
|---|---|---|
| 看看我的投资 | `investment` | 持仓、成本、市值、未实现盈亏、当前策略舱位、每只净值日期/质量及工作台待办 |
| 本周怎么投 | `plan --budget 用户明确金额`，同时 `week` | 复用批准策略、在途和未完成承诺、资格及配置中的限额；不生成草稿或冻结 |
| 我已经买了 | `purchase-draft` → `purchase-commit` | 记录已在外部提交的真实申购，尚不改变份额；需确认记录 |
| 确认份额 | `shares-draft` → `purchase-commit` → `transaction-draft` → `transaction-commit` | 分别核对平台确认与毛额记账；每次财务提交都需确认记录 |
| 这周不投 | 无计划用 `no-investment-draft`，已有计划用 `skip-draft` | 先预览准确周期、原预算、零执行/放弃原因和不结转，再确认提交 |
| 结束本周 | `close-draft` → `close-commit` | 已有部分执行与未执行金额分别保留，未执行不结转 |
| 看看周报 | `week`、`inspect report-draft 标识` | 先查看已有草稿/正式报告，避免重复创建；零投入不代表零收益 |
| 系统是否正常 | `system --since YYYY-MM-DD` | 近 31 天内应执行时刻、真实记录、来源、重复、重试、历史异常和行情状态 |
| 医疗板块有什么机会 | `research --topic 医疗` | 只读现有快照与批准资格；没有行业/事件证据时明确受限，不制造上涨原因 |

默认输出中文；`--json` 放在子命令前用于 Codex 内部结构化核对，不原样展示给用户。`context` 读取已保存默认组合；缺失时仅解析唯一有效组合/账户，不保存配置。有歧义或已保存上下文失效时停止，展示候选名称并询问，不擅自挑第一个。用户选定后由 Codex 用已读取的标识传入全局 `--portfolio` 和 `--account`，仅本次读取，不保存默认配置。

Core `display_text` 原样保留。查询日期不等于净值日期，单源/过期/缺失的 WARNING 必须展示。收益为持仓估值口径，不宣称完整已实现收益或投资回报保证。计划预览不是平台委托，配置中未记录的实时限购仍须用户核对平台。

## 受治理写入（Codex 使用，用户不复制标识）

1. 使用 `schema 操作名` 从正在运行的 Core 读取准确必填字段。标识由前一步真实响应取得，预算、日期、基金、金额、份额、费用和真实平台事实由用户提供，不能猜测。
2. `workflow 操作名 --subject 当前对象` 从标准输入接收 JSON；交易记账提交还需 `--parent 份额确认对象`。正文和确认令牌不得放命令行、Git、日志、长期上下文或临时持久文件。通过当前内存子进程 stdin 传入。
3. 草稿操作只表示业务意图。展示现值、拟议值、业务影响、资金/持仓变化和不会发生的操作，再请求一次明确确认。普通配置/计划/报告提交对应 `--confirmation 确认`；申购事实、份额确认和交易记账对应 `--confirmation 确认记录`。这些参数只能由当前用户确切回复映射，不能由助手主动补齐授权。
4. 提交使用当前草稿返回的确认凭据及对象，禁止混用其他草稿。草稿过期先读当前事实；若内容不变且无漂移，可按 Core 现有续签流程处理，但仍重新展示并再次确认，不复用过期授权。客户端不自动续签或提交。
5. 写入前在用户本地 `InvestorCore/http-request-journal.sqlite3` 保存请求指纹，内容和凭据不入日志。失败、超时、响应丢失或进程崩溃均保持结果不明；同一请求不能由另一个进程盲目重发。先 `inspect` 对应对象，并通过 `week`/既有只读列表核对原幂等意图。不要换幂等键、删日志或创建第二笔来“试一下”。Core 自身仍为原子幂等的最终关卡。
6. 一项计划可关联多笔不同真实申购：沿用同一计划标识，每笔外部事实使用自身固定幂等标识与平台参考；确认和记账逐笔绑定原申购。不得把同一笔响应丢失当作第二笔购买。记账毛额包含确认净额和费用，复用 Core 的一致性检查。
7. 查询或预览遇到 HTTP 错误时显示具体状态；不借机启动服务、更新行情、切换调度、修复数据或提交其他操作。遇到不支持流程使用 Core 已有受治理接口，不开放任意 POST 快捷通道。

## 机会解释契约

事实必须带截至日期、原始来源和质量；推断单独标注。基金名称匹配只是候选相关性，不是精确行业穿透，混合基金全部市值不得直接算作医疗暴露。净值变化不能证明某板块上涨，更不能证明新闻因果。

首版研究不新增庞大抓取系统。现有证据不足时列出：板块定义及区间、指数行情、事件原文及发布时间、基金行业持仓、独立验证；不给捏造的驱动因素。当前批准定投资格为 false 则明确不能获得直接定投资金；为 true 也必须经预算预览、舱位、在途、限购及信号条件。任何基金池、映射、阈值或策略改动另走草稿确认。

## 运维边界

Windows 持续调度与 Codex 对话独立；历史离线日志不等于当前仍断线，心跳正常也不等于行情完整或通知送达。禁用政策/历史变更、切换前时刻、截断记录须另核，不能按当前政策重建结果直接宣布历史漏跑。通知投递、开机未登录、真实休眠和长期稳定性仍属后续工作。

## v0.33.1 接手增补
- `plan` 中金额仅为当前批准策略的候选分配。`execution_assessment` 明确返回约束未知，策略上下限不是平台单日限制。未知时无拆单日历、无预计交易日数，也不假设 QDII 为 T+1。
- `research` 使用 Core `/v1/research-diagnosis`；旧论点状态不代表有版本化持有理由。未读取到的收益来源、反证、模型和相对诊断必须保留缺失。
- 软件、当前策略、候选架构与模型版本分开显示；候选 v1.7 未激活。
- 按 `PROJECT_BACKGROUND.md` 和唯一 `LAUNCH_CHECKLIST.md` 继续，不重做已完成工程。


执行证据、账户约束确认、准确周期及拆单入口见 [EXECUTION_EVIDENCE](EXECUTION_EVIDENCE.md)。公开归档不等于账户适用或策略批准；计划显示候选、已核实、未核实与本期不可执行金额。
`case --code <基金代码>` 与 `risk-coverage` 为只读入口；版本化研究与决策日志见 [RESEARCH_NOTEBOOK](RESEARCH_NOTEBOOK.md)。

## 基准与相对表现

“这只基金跑赢基准了吗”：先运行 `investor-assistant benchmark --code <代码>`，复用归档结果和来源。按窗口展示净值总收益、历史口径基准、差额与警告。没有数据时查缺口，不以聊天记忆填数。映射的具体确认流程见 [BENCHMARK_RESEARCH](BENCHMARK_RESEARCH.md)。


## 组合研究与通知可见性

“哪些持仓值得复核”使用 `investor-assistant review`；“看看我的投资”已同时包含研究摘要。解释观察日期、批准状态和来源质量，历史相对收益不触发买卖。

“通知是否送达”使用 `investor-assistant notifications`；平台接受、进程执行和用户收件是不同证据，不把任务执行成功称为消息送达。


## 论点与日常复盘

“看看持仓论点/为什么持有”用 `thesis --code <基金代码>`，展示已确认、新候选、支持/反证、变化和缺口。`review` 与 `investment` 同步读取这些状态及原研究窗口。先保留历史买入理由未知；当前新论点不是历史解释。

用户表达修改研究意图后，可经 `research-case-draft` 建新版本，再 `thesis-draft` 生成具体内容确认；展示当前值、拟议版本/解释、证据有效期依据及不改变资金的边界。用户明确回复“确认”后才允许 `thesis-confirm`。未确认映射和论点不因软件部署而生效。观察事实用 `thesis-observation` 追加，失效观察仅触发研究复核；不能自动停投、卖出或调整金额。完整合同见 RESEARCH_NOTEBOOK。


## D1 持仓比较与复核

“复核我的持仓”使用 `holdings-review`，一次展示比较资格、证据原因、论点及相对上次保存的变化；查询不保存待办。“查看复核历史”使用 `review-history`。需保存时先读取当前输入，再用 `review-capture`；处理用 `review-handle` 记录具体说明，不能视为批准论点、映射或投资。响应未知先回读历史。详见 [HOLDING_REVIEW](HOLDING_REVIEW.md)。

D1使用补充：`holdings-review` 默认逐基金一行，`holdings-review --details` 查看Core提供的所有窗口、原因和来源。`review-baseline-preview` 仅预览首次观察基线或与已有基线比较；本阶段不自动调用保存/处理工作流。批准保存观察也不代表确认问题已处理。

## 比较结论验证
“验证003096与009163的比较结论”调用 `investor-assistant peer-validation --anchor 003096`；详情追加 `--details`，或GET `/v1/peer-comparison-validation?anchor_code=003096&view=DETAIL`。显示Core原文，不在客户端重算；查询不采集或归档。说明研究截止与本次未获取新证据，保留渠道非独立与历史警告。方法及真实案例见 docs/PEER_COMPARISON_VALIDATION.md。


## 组合研究覆盖

“复核我的组合，哪些最值得进一步研究？”执行 `investor-assistant portfolio-research`；“查看某基金研究覆盖详情”增加 `--code <代码> --details`。该入口动态读取当前持仓，不替代原“复核我的持仓”的基线差异语义。先使用Core `display_text`，不在客户端重算结论。医疗继续 `peers --anchor 003096` / `peer-validation --anchor 003096`；A500联接比较使用 `peers --anchor 022463`。所有查询仅复用归档，明确说明没有取得新资料。完整口径见 PORTFOLIO_RESEARCH_COVERAGE.md。

## 主动研究更新（v0.44.0）

“检查哪些研究需要更新”调用 `research-update-check`，只读不联网。
“更新研究，告诉我有什么变化”调用 `research-update`，执行公开资料更新，仅两既有案例；这是公开研究写入意图，不是投资配置批准。
“查看上次研究更新结果”调用 `research-update-last`，只读回看成功范围与失败。

研究更新发生响应丢失时先回读原请求，客户端保留标识；不换键重发。需要恢复中断时沿原意图 `--resume`，不要求Ryan处理标识。新研究不自动审批或处理D1。范围、部分完成、来源限制和恢复方式见[RESEARCH_UPDATES](RESEARCH_UPDATES.md)。


v0.44.2：研究入口同步显示当前归档证据判断及精确分红适用区间。历史研究查询保留当时原文，明确标为非当前结论，另列最新判断；不将旧UNRESOLVED状态套给当前研究。非跨争议日窗口不受本条影响，其他独立警告保留；查询不采集、不写入、不批准。


## R1.2医疗H研究隔离候选（未部署）

自然语言“查看医疗影子研究”映射到 `investor-assistant --core-url <临时回环地址> medical-shadow`，也支持中文同名子命令；`--details`展开来源。不得将未部署命令默认指向生产8710后宣称可用。Codex保管地址和原件路径，不要求用户复制技术标识。

隔离运行准备一个本地JSON包：`base_input`为既有真实R1.2输入，`original_paths`为已审阅manifest的原件key到路径映射（相对路径以包所在目录为准）。不将包、原件或私人路径提交Git。设置`INVESTOR_MEDICAL_H_PACKET`为该包的绝对路径，然后在临时回环端口启动：

```powershell
python -m uvicorn investor_core.api.medical_shadow:create_app --factory --host 127.0.0.1 --port <临时端口>
investor-assistant --core-url http://127.0.0.1:<临时端口> medical-shadow
investor-assistant --core-url http://127.0.0.1:<临时端口> medical-shadow --details
```

GET `/v1/medical-shadow-research?view=SUMMARY|DETAIL`只读。独立工厂不加载生产配置、不创建业务服务、不连接数据库，无写入路由；用完停止临时进程。无外部采集，不保存新观察、不触发前瞻。不是常驻服务，也不是生产安装。

本次重新校验本地原件并重算历史H观察，不将查询日期当净值日期；来源/指纹不可用则明确BLOCKED，不退回历史保存结果。保留003096历史方法范围缺口与009163自身缺口，缺失不填零、不排名。数字及中文展示由Core模块统一生成，客户端不计算。详细日期/适用范围与资料指纹按需展开。真实使用证据和剩余限制仍维护原MEDICAL_D_ONCE_FEASIBILITY报告。


### v0.47.0干净环境安装与路径契约（源码发布，不部署生产）

使用Python 3.11及uv，从v0.47.0源码归档解压到任意目录，执行：

```powershell
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
uv build --wheel
uv venv .isolated-env --python 3.11
uv pip sync requirements.txt --python .isolated-env/Scripts/python.exe
uv pip install --no-deps --python .isolated-env/Scripts/python.exe dist/value_dca_agent-0.47.0-py3-none-any.whl
```

Linux对应解释器/命令位于`.isolated-env/bin/`。也可将构建后的wheel和锁定依赖清单送入独立环境；不是editable安装，不要求开发checkout在sys.path。

原件由操作者单独提供，发布包不含私有原件或完整真实输入包。沿用已有核验输入，不手工补造必需字段。目录示例为`<研究目录>/packet.json`及`<研究目录>/originals/<原件key>`。packet根必须是对象，包含`base_input`（既有真实R1.2输入对象）和`original_paths`（原件key到路径的对象）；原件key与打包的`r12_003096_archive.json`中documents逐项对应，SHA必须一致。相对路径一律按packet所在目录解析，与启动目录无关；可整体搬迁。没有真实输入/原件时只能验证明确阻断，不宣称真实研究重算。

启动前显式配置，不修改生产.env；以下端口仅作隔离示例，使用前确认空闲：

```powershell
$env:INVESTOR_MEDICAL_H_PACKET = (Resolve-Path '<研究目录>/packet.json').Path
.isolated-env/Scripts/python.exe -m uvicorn investor_core.api.medical_shadow:create_app --factory --host 127.0.0.1 --port 18747
# 在第二终端运行，使用同一隔离环境：
.isolated-env/Scripts/investor-assistant.exe --core-url http://127.0.0.1:18747 medical-shadow
```

Linux使用`export INVESTOR_MEDICAL_H_PACKET=/absolute/path/packet.json`及bin目录命令。默认未配置、配置损坏、原件缺失/不匹配分别阻断，不尝试开发机目录、不下载原件、不读取保存结果冒充重算。完成后停止临时进程；不注册服务或调度。
