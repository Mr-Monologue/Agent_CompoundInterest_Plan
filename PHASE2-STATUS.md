# Phase 2 market data and valuation status

Date: 2026-07-22
Release target: 0.7.2

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
- Immutable cross-source verification links between a primary aggregator observation and an
  independently sourced `OFFICIAL` or `PLATFORM` observation.
- Exact same-date/same-value matching that upgrades valuation quality to `PASS` without mutating
  either source snapshot.
- Conflict evidence that remains stored and forces `SOURCE_ERROR` with no portfolio amount totals.
- Provider-neutral MCP verification bridge for connected Wind/professional or official tools.
- Deterministic portfolio brief with facts-only narration and machine-readable capability gates.
- Explicit `NOT_AVAILABLE` allocation, risk, sell, weekly-plan and role-update assessments.
- Upstream publisher lineage that treats AKShare, Eastmoney and 天天基金 as one source.

## Deliberately not implemented yet

- Bundled credentials or proprietary implementation for a second-source data vendor.
- Automatic promotion of user-entered data or same-upstream endpoints to independently verified.
- Unattended daily market sync and official NAV backfill Cron.
- Index PE/PB percentile calculations or fund-to-index proxy mappings.
- DCA amount recommendations, weekly plans, risk rules or sell proposals.
- Broker/fund-platform order execution or automatic transaction confirmation.

## Next target-host gate

1. Upgrade a backed-up 0.7.1 database to revision `0007_source_lineage`.
2. Confirm six committed holdings remain unchanged after migration.
3. Ask Hermes for the current investment situation without naming internal tools.
4. Verify Hermes autonomously runs market sync and valuation for all six holdings.
5. Confirm primary-only snapshots remain `WARNING` when no independent tool is available.
6. With a connected professional or official data tool, confirm exact matches become `PASS` and
   all evidence appears in `market_nav_verification_list`.
7. Inject one isolated mismatch and verify it produces `SOURCE_ERROR` with no aggregate amount
   conclusion.
