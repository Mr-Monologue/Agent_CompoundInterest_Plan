# Changelog

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
