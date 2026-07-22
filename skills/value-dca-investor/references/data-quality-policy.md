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

For a portfolio valuation, a missing or stale NAV for any non-zero holding makes the aggregate
quality `SOURCE_ERROR`; portfolio market value, P&L, return, and weights must then remain absent.
Historical cost composition may still be labeled explicitly as a ledger fact, never as live weight.

For proxy valuation, disclose `STRONG`, `WEAK`, or `NOT_APPLICABLE`. Never use a `WEAK` proxy as
the only basis for a sell conclusion. Do not calculate PE percentiles for `NOT_APPLICABLE` assets.

At model degradation L2 or L3, deliver Core facts and deterministic templates only. State that the
explanation layer is degraded; never fill the missing narrative with guesses.
