# Phase 2 market data and valuation status

Date: 2026-07-22
Release target: 0.7.0

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
- Canary-gated `AKSHARE_OPEN_FUND` adapter using confirmed open-fund unit NAV history.
- Locked AKShare dependency with recorded library and adapter contract versions.
- Default-context synchronization for all committed FUND holdings without user-managed codes.
- Provider health and sync-run audit records, including per-instrument raw summary hashes.
- Bounded parallel fetch, provider timeout handling and partial-run `SOURCE_ERROR` propagation.
- Real canary and six-current-holding contract checks against AKShare 1.18.72.

## Deliberately not implemented yet

- Second-source or official fund-company cross-validation.
- Automatic promotion of user-entered or aggregator data to `VERIFIED`.
- Unattended daily market sync and official NAV backfill Cron.
- Index PE/PB percentile calculations or fund-to-index proxy mappings.
- DCA amount recommendations, weekly plans, risk rules or sell proposals.
- Broker/fund-platform order execution or automatic transaction confirmation.

## Next target-host gate

1. Upgrade a backed-up 0.6.0 database to revision `0005_market_data_sync`.
2. Confirm six committed holdings remain unchanged after migration.
3. Ask Hermes for the current investment situation without naming internal tools.
4. Verify Hermes autonomously runs market sync and valuation for all six holdings.
5. Confirm all snapshots remain `WARNING` until an independent source corroborates them.
6. Verify one provider failure produces `SOURCE_ERROR` with no aggregate amount conclusion.
