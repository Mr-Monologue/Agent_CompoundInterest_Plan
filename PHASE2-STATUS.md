# Phase 2 market data and valuation status

Date: 2026-07-21
Release target: 0.6.0

## Implemented in this increment

- Immutable `market_nav_snapshots` records with exact six-decimal NAV storage.
- Source type, source identity, source reference, verification state and timestamps.
- Content-hash idempotency and an audit event for every newly stored observation.
- Deterministic valuation from committed holding shares and the latest eligible NAV.
- Per-position market value, unrealized P&L, return and market-value weight.
- Portfolio-level `PASS`, `WARNING` and `SOURCE_ERROR` propagation.
- Aggregate amount suppression when any non-zero holding NAV is missing or stale.
- REST and MCP tools for NAV recording, NAV history and portfolio valuation.
- Saved investment-context resolution for valuation without user-managed UUIDs.

## Deliberately not implemented yet

- Automatic calls to AKShare, Wind, fund platforms or other external providers.
- Automatic promotion of user-entered or aggregator data to `VERIFIED`.
- Index PE/PB percentile calculations or fund-to-index proxy mappings.
- DCA amount recommendations, weekly plans, risk rules or sell proposals.
- Broker/fund-platform order execution or automatic transaction confirmation.

## Next target-host gate

1. Upgrade a backed-up 0.5.3 database to revision `0004_market_nav`.
2. Confirm six committed holdings remain unchanged after migration.
3. Record six sourced NAV observations through Hermes.
4. Verify `portfolio_valuation_get` returns market-value weights only when all six NAVs qualify.
5. Verify one missing or stale NAV produces `SOURCE_ERROR` with no aggregate amounts.
