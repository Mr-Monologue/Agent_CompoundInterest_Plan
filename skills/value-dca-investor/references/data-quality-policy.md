# Data quality policy

| Grade | Meaning | Response rule |
|---|---|---|
| `PASS` | Verified or official data within the allowed freshness window | Explain normally |
| `WARNING` | Single source, estimated value, short delay, weak proxy, or non-critical gap | State the limitation and use only Core-provided conservative results |
| `SOURCE_ERROR` | Missing, conflicting, stale, or unparseable critical data | Do not give an amount-based conclusion |

Market value is available only from committed shares multiplied by a stored NAV snapshot. Every
snapshot must retain its NAV date, observation timestamp, source identity, source type, and
verification status. An `OFFICIAL` or `PLATFORM` observation is `PASS` only after its evidence is
verified. Aggregator, user-entered, or unverified observations are `WARNING` even when parseable.
An aggregator NAV becomes `PASS` only when Core records an exact same-date, same-value `MATCH`
against an independently named `OFFICIAL` or `PLATFORM` observation with a non-empty evidence
reference. Both immutable snapshots and the verification link remain auditable. Any different
same-date value is `SOURCE_ERROR`; never average, choose between, or silently overwrite conflicts.

For a portfolio valuation, a missing or stale NAV for any non-zero holding makes the aggregate
quality `SOURCE_ERROR`; portfolio market value, P&L, return, and weights must then remain absent.
Historical cost composition may still be labeled explicitly as a ledger fact, never as live weight.

For proxy valuation, disclose `STRONG`, `WEAK`, or `NOT_APPLICABLE`. Never use a `WEAK` proxy as
the only basis for a sell conclusion. Do not calculate PE percentiles for `NOT_APPLICABLE` assets.

Runtime levels are Core capability facts: L0 is full deterministic operation; L1 permits only the
warning-qualified capabilities returned by Core; L2 is ledger/facts-only; L3 is safety and system
health only. Never continue a valuation, plan, risk scan, review, or explanation when its
capability is false. Degradation cannot enable a mutation or automatic trade.

Official history backfill must preserve the source name, independent lineage, evidence reference,
observation timestamp and every original NAV date. A batch conflict is `SOURCE_ERROR`; never
average, overwrite, or hide it. `EASTMONEY` aliases remain aggregator evidence and cannot be
relabeled `FUND_MANAGER_OFFICIAL`.

Performance, loss, market-value weight, and an instrument role are observations, not allocation or
sell rules. Without a Core-supplied target, threshold, trigger state, and reason code, describe the
numbers neutrally and do not label a weight inadequate or a loss as a triggered risk action.
