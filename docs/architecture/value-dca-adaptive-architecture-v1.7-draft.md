# Value DCA Agent 自适应纪律架构 — v1.7 候选实施规格

> 文档版本：`architecture-v1.7.0-draft.2`  
> 编制日期：2026-08-06  
> 当前生产策略：`Value DCA v1.6`（保持有效，不因本文自动变更）  
> 候选策略：`Value DCA v1.7`（完成验证和用户确认后方可激活）  
> 软件发布关系：本文不指定或限制软件版本；能力实际落点以实施时的仓库审计和发布计划为准  
> 运行形态：常开本地主机、单用户先行、Hermes `investor` Profile  
> 系统定位：投资研究、计划、记录、执行跟踪与复盘助手；不连接交易执行接口  
> 文档状态：候选架构基线，可用于拆分开发任务；不是生产策略授权

---

## 0. 本次升级的结论

本次升级是必要的，但必须明确：它不是给原有定投公式增加几个参数，而是为系统增加一套“理解资产、理解环境、识别不确定性、检验投资理由”的能力。

升级后系统由四层组成：

1. **事实与账本层**：交易、持仓、订单、净值、估值、数据质量和计划执行进度。
2. **战略定投层**：继续执行已批准的 CORE/SATELLITE、预算、准入和风险规则。
3. **研究与诊断层**：资产收益来源、投资论点、市场季节、相对表现、候选排位和反证研究。
4. **治理与晋级层**：任何新模型先影子运行，经过回放、样本外观察、人工评审和明确确认后，才允许影响真实金额或生成调仓提案。

以下四项原则不可颠倒：

- 先完成真实业务闭环，再扩展策略复杂度；
- 先证明研究模型有效，再允许模型影响资金；
- 先明确买入理由，再定义持有、加仓和退出动作；
- 任何模型都不能剥夺用户跳过、拒绝或延后的权利。

---

## 1. 版本轴与变更治理

系统同时存在三条版本轴，禁止混写：

| 版本轴 | 示例 | 含义 |
|---|---|---|
| 软件版本 | 仓库实际 release tag | Core、MCP、数据库、调度和交互能力；由实施阶段决定 |
| 策略版本 | `value-dca-v1.7.0` | 预算、准入、阈值、权重和动作规则 |
| 模型版本 | `macro-regime-v1.0.0` | 四季、排位、相对回撤等确定性模型 |

规则：

1. 本文不自动替换当前生产策略 v1.6。
2. 新的软件能力可以先上线，但新策略必须通过 `DRAFT → CONFIRMED → ACTIVE`。
3. 模型升级不允许静默改变历史计划。
4. 每份计划冻结时绑定精确策略版本、模型版本和输入快照。
5. 回放历史时使用当时可获得的数据，禁止未来数据泄漏。
6. 本文的能力阶段不预设软件版本号；一个阶段可以跨多个软件版本，也可以与其他阶段在同一版本交付。

---

## 2. 从播客吸收的架构原则

### 2.1 准确描述问题

所有投资动作必须从一个结构化决策问题开始：

- 决策对象是什么；
- 预期收益来自哪里；
- 当前环境是什么；
- 使用了哪些事实；
- 数据是否足以支持动作；
- 符合预期、不符合预期和不确定时分别怎么办。

Core 生成 `decision_frame`，Agent 只能解释，不能补造缺失事实。

### 2.2 策略不能混搭，但组合可以包含多个受隔离的策略

每个持仓和每笔新增投入必须绑定一个 `playbook`：

- `STRATEGIC_DCA`：长期战略定投；
- `VALUATION_REVERSION`：估值修复；
- `INCOME_CARRY`：票息、股息和现金流；
- `GROWTH_COMPOUNDING`：盈利与内在价值增长；
- `CYCLICAL_RECOVERY`：周期修复；
- `TREND_PARTICIPATION`：趋势参与，仅允许在独立研究或用户明确启用的策略实例中存在。

持仓下跌后不得临时更换 playbook 为自己辩护。变更投资理由必须创建新 thesis 版本并说明证据。

### 2.3 市场季节是区间，不是精确拐点

四季模型输出状态分布、置信度和证据，不声称准确捕捉顶部或底部。市场整体、行业和单个资产可以处在不同季节。

### 2.4 不确定性必须映射为动作强度

不确定不是“不做判断”，而是独立状态：

- 高置信度且证据充分：允许形成明确建议；
- 中等置信度：降级为保守建议或保持原计划；
- 低置信度：只输出观察，不改变金额；
- 数据不足：`DATA_BLOCKED`。

### 2.5 量化是工程机械

量化与AI负责广度、速度、筛选、比较、重算和审计，不替代深度研究。自动排行只能回答“先研究谁”，不能单独回答“买谁、卖谁”。

### 2.6 过程与结果分开评价

每次复盘同时记录：

- 决策过程是否合规；
- 当时信息是否充分；
- 原假设是否合理；
- 执行是否准确；
- 单次结果是否有利；
- 结果与原逻辑是否一致。

允许存在“过程正确、结果不利”和“过程错误、结果有利”。

### 2.7 研究必须包含反证

私域材料与公开材料必须分别形成证据包，并进行观点冲突检查。只使用与既有观点一致的材料时，研究状态不得超过 `PROVISIONAL`。

---

## 3. 策略分层

### 3.1 CORE：战略定投层

职责：

- 承担长期配置和组合稳定性；
- 依据目标比例、实际比例、预算和已批准规则分配新增资金；
- 优先通过新增资金修复比例；
- 不因短期排位落后自动卖出。

当前四只 CORE 的具体基金、权重和账户约束属于用户实例数据，不进入公共代码默认值。

### 3.2 SATELLITE：条件准入层

职责：

- 按已批准信号和数据质量规则决定新增资金是否开放；
- 无合格标的时保留资金；
- 不用目标比例强迫产生买入；
- 所有替换和卖出仅生成提案，等待用户决策及外部真实执行。

### 3.3 RESEARCH：影子研究层

包括：

- 宏观四季；
- 行业季节；
- 资产收益来源分类；
- 卫星候选排位；
- 估值、质量、动量和环境组合模型；
- Alpha/Beta 相对表现诊断；
- 假设失效检测。

研究模型具有四种运行模式：

```text
OFF → SHADOW → ADVISORY → ACTIVE
```

- `OFF`：不运行；
- `SHADOW`：计算并记录，不向用户形成动作建议；
- `ADVISORY`：可以展示建议，但不改变金额、不创建卖出提案；
- `ACTIVE`：经明确批准后，可以影响确定性计划或创建待确认提案；仍然不能交易。

任何模型不得从 `SHADOW` 直接进入 `ACTIVE`。

---

## 4. 不可协商的安全边界

```text
NO_AUTO_TRADE             不执行真实交易
NO_AUTO_CONFIRM           不替用户确认任何金融写操作
NO_FORCED_INVESTMENT      不禁止用户跳过、取消或延后计划
NO_BUDGET_OVERRUN         未经预先批准不得超过可用预算
NO_DIRECT_DB_FOR_LLM      Agent 不直接读写数据库
NO_LLM_MONEY_CALC         金额、份额、收益和评分由确定性代码计算
NO_SILENT_MUTATION        策略、模型、预算、计划和交易不得静默变化
NO_WARNING_MONEY_ACTION   WARNING 数据不得改变金额或开放卫星准入
NO_PRICE_ONLY_FUNDAMENTAL 价格表现不能单独定性基本面失效
NO_UNVERIFIED_BENCHMARK   未确认代理基准不得用于 Alpha/Beta 结论
NO_REASON_DRIFT           投资理由变更必须版本化
APPEND_ONLY_AUDIT         审计事件只追加
REVERSAL_NOT_DELETE       错误交易用冲正，不物理删除
HUMAN_EXECUTION           用户只在外部平台执行真实操作
SELL_PROPOSAL_ONLY        卖出规则只生成建议和诊断
```

---

## 5. 数据质量与证据等级

### 5.1 数据质量

| 等级 | 条件 | 允许行为 |
|---|---|---|
| `PASS` | 官方单源通过完整性检查，或至少两源一致；时效合格 | 可参与已批准的金额、准入和诊断规则 |
| `WARNING` | 单一非官方来源、轻微延迟或非关键字段缺失 | 可展示、可影子计算；不得改变金额或产生 OPEN 信号 |
| `SOURCE_ERROR` | 关键字段缺失、冲突、过期或解析失败 | 阻断相关计算 |

数据质量必须按字段和用途判断，不能用报告级 `PASS` 掩盖关键字段的 `WARNING`。

### 5.2 证据成熟度

```text
UNVERIFIED → PROVISIONAL → CORROBORATED → DECISION_GRADE
```

- `UNVERIFIED`：尚未验证；
- `PROVISIONAL`：逻辑可用，但只有单侧或单源证据；
- `CORROBORATED`：多源一致并完成反证检查；
- `DECISION_GRADE`：满足指定规则的决策级证据。

宏观判断、资产分类和投资论点都必须保存证据成熟度。

---

## 6. 资产定性：从 TREE/GRAIN 升级为收益来源画像

### 6.1 保留播客比喻，但不作为刚性二分类

用户界面可以继续显示：

- 树：主要依靠长期增长；
- 粮：主要依靠估值回归、周期恢复或现金回报；
- 菜：主要依靠趋势与交易价值。

数据库使用更精确的 `return_driver`：

```text
LONG_TERM_GROWTH
VALUATION_REVERSION
INCOME_CARRY
CYCLICAL_RECOVERY
TREND
MIXED
UNKNOWN
```

### 6.2 分类不是永久属性

分类记录必须包含：

- 作用范围：基金、指数、行业或底层资产；
- 主收益来源与次收益来源；
- 证据与数据日期；
- 置信度；
- 生效和失效日期；
- 分类模型版本；
- 人工确认状态。

基金不得直接使用上市公司的自由现金流规则。基金分类必须基于基金合同、基准、持仓暴露、经理/指数方法和历史风格稳定性。

### 6.3 基金版 MAPER 研究框架

播客中的公司研究 MAPER 被改写为适合基金的五维框架：

| 维度 | 基金研究问题 |
|---|---|
| M — Mandate | 合同目标、指数方法或主动管理边界是什么 |
| A — Advantage | 指数、管理流程、费率、规模和跟踪能力是否有优势 |
| P — Potential | 所代表资产或行业的长期空间、估值和容量如何 |
| E — Execution | 经理、团队、申赎、限额、跟踪误差和运营是否可靠 |
| R — Results & Reliability | 业绩来源、风险、风格稳定性和数据确定性如何 |

MAPER 只形成研究事实和投资论点，不直接生成金额。

### 6.4 核心数据结构

```sql
CREATE TABLE return_driver_profiles (
    id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    version INTEGER NOT NULL,
    primary_driver TEXT NOT NULL CHECK (primary_driver IN (
        'LONG_TERM_GROWTH','VALUATION_REVERSION','INCOME_CARRY',
        'CYCLICAL_RECOVERY','TREND','MIXED','UNKNOWN'
    )),
    secondary_drivers_json TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL CHECK (scope IN ('FUND','INDEX','SECTOR','UNDERLYING')),
    confidence TEXT NOT NULL CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
    evidence_maturity TEXT NOT NULL CHECK (evidence_maturity IN (
        'UNVERIFIED','PROVISIONAL','CORROBORATED','DECISION_GRADE'
    )),
    evidence_json TEXT NOT NULL,
    model_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','ACTIVE','RETIRED')),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(instrument_id, version)
);

CREATE TABLE instrument_thesis_versions (
    id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    version INTEGER NOT NULL,
    playbook TEXT NOT NULL,
    thesis_json TEXT NOT NULL,
    thesis_hash TEXT NOT NULL,
    evidence_maturity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','ACTIVE','REVIEW','INVALID','RETIRED')),
    approved_by TEXT,
    approved_at TEXT,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(instrument_id, version)
);
```

同一标的只能有一个当前 ACTIVE thesis；历史版本不可覆盖。

---

## 7. 投资论点与动作契约

每个可投入标的必须存在活动 `instrument_thesis_version`，至少包括：

```json
{
  "playbook": "STRATEGIC_DCA",
  "primary_return_driver": "LONG_TERM_GROWTH",
  "secondary_return_drivers": ["VALUATION_REVERSION"],
  "why_hold": "...",
  "expected_horizon": "1-3Y",
  "evidence_refs": ["..."],
  "expected_adversity": ["..."],
  "invalidation_conditions": ["..."],
  "add_conditions": ["..."],
  "hold_conditions": ["..."],
  "sell_review_conditions": ["..."],
  "confidence": "MEDIUM",
  "evidence_maturity": "CORROBORATED"
}
```

规则：

1. `why_hold` 不能只是“跌了很多”或“最近上涨”。
2. `expected_adversity` 描述该策略必须承受的正常逆风期。
3. 触发 `invalidation_conditions` 只进入复核，不直接创建交易。
4. 更换 playbook 必须创建新版本，不覆盖旧版本。
5. 复盘必须检查实际动作是否与当时 playbook 一致。

---

## 8. 市场与结构四季模型

### 8.1 作用范围

四季模型分别计算：

- `MARKET`：全市场；
- `REGION`：A股、美国等；
- `SECTOR`：科技、消费、医药等；
- `STYLE`：成长、价值、高股息等。

不得用全市场夏季覆盖处于冬季的行业，也不得用行业冬季代表整个组合。

### 8.2 输入维度

四季模型至少包含：

1. **估值**：历史分位、股债风险溢价、盈利周期适配；
2. **基本面/经济**：盈利修正、景气扩散、结构性分化；
3. **政策与流动性**：只使用可验证的政策事实和流动性指标；
4. **情绪与市场结构**：成交、基金发行/赎回、宽度、拥挤度；
5. **价格确认**：只作为状态证据之一，不单独决定季节。

宏观数据通常滞后，因此宏观维度不能拥有单独否决权。

### 8.3 输出

```json
{
  "scope": "SECTOR:CONSUMER",
  "as_of": "YYYY-MM-DD",
  "probabilities": {
    "SPRING": 0.10,
    "SUMMER": 0.15,
    "AUTUMN": 0.20,
    "WINTER": 0.55
  },
  "dominant_season": "WINTER",
  "confidence": "LOW",
  "ambiguity": "HIGH",
  "evidence_maturity": "PROVISIONAL",
  "model_version": "macro-regime-v1.0.0",
  "data_quality": "PASS"
}
```

### 8.4 状态稳定机制

为避免季节频繁跳变，必须支持：

- 最短持续期；
- 进入阈值与退出阈值分离；
- 连续多期确认；
- `TRANSITION` 过渡状态；
- 状态修订记录；
- 低置信度时保持上期状态但标记不确定。

### 8.5 四季对资金的影响

模型默认 `SHADOW`。进入 `ACTIVE` 后仍遵守：

1. 不得禁止用户跳过；
2. 不得突破用户批准的周预算；
3. 冬季增加金额只能使用预先批准的机会预算或储备金；
4. 夏季降速产生的余额进入 `RESERVED_CASH`，不得自动转投其他基金；
5. 低置信度、WARNING 或基准不完整时，宏观乘数固定为 `1.0x`；
6. 所有乘数必须来自策略实例，不在代码中硬编码 `1.5x/0.8x`。

### 8.6 模型注册与四季事实表

```sql
CREATE TABLE model_registry (
    id TEXT PRIMARY KEY,
    model_key TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    model_type TEXT NOT NULL CHECK (model_type IN (
        'MACRO_REGIME','SATELLITE_RANKING','RELATIVE_DIAGNOSIS','RETURN_DRIVER'
    )),
    mode TEXT NOT NULL CHECK (mode IN ('OFF','SHADOW','ADVISORY','ACTIVE')),
    parameters_json TEXT NOT NULL,
    parameters_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','VALIDATING','APPROVED','RETIRED')),
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(model_key, semantic_version)
);

CREATE TABLE market_regime_observations (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES model_registry(id),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('MARKET','REGION','SECTOR','STYLE')),
    scope_key TEXT NOT NULL,
    as_of_market_date TEXT NOT NULL,
    dominant_season TEXT NOT NULL CHECK (dominant_season IN (
        'SPRING','SUMMER','AUTUMN','WINTER','TRANSITION','UNKNOWN'
    )),
    probabilities_json TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
    ambiguity TEXT NOT NULL CHECK (ambiguity IN ('LOW','MEDIUM','HIGH')),
    evidence_maturity TEXT NOT NULL,
    data_quality TEXT NOT NULL CHECK (data_quality IN ('PASS','WARNING','SOURCE_ERROR')),
    input_hash TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(model_id, scope_type, scope_key, as_of_market_date, input_hash)
);

CREATE TABLE model_validation_runs (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES model_registry(id),
    validation_type TEXT NOT NULL CHECK (validation_type IN (
        'BACKTEST','WALK_FORWARD','SHADOW','STRESS','COST_SENSITIVITY'
    )),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    baseline_key TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    leakage_checks_json TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS','WARNING','FAIL','INSUFFICIENT_DATA')),
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

模型 `mode` 的改变属于金融配置写操作，必须经过草稿和明确确认。

---

## 9. 卫星雷达与候选排位

### 9.1 雷达的目的

雷达解决广度和速度问题：从用户批准的候选范围中找到优先研究对象。它不是自动选基器。

### 9.2 候选范围

候选范围必须由策略实例明确配置：

- 允许的市场、产品类型和赛道；
- 最低成立年限、规模、流动性；
- 费用和申赎约束；
- 是否允许主动基金；
- 是否允许 QDII；
- 数据覆盖门槛。

公共代码不得默认固定“五赛道、每赛道四只、Top20”。

### 9.3 先过滤，后评分

硬过滤包括：

- 产品状态异常；
- 数据不足；
- 基准不明确；
- 跟踪或风格严重漂移；
- 规模、流动性或费用不满足实例规则；
- 用户未批准的资产范围。

过滤通过后，才允许评分。

### 9.4 评分维度

候选评分可以包含：

- 估值吸引力；
- 产品质量；
- 长期收益来源质量；
- 价格/资金趋势；
- 环境适配；
- 与现有组合的分散价值；
- 交易成本和执行可行性；
- 证据成熟度。

权重必须版本化并经过验证。初始阶段不采用固定 `40/30/30` 作为生产事实。

### 9.5 排位稳定机制

生成替换复核前，至少要求：

- 新候选连续多个评估期领先；
- 领先差值超过最小门槛；
- 当前持仓未处于最短持有期；
- 赎回费和确认周期可接受；
- 当前持仓投资论点被削弱，或替换后组合明显改善；
- 数据质量为 PASS；
- 代理基准和产品信息完整。

单次掉到末位不能触发替换。

### 9.6 运行模式

- `SHADOW`：记录模拟排名和假想替换；
- `ADVISORY`：向用户解释研究优先级；
- `ACTIVE`：仅生成 `REPLACE_REVIEW_REQUIRED`，不自动批准、不自动交易。

---

## 10. 相对表现与 Alpha/Beta 诊断

### 10.1 目的

相对表现用于回答“下跌主要是共同因素还是标的特有因素”，不直接回答“基本面是否暴雷”。

### 10.2 前置条件

- benchmark mapping 已由用户确认；
- `proxy_suitability=STRONG` 才能参与动作型诊断；
- WEAK 代理只能展示；
- 无代理时返回 `DATA_BLOCKED`；
- 评估窗口、收益口径和跟踪误差模型版本化。

### 10.3 输出状态

```text
SYSTEMIC_COMPONENT_HIGH
IDIOSYNCRATIC_COMPONENT_HIGH
MIXED
NORMAL_VARIATION
INSUFFICIENT_DATA
```

禁止输出“价格独跌 = 基本面暴雷”。

### 10.4 诊断清单

出现显著特有下跌时继续检查：

- 基准和行业同期表现；
- 基金经理、合同或指数规则变更；
- 风格漂移；
- 规模、申赎和流动性；
- 跟踪误差；
- 底层盈利和估值；
- 产品公告与可验证事件；
- 原 thesis 的失效条件。

只有多项证据共同支持时，才能形成 `THESIS_REVIEW_REQUIRED` 或卖出提案。

---

## 11. 外部订单、成交与计划执行闭环

这是当前实施的最高优先级，优先于宏观和排位模型进入生产；具体发布版本由实施时决定。

### 11.1 三类事实必须分离

1. `investment_plan`：系统建议用户做什么；
2. `external_order`：用户在外部平台提交了什么；
3. `transaction`：平台最终确认了什么。

提交订单不等于成交，成交确认前不改变正式持仓。

### 11.2 外部订单状态机

```text
DRAFT
  → SUBMITTED
  → PENDING_CONFIRMATION
  → PARTIALLY_CONFIRMED
  → CONFIRMED
  ├→ FAILED
  └→ CANCELLED
```

记录字段至少包括：

- 组合、账户、平台；
- 基金代码与份额类别；
- BUY/SELL；
- 提交金额或份额；
- 提交时间、交易日、预计确认日；
- 实际确认日；
- 平台订单号的脱敏值；
- 关联计划项；
- 当前状态；
- 失败或取消原因。

### 11.3 基金与账户执行约束

实例级约束表保存：

- 起购金额；
- 单笔最低/最高金额；
- 单日最高金额；
- 申购、赎回和转换状态；
- 预计确认规则；
- 赎回费规则；
- 平台和份额类别差异；
- 生效日期、来源和质量。

计划生成时必须检查这些约束，并显示预计需要的交易日数量。未知约束必须标记 `EXECUTION_CONSTRAINT_UNKNOWN`。

### 11.4 周计划执行状态

```text
DRAFT
→ FROZEN
→ PARTIALLY_SUBMITTED
→ SUBMITTED
→ PARTIALLY_EXECUTED
→ EXECUTED
```

旁路状态：`SKIPPED / EXPIRED / CANCELLED`。

定义：

- `PARTIALLY_SUBMITTED`：只有部分计划金额已在外部平台下单；
- `SUBMITTED`：全部计划金额已下单，但尚未全部确认；
- `PARTIALLY_EXECUTED`：至少一笔真实成交已确认，但计划尚未完全覆盖；
- `EXECUTED`：全部必需计划项满足金额口径和关联校验。

### 11.5 执行分配表

使用 `plan_execution_allocations` 连接计划项、外部订单和真实成交：

```sql
CREATE TABLE plan_execution_allocations (
    id TEXT PRIMARY KEY,
    plan_item_id TEXT NOT NULL REFERENCES plan_items(id),
    external_order_id TEXT REFERENCES external_orders(id),
    transaction_id TEXT REFERENCES transactions(id),
    allocated_amount_fen INTEGER NOT NULL CHECK (allocated_amount_fen > 0),
    allocation_basis TEXT NOT NULL CHECK (
        allocation_basis IN ('ORDER_SUBMITTED','TRANSACTION_CONFIRMED')
    ),
    created_at TEXT NOT NULL,
    UNIQUE(plan_item_id, external_order_id),
    UNIQUE(plan_item_id, transaction_id)
);
```

同一交易不得被多个计划重复使用。完成计划时按真实确认金额核对；手续费口径由策略参数显式定义，默认计划金额指平台确认申购金额，不重复加计外部手续费。

### 11.6 在途金额

下一份计划不能只看已确认持仓。Core 同时计算：

```text
effective_exposure
= confirmed_holdings
+ eligible_pending_buy_orders
- eligible_pending_sell_orders
```

在途订单只影响“避免重复下单”的计划曝光，不进入正式持仓、收益和成交账本。失败或取消后立即释放在途占用。

### 11.7 计划进度

每个计划项展示：

- 计划金额；
- 已提交金额；
- 待确认金额；
- 已确认成交金额；
- 剩余未提交金额；
- 预计最早完成日；
- 阻塞原因。

### 11.8 外部订单与执行约束数据结构

```sql
CREATE TABLE instrument_account_constraints (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    share_class TEXT,
    minimum_order_fen INTEGER,
    maximum_order_fen INTEGER,
    maximum_daily_fen INTEGER,
    subscription_status TEXT NOT NULL CHECK (subscription_status IN (
        'OPEN','LIMITED','SUSPENDED','UNKNOWN'
    )),
    confirmation_rule_json TEXT NOT NULL DEFAULT '{}',
    redemption_fee_rule_json TEXT NOT NULL DEFAULT '{}',
    source_json TEXT NOT NULL,
    data_quality TEXT NOT NULL CHECK (data_quality IN ('PASS','WARNING','SOURCE_ERROR')),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE external_orders (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    plan_item_id TEXT REFERENCES plan_items(id),
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    status TEXT NOT NULL CHECK (status IN (
        'DRAFT','SUBMITTED','PENDING_CONFIRMATION','PARTIALLY_CONFIRMED',
        'CONFIRMED','FAILED','CANCELLED'
    )),
    submitted_amount_fen INTEGER,
    submitted_shares_decimal TEXT,
    confirmed_amount_fen INTEGER,
    confirmed_shares_decimal TEXT,
    submitted_at TEXT,
    trade_date TEXT,
    expected_confirmation_date TEXT,
    actual_confirmation_date TEXT,
    platform_order_ref_hash TEXT,
    status_reason TEXT,
    source_message TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`expected_confirmation_date` 是估计事实，不得覆盖 `actual_confirmation_date`。平台规则变化后，新订单使用新约束版本，历史订单保留原依据。

---

## 12. 确定性周计划引擎

### 12.1 输入

- 用户批准的可用预算；
- 当前策略版本；
- 已确认持仓；
- 合格的在途订单；
- CORE/SATELLITE 目标和准入；
- 产品和账户执行约束；
- PASS 级估值与风险事实；
- 已批准且为 ACTIVE 的模型输出；
- 组合过渡状态。

### 12.2 计算顺序

1. 扣除已经提交但未确认的计划占用；
2. 计算有效暴露与目标缺口；
3. 执行 CORE 比例修复；
4. 检查 SATELLITE 信号和数据质量；
5. 检查申购限额、最小金额和确认周期；
6. 应用经批准的估值乘数；
7. 可选应用 ACTIVE 宏观调整；
8. 舍入并处理剩余资金；
9. 输出可执行拆单日历；
10. 保存完整输入哈希和原因码。

### 12.3 保留资金

卫星无 OPEN 信号、数据为 WARNING 或执行约束阻塞时：

- 资金进入 `RESERVED_CASH`；
- 不自动转投其他标的，除非策略实例明确批准备用顺序；
- 周报说明保留原因；
- 不因此阻塞可独立执行的 CORE 部分。

---

## 13. 研究流程：广度、深度、速度

### 13.1 普读

一天内形成：

- 产品做什么；
- 跟踪或投资什么；
- 在同类中的位置；
- 数据是否足够；
- 是否值得精读。

输出只允许为 `DISMISS / WATCH / DEEP_RESEARCH_REQUIRED`。

### 13.2 精读

完整回答：

- 收益来源；
- 成功关键因素；
- 产品/经理/指数优势；
- 长期空间与容量；
- 估值适用方法；
- 最重要风险；
- 反方证据；
- 对组合的增量价值；
- 哪些事实会推翻结论。

### 13.3 AI 使用规则

- AI 可以整理、筛选、比较和发现问题；
- AI 输出必须由事实工具验证；
- 私域观点与公域反方观点分别检索；
- 不允许直接复制AI研究结论作为 `DECISION_GRADE`；
- 用户或研究流程必须能够说清楚结论，而不是朗读模型输出。

---

## 14. 决策质量与错题本

新增 `decision_journal`：

```text
decision_id
decision_type
decision_frame
known_facts
unknowns
selected_playbook
expected_scenarios
chosen_action
process_quality
execution_quality
outcome_quality
logic_match
root_cause
lesson
```

结果矩阵：

| 过程 | 结果 | 复盘结论 |
|---|---|---|
| 正确 | 有利 | 规则得到有限支持，不等于永久有效 |
| 正确 | 不利 | 检查随机性、逆风期和假设是否仍成立 |
| 错误 | 有利 | 记录幸运结果，不强化错误动作 |
| 错误 | 不利 | 提炼根因并建立防复发规则 |

禁止只用盈亏判断决策质量。

### 14.1 决策日志数据结构

```sql
CREATE TABLE decision_journal (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    decision_frame_json TEXT NOT NULL,
    selected_playbook TEXT,
    known_facts_json TEXT NOT NULL,
    unknowns_json TEXT NOT NULL,
    scenarios_json TEXT NOT NULL,
    chosen_action_json TEXT NOT NULL,
    process_quality TEXT CHECK (process_quality IN ('PASS','WARNING','FAIL','NOT_REVIEWED')),
    execution_quality TEXT CHECK (execution_quality IN ('PASS','WARNING','FAIL','NOT_APPLICABLE')),
    outcome_quality TEXT CHECK (outcome_quality IN ('FAVORABLE','UNFAVORABLE','MIXED','UNKNOWN')),
    logic_match TEXT CHECK (logic_match IN ('MATCHED','DIVERGED','UNKNOWN')),
    root_cause_json TEXT,
    lesson TEXT,
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
```

---

## 15. Agent 与确定性 Core 的职责

### 15.1 Core 负责

- 数据同步和质量；
- 订单、成交、持仓和计划进度；
- 估值、收益、缺口和金额；
- 四季概率、排位和相对表现计算；
- 状态机、幂等、审计和报告事实包；
- 研究模型影子回放。

### 15.2 Agent 负责

- 理解用户意图；
- 将事实解释为自然语言；
- 提出需要用户决定的唯一问题；
- 区分事实、推断、建议和未知；
- 在复盘中引导用户检查原始投资理由；
- 调用受控 MCP 工具。

### 15.3 Agent 不得

- 自行补全金额或份额；
- 将新闻直接变成交易建议；
- 用情绪安慰覆盖风险事实；
- 把“系统性下跌”说成“一定会反弹”；
- 把“排名落后”说成“资产劣质”；
- 使用法律意义不明确的“受托人承诺”。

产品表达建议使用“纪律型投资助手”，而不是宣称系统承担法律意义上的 Fiduciary 责任。

---

## 16. 面向用户的表达规范

默认结构：

```text
当前发生了什么
为什么会这样
对计划和持仓有什么影响
现在是否需要你处理
```

禁用默认文案：

- 抄底、逃顶、起飞、霸榜、角斗、卧倒；
- 强买、强卖、必涨、必跌；
- 暴雷（除非有已验证的正式事实）；
- 机构级、绝对安全等无法证明的表述。

内部可以保留模型名称，但用户看到的是中性业务语言，例如“卫星候选比较”“相对表现诊断”“市场环境观察”。

---

## 17. 报告体系

### 17.1 日报

- 数据日期和质量；
- NAV/行情同步；
- 风险扫描；
- 在途订单与预计确认；
- 周计划进度；
- 新增或恢复告警；
- 需要用户处理的事项；
- 明确没有自动交易。

### 17.2 周报

- 本周预算、计划和实际执行；
- CORE/SATELLITE 目标、确认持仓和有效暴露；
- 已提交、待确认、已成交和剩余金额；
- 申购限额与下周任务；
- 研究模型只读观察；
- 数据质量与阻塞原因。

### 17.3 月度复盘

- 期初/期末持仓和现金流；
- 计划执行偏差；
- 收益与基准；
- thesis 状态；
- 决策过程/结果矩阵；
- 四季和候选排位的影子表现；
- 需要继续观察、验证或创建草稿的事项。

### 17.4 年度复盘

- Modified Dietz、XIRR、TWR 和基准；
- 各基金与各策略贡献；
- 策略参数效果；
- 模型影子/正式表现差异；
- 错题本中的重复问题；
- 下一策略版本提案。

所有报告必须经过 outbox、送达渠道和回执链；正常报告不能永远 SILENT。

---

## 18. MCP 能力增量

### 18.1 只读

```text
external_order_list
plan_execution_progress_get
pending_exposure_get
instrument_constraints_get
instrument_thesis_get
return_driver_profile_get
macro_regime_get
satellite_ranking_get
relative_performance_diagnosis_get
decision_journal_get
model_validation_get
```

### 18.2 草稿型

```text
external_order_draft_create
instrument_thesis_draft_create
model_mode_change_draft_create
strategy_parameter_draft_create
sell_review_draft_create
```

### 18.3 明确确认型

```text
external_order_commit
transaction_draft_commit
instrument_thesis_commit
model_mode_change_commit
strategy_parameter_commit
sell_decision_commit
plan_execution_allocation_commit
```

所有确认令牌单次使用、短期有效、绑定内容哈希。用户无需复制内部ID，但系统必须精确引用当前唯一待确认草稿。

---

## 19. 模型晋级门槛

### 19.1 SHADOW → ADVISORY

至少满足：

- 数据覆盖和质量达标；
- 历史回放无未来数据泄漏；
- 规则、阈值和权重有版本；
- 输出稳定，无明显频繁翻转；
- 能解释为何给出结果；
- 与当前生产策略不冲突；
- 用户明确确认。

### 19.2 ADVISORY → ACTIVE

至少满足：

- 完成预定样本外观察期；
- 与无模型基线比较；
- 计入申赎费用、确认延迟和限额；
- 检查最大回撤、换手率和错误动作；
- 对不同市场阶段做压力测试；
- 形成独立评审报告；
- 创建策略草稿；
- 用户明确确认。

### 19.3 自动降级

出现以下任一情况时从 ACTIVE 自动降为 ADVISORY 或 SHADOW：

- 关键数据质量下降；
- 模型漂移或输出异常；
- 基准映射失效；
- 连续多期触发稳定性阈值；
- 实际行为与回测假设不一致；
- 用户暂停。

降级不能删除历史结果。

---

## 20. 开发阶段与发布顺序

### 阶段 A：场外订单与产品闭环

必须完成：

1. 外部订单状态；
2. 份额确认周期；
3. 周计划部分提交、部分成交和累计关联；
4. 在途金额参与下一计划；
5. 产品申购限额与拆单计划；
6. 日报、周报和周期复盘真实送达；
7. 第一笔真实计划完成闭环。

### 阶段 B：投资论点与决策质量

1. 投资论点版本；
2. 收益来源画像；
3. 基金 MAPER；
4. 决策日志与错题本；
5. 过程/结果分离复盘。

### 阶段 C：宏观与结构四季影子运行

1. 市场与结构四季；
2. 概率、置信度和过渡；
3. 影子模式；
4. 历史回放和样本外观察；
5. 不影响真实金额。

### 阶段 D：卫星研究雷达

1. 用户批准的候选范围；
2. 普读/精读；
3. 排位和稳定机制；
4. 相对表现诊断；
5. 影子替换记录。

### 阶段 E：策略 v1.7 激活评审

完成模型验证后，决定哪些能力：

- 保持研究；
- 升级为建议；
- 进入金额计算；
- 永不进入生产。

只有阶段 E 才允许激活 `Value DCA v1.7`。

上述阶段描述依赖关系和验收顺序，不规定软件版本。每次开始实施前，应先审计当前已发布能力，跳过已完成项，并根据真实代码差距确定本次发布范围。

发布继续遵守：feature → develop → 长期 release → main；版本通过 tag 标识，不创建版本化 release 分支。

---

## 21. 数据迁移

### 21.1 非破坏性原则

- 现有交易、持仓、计划和审计不得重写；
- 新表通过 Alembic 增量迁移；
- 旧 FROZEN 计划初始化执行进度时只读取现有事实；
- 无法推断的外部订单状态保持 UNKNOWN，不自动补造；
- 旧 benchmark mapping 继续有效，但必须补充 suitability 和证据；
- 旧 TREE/GRAIN 标签若存在，迁移为 `legacy_label`，不直接转成决策级 return driver。

### 21.2 当前真实计划迁移验证

迁移测试至少覆盖：

- 多只基金构成一份冻结计划；
- 同一基金分多个交易日申购；
- 订单已提交但数日后才确认；
- 一部分基金已确认、一部分仍待确认；
- 下一周计划不会重复计算在途申购；
- 只有真实确认交易进入正式持仓；
- 全部计划项满足后才能 EXECUTED。

---

## 22. 测试与验收

### 22.1 订单和计划

- 同一计划项可关联多个订单和交易；
- 单笔/单日限额能生成正确拆单计划；
- 周末、节假日和 QDII 延迟不被固定假设为 T+1；
- 待确认订单不改变持仓但影响有效暴露；
- 失败订单释放在途占用；
- 成交金额不足时保持 PARTIALLY_EXECUTED；
- 超额、错基金、错账户、冲正或重复关联被拒绝；
- 手续费口径一致。

### 22.2 四季模型

- 市场与行业可以输出不同季节；
- 低置信度不改变金额；
- WARNING 不改变金额；
- 状态具备滞回和最短持续期；
- 用户始终可以跳过；
- 没有机会预算时不得突破周预算。

### 22.3 排位和诊断

- 单期末位不创建替换提案；
- 未确认基准返回 DATA_BLOCKED；
- 同步下跌不自动定性基本面安全；
- 独立下跌不自动定性暴雷；
- 交易成本和确认延迟进入替换评估；
- SHADOW 模式不写生产策略和提案。

### 22.4 研究和论点

- 没有活动 thesis 的新增标的不得进入 ACTIVE 准入；
- 修改 playbook 创建新版本；
- 只有单侧资料时证据不得升级为 DECISION_GRADE；
- Agent 不能将解释写成市场事实；
- 过程正确/结果不利可以被正确记录。

### 22.5 业务闭环验收

1. 至少一份日报真实送达；
2. 至少一份周计划报告真实送达；
3. 一份计划完成 DRAFT → FROZEN → 部分提交 → 部分成交 → EXECUTED；
4. 多日多笔交易累计正确；
5. 在途订单没有造成下一计划重复买入；
6. 持仓与真实成交一致；
7. 至少一份月度复盘真实送达；
8. 无自动交易、自动确认或强制投资；
9. WARNING 数据没有影响金额；
10. readiness 中周计划生命周期和周期复盘均为 PASS。

---

## 23. 当前默认模式

在用户另行确认前：

| 能力 | 默认模式 |
|---|---|
| 原 Value DCA v1.6 | ACTIVE |
| 外部订单与部分执行 | 开发完成后 ACTIVE |
| 投资论点与收益来源画像 | ADVISORY，确认后逐只激活 |
| 宏观四季 | SHADOW |
| 卫星候选排位 | SHADOW |
| 相对表现诊断 | ADVISORY；无强代理则 DATA_BLOCKED |
| 自动替换或宏观金额油门 | OFF |

这套默认值既保留本次升级方向，也保证尚未验证的新模型不会直接控制真实资金。

---

## 24. 最终完成定义

本架构只有同时满足以下条件，才能称为“自适应纪律投资系统”：

- 能完整记录计划、订单、成交、持仓和复盘；
- 能说清每项持仓的收益来源和原始理由；
- 能区分正常逆风、数据不足和假设可能失效；
- 能表达市场与结构环境，同时承认不确定性；
- 能用量化扩大研究广度和速度，不让排行代替判断；
- 能从错误中提炼根因，而不是只看盈亏；
- 新模型必须经过影子验证和人工晋级；
- 用户始终拥有最终决定权；
- 系统永远不执行真实交易。

这才是播客所强调的“适应环境”，也是 Value DCA 从固定规则走向可学习、可审计、可回退体系的正确升级方式。

---

## 25. 设计依据与使用限制

本架构吸收了用户提供播客转写中的方法论：策略与环境适配、树/粮/菜收益来源、四季区间、不确定性与仓位、量化的广度和速度、普读/精读、MAPER、过程与结果分离、组合化研究与执行。

外部研究仅支持“状态、价值、动量和多因子值得研究”，不证明本文任何具体阈值天然有效：

- NBER, *How do Regimes Affect Asset Allocation?*  
  https://www.nber.org/papers/w10080
- NBER, *Conditional Market Timing with Benchmark Investors*  
  https://www.nber.org/papers/w6434
- AQR, *Value and Momentum Everywhere*  
  https://www.aqr.com/insights/research/journal-article/value-and-momentum-everywhere
- MSCI, *How Can Factors Be Combined?*  
  https://www.msci.com/research-and-insights/paper/how-can-factors-be-combined

因此，模型权重、阈值、窗口、乘数和晋级结论必须由本系统自己的数据、成本模型、样本外观察和用户确认决定。
