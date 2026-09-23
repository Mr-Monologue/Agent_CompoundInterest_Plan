# v0.33.1

Read-only execution constraint diagnostics distinguish candidate allocations from platform execution. Core research diagnosis preserves missing thesis, evidence and model limitations. Candidate v1.7 architecture is archived for audit only; no strategy, money algorithm or schema change.

## v0.33.0

- 新增本机 HTTP 日常入口：真实组合、明确预算预览、周报草稿、调度审计和受限机会解释。
- 写请求使用固定业务流程、明确确认及持久请求指纹，响应丢失/重启/并发不盲重发；不存确认令牌。
- 新增无保存副作用的默认上下文解析和周报草稿列表，查询过期状态不改变原计划/报告事实。
- 不变更策略、财务事实、后台来源、通知渠道或数据库结构。

# Changelog

## v0.31.8 — 2026-09-07

### Unreleased addition — 2026-09-16

- Connect Windows-local Codex directly to the existing MCP/Core with a read-only handshake and readiness preflight; no second ledger or Hermes relay.
- Deliver client-independent operating instructions during MCP initialization and attribute Codex requests accurately while retaining Hermes defaults and confirmation contracts.
- Preserve `-SkipHermes` across installer, updater, finalizer and rollback so Codex-only installations do not regain a Hermes dependency.
- Keep background scheduler/notification migration explicitly separate from connecting the interactive client.

### No-investment weeks

- Add a governed no-investment-week preview, draft, renewal and commit lifecycle without fabricating an allocation plan.
- Commit one explicit seven-day zero-contribution decision as `SKIPPED` with `decision_kind=NO_INVESTMENT`, zero execution, the full weekly budget abandoned and no carry-forward.
- Reject overlapping non-expired plans, posted BUY transactions and active external subscriptions at preview, renewal and commit; expired unconfirmed plan drafts do not become plan facts.
- Keep no-investment records eligible for the existing immutable formal weekly-report workflow while creating no transaction, subscription, holding, cash or strategy facts.
- Teach Hermes to use this path when the user skips a whole week and to preserve the same draft identity across expiry.

## v0.31.7 — 2026-09-03

- Add governed weekly-report previews, confirmation drafts, immutable formal versions and exact weekly-plan binding.
- Persist every plan's own start and end dates and preserve `EXECUTED`, `SKIPPED`, and `PARTIALLY_EXECUTED_CLOSED` outcome semantics.
- Store formal weekly reports in `report_bundles`, keep regeneration history and reasons, and make manual backfill and automation idempotent.
- Keep deterministic transaction facts available when exact end-date valuation evidence is missing or insufficient; never substitute current or adjacent-date NAV.
- Surface missing, failed, generated and valuation-limited weekly-report states in the investment workspace without sending notifications automatically.

## v0.31.6 — 2026-09-02

- Add `PARTIALLY_EXECUTED_CLOSED` as an explicit terminal outcome that preserves planned, executed and abandoned amounts without pretending full execution or a skipped plan.
- Require a separately created, short-lived and renewable closure draft followed by exact user confirmation; reject active subscriptions, confirmations and still-submittable related drafts.
- Release the abandoned remainder from future-plan blocking and prior commitments without carrying it into the next weekly budget.
- Show the terminal outcome, execution rate, reason and no-carry-forward fact in weekly reports while removing it from daily action-required work.
- Add a version-controlled `investor-core-operations` skill source and install/update it alongside the primary Investor skill.

## v0.31.5 — 2026-09-02

- Post an external-subscription BUY at the submitted gross cash cost while preserving confirmed net amount and fee as separate audited facts.
- Use the same gross amount for holding cost, derived cash and frozen-plan execution, without charging the fee a second time; shares, NAV and NAV date remain unchanged.
- Reject posting when submitted gross, confirmed net and fee differ by more than the existing one-minor-unit currency rounding tolerance.
- Add an explicit, external-subscription-only transaction-draft revision operation that preserves draft and business identity, rotates the credential, and creates no financial fact.
- Mark migrated linked drafts with their external-subscription origin so the generic transaction endpoint cannot bypass the governed posting contract.

## v0.31.4 — 2026-09-01

- Add structured `EXACT` and `DATE_ONLY` precision for external-subscription confirmation drafts and committed confirmation facts.
- Normalize date-only confirmations to business-date midnight internally while exposing a date-only display value and never presenting the normalized timestamp as a sourced confirmation time.
- Add an explicit, audited and concurrency-safe revision operation for uncommitted `PENDING` or `EXPIRED` confirmation drafts while preserving draft, subscription, idempotency and order identity.
- Rotate the confirmation credential after a successful revision, require an expected payload hash, and create no confirmation, transaction, holding, cash or plan-execution fact.
- Migrate historical confirmation facts and drafts to the backward-compatible `EXACT` classification without guessing precision from midnight timestamps.

## v0.31.3 — 2026-08-28

- Add an explicit, audited and concurrency-safe renewal operation for expired, uncommitted external-subscription drafts while preserving the draft ID, idempotency key, payload and payload hash.
- Rotate the confirmation digest and expiry in one conditional transaction so concurrent renewal attempts produce at most one new valid token and immediately invalidate the old token.
- Derive `EXPIRED` on reads after expiry and direct expired commits to the supported renewal operation.
- Set the configurable default TTL for external-subscription `SUBMIT` and `CONFIRM` drafts to 24 hours without extending existing drafts or creating any subscription or financial fact.

## v0.31.2 — 2026-08-14

- Rename the stored instrument registration classification to `registration_role` and expose the active portfolio configuration as `strategy_role`.
- Keep the legacy `role` response field as an explicitly deprecated compatibility alias whose meaning is documented per endpoint.
- Replace immediate strategy-role mutation with a governed draft that still requires the existing explicit commit confirmation.
- Make strategy configuration authoritative for holdings, reports, workspace views and weekly plans; registration metadata is never used as a portfolio decision fallback.
- Add migration and regression coverage for 040046, 000083 and 003765 role mismatches without reading or changing production data.

## v0.31.1 — 2026-08-11

- Distinguish omitted nullable strategy fields from explicit clearing in API drafts and expose explicit MCP clear-field semantics.
- Add short-lived, separately confirmed skip drafts for existing frozen weekly plans without recovering or bypassing their original credential.
- Align plan-list, workspace and readiness facts so a frozen plan remains active, action-required and future-plan-blocking until executed or explicitly skipped.
- Preserve the prohibition on automatic strategy changes, plan closure, subscription creation and trading.

## v0.31.0 — 2026-08-07

- Add a governed lifecycle for externally submitted fund subscriptions, pending cash, partial confirmations, cancellations, corrections and explicit ledger posting.
- Reserve unfinished plan and subscription amounts from later weekly allocation without treating them as holdings.
- Extend daily, weekly and readiness views with submitted, pending, confirmed-unbooked and remaining amounts.
- Allow a valid CORE contribution allowlist to support the minimum business loop while SATELLITE direct contribution remains intentionally closed.
- Preserve the prohibition on broker execution, automatic confirmation, automatic trading and inferred external facts.

## v0.30.0 — 2026-08-06

- Added incremental links from frozen weekly plans to separately confirmed real BUY records.
- Added `PARTIALLY_EXECUTED`, per-fund accumulated execution, remaining amounts, cross-date fills and reversal-aware reopening.
- Added read-only daily and weekly execution progress without creating trades or changing frozen plans.

## v0.29.2 — 2026-08-05

- Aligned runtime, package, readiness and release-manifest version reporting.

## v0.29.1 — 2026-08-05

- Fixed Windows Core supervisor ownership during governed upgrades.

## v0.29.0 — 2026-08-04

- Added governed satellite PE/PB signal policy and immutable signal snapshots without automatic trading or strategy changes.
