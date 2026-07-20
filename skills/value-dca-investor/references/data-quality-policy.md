# Data quality policy

| Grade | Meaning | Response rule |
|---|---|---|
| `PASS` | Verified or official data within the allowed freshness window | Explain normally |
| `WARNING` | Single source, estimated value, short delay, weak proxy, or non-critical gap | State the limitation and use only Core-provided conservative results |
| `SOURCE_ERROR` | Missing, conflicting, stale, or unparseable critical data | Do not give an amount-based conclusion |

For proxy valuation, disclose `STRONG`, `WEAK`, or `NOT_APPLICABLE`. Never use a `WEAK` proxy as
the only basis for a sell conclusion. Do not calculate PE percentiles for `NOT_APPLICABLE` assets.

At model degradation L2 or L3, deliver Core facts and deterministic templates only. State that the
explanation layer is degraded; never fill the missing narrative with guesses.

