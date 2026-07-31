# Output templates

## Query

1. Conclusion.
2. Key facts with the as-of date.
3. Data quality and limitations.

## Confirmation preview

1. Action and target.
2. Amount, shares, date, or state change exactly as returned by Core.
3. Consequences and expiry.
4. A request for explicit confirmation.

## Opening-position confirmation preview

1. State `期初持仓导入（不是买入交易）` and identify the account and instrument.
2. Show the as-of date, platform-reported total shares and supplied cost basis. Use the user-facing
   label `账面成本（按平台显示的份额和成本价换算）` for a calculated total; do not say
   `Core 推导总成本`. Disclose rounding to CNY 0.01 in one short note.
3. State that holdings change only after the exact opening-position commit tool succeeds.
4. Show the draft ID and expiry, disclose any source warning, and request explicit confirmation.

## Weekly-plan confirmation preview

1. State the exact plan ID, revision, plan date, strategy version, and data quality.
2. Show every Core-returned instrument item, candidate amount, reserved amount, action, and reason
   code. Never add or substitute an instrument.
3. State `DRAFT（未冻结、未成交）`, show the confirmation expiry, and explain that freezing creates
   no transaction.
4. Request explicit confirmation of that exact revision. After freezing, keep the label
   `FROZEN（计划已确认、交易未执行）` until separately committed BUY records are linked.

## Sell proposal

1. Trigger and evidence.
2. Thesis and proxy applicability.
3. Fees, portfolio impact, risk of holding, and fund destination.
4. Proposal state and `未执行交易` notice.
5. Available decisions: approve, defer, or reject.

## Risk scan summary

1. Execution: `execution_status`, `state`, `reason_code`, and `data_quality`.
2. Completeness: candidate, configured, evaluable/evaluated, unconfigured, unavailable,
   not-applicable and exempt counts.
3. Outcome: triggered rules and review-only proposal count.
4. Per-instrument summaries from Core. Do not describe `NOT_CONFIGURED`, `DATA_UNAVAILABLE`, or
   `NOT_APPLICABLE` as a successful non-hit.
5. Fetch rule-level pages only when needed. Keep details opt-in and filtered to protect context.

## Scheduled report

Return `[SILENT]` when the job contract says to remain silent and no qualifying change exists.
Otherwise provide conclusion, changed facts, warnings, and the one next action the user may take.
Treat `delivery_action` literally. A `NOTIFY` bundle is pending content, not proof that Weixin or
another channel received it. Never call a write tool from the scheduled report turn.

Treat delivery lifecycle literally:

- `PENDING`: waiting for an adapter claim.
- `DISPATCHED`: claimed by an adapter; delivery is not proven.
- `DELIVERED`: provider-acceptance evidence is recorded; this is not necessarily a human read.
- `FAILED`: maximum attempts exhausted.
- `SUPPRESSED`: intentionally not sent.

Never translate `DISPATCHED`, Cron `ok`, stdout, or an origin handoff as “已送达”.

## Performance and periodic review

Present Core's period, calculation version, Modified Dietz, XIRR, TWR, benchmark return, excess
return, data quality and warnings exactly. A null metric is `不可用`, never `0`. State the
`EXTERNAL_FLOW` cash convention whenever Core returns it. Periodic review action items are
follow-up facts only; do not convert them into trade advice.

## Market discovery

State the explicit scanned universe, as-of date, lookback, source quality and calculation version.
For each item, show only Core-returned return windows, drawdown, volatility, freshness, evidence
coverage, state and review flags. Label the package `事实观察，不是基金排名或买卖建议`. Never infer
that `REVIEW` means buy, sell, rotate, add to strategy, or make contribution-eligible.

## Discovery changes

State both run IDs and dates. Separate state transitions, added/removed flags, evidence or
verification-coverage changes, and numeric metric deltas. Label every item `事实变化，不是轮换或交易
信号`. If Core reports zero attention changes, do not manufacture a narrative.

## Review trends

State the as-of date, review type, lookback and included review count. Report quality continuity,
the returned performance series, governance coverage, recurring action codes, unresolved count
and oldest unresolved age. Preserve null values. Label the result `跨期复盘事实，不是策略评分或投资
建议`.

## Research watchlist and source changes

For a watchlist transition, show the instrument, previous state, requested state, review date,
reason, expiry and all unchanged-system flags. State `研究状态，不代表进入策略、获得定投资格或完成交易`.
For source-content changes, show the two evidence IDs, source lineage, evidence type and exact
added, removed and changed field paths. Do not infer importance or direction.

## Watchlist review cycle

State the snapshot date, status, reason code, data quality and due-status counts. For each entry,
report the current watchlist state, observation days, review due date, due status, evidence
coverage, latest discovery facts and quality flags exactly. State `到期复核事实，不代表自动改变观察池
状态、采用标的、轮换或交易`.

## Review action outcomes

Show the action ID, action code, outcome, evidence quality, evidence reference, note and confirmed
time. For a trend, report outcome coverage, missing outcomes, outcome distribution and resolution
days exactly. State `复盘结果事实，不是策略评分、因果证明或自动调参依据`.

## Research collection run

State the connector key, adapter version, upstream source lineage, start/finish times, manifest
hash and execution status. Report recorded, replayed and rejected counts plus every stable item
error exactly. State `采集成功只证明事实已按契约保存，不代表独立验证、投资相关性或推荐`.

## Review quality

State the as-of date, lookback, status and data quality. Report review continuity, action closure
and outcome coverage, unresolved age, research-run traceability and each strategy context exactly.
Preserve `INSUFFICIENT_HISTORY` and `OBSERVATIONAL_ONLY`. State `复盘流程质量事实，不是策略得分、
参数优劣、因果证明或自动调参依据`.

## Research source and evidence coverage

For a research-source configuration, show connector key, display name, enabled state, supported
evidence types, declared upstream lineages, whether a credential reference exists, version and the
confirmation boundary. Never display or request a secret value. For a coverage snapshot, report the
explicit universe, required evidence types, freshness threshold, current/stale/missing totals,
blocked total and every bounded collection task exactly. State `证据覆盖与待采集任务，不是来源已运行、
独立验证、基金排名或投资建议`.
