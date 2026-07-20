# Phase 1 implementation status

Date: 2026-07-20  
Release: 0.5.0

## Implemented

- Exact integer storage for CNY amounts, NAV values and shares.
- Portfolio, account and instrument setup records.
- Expiring transaction drafts with hashed confirmation tokens.
- Idempotent draft creation and idempotent committed-response replay.
- Confirmed BUY/SELL recording without any broker execution capability.
- Deterministic holding reconstruction with average-cost reduction on sells.
- Confirmed reversal records that preserve the original audit history.
- Guarded REST and MCP read/draft/commit boundaries.
- Skill capability-inventory and source-attribution hardening.
- Idempotent Windows installer/upgrader baseline.
- Windows preflight detection for Core/MCP processes that lock the virtual environment.
- Hidden per-user Windows scheduled task for Core startup and process supervision.
- Guarded MCP on-demand Core recovery with one readiness wait and one request retry.
- Disabled-by-default Hermes health-watch Cron template with `[SILENT]` success behavior.
- Atomic Hermes MCP profile updates without unsupported interactive prompt piping.
- Process-owned rotating Core logs and bounded Windows supervisor restart behavior.
- Idempotent Hermes onboarding tools for portfolios, accounts and instruments.
- Non-tradable index benchmark enforcement at the deterministic ledger boundary.
- Dedicated opening-position draft and explicit commit workflow for legacy holdings.
- `OPENING` ledger events that remain distinct from `TRADE` records.
- First-event, non-future-date, non-index and trade-date ordering guards.
- Alembic revision verification in Core readiness checks.
- Mutually exclusive total-cost or average-cost-NAV input for opening positions.
- Deterministic CNY-cent derivation and explicit rounding warnings for platform cost prices.
- Locked IANA timezone data for Windows and a readiness check for the business timezone.
- GitHub stable Releases as the only unattended Windows update source.
- One-command GitHub bootstrap with no user-managed release archives.
- Daily hidden update checks with release-manifest policy gates.
- Verified pre-migration SQLite backups and automatic code/database rollback attempts.
- Public-repository hygiene that excludes personal databases, environment files and logs.

## Explicitly disabled

- Broker or fund-platform order execution.
- Automatic confirmation.
- Cron mutations.
- Weixin/ClawBot integration.
- AKShare, Wind or other market-data adapters.
- Valuation, weekly plan and recommendation tools.

## Validation completed

- 48 automated API, CLI, migration, ledger, MCP runtime, Windows contract and safety tests passed.
- BUY draft, explicit commit, duplicate commit, SELL, expiry and reversal paths passed.
- Phase 0 to Phase 1 migration preserved existing audit data.
- Static type analysis completed with zero errors or warnings.
- Real Core process smoke test passed for `/health`, `/ready` and rotating access logs.
- Wheel and source distribution builds passed.

## Target-host gate still open

- Release 0.2.0 installed successfully and upgraded the Phase 0 database to Phase 1.
- Release 0.2.2 hidden runtime and Hermes-triggered automatic recovery passed on Windows.
- Release 0.3.0 real portfolio, account, benchmark and fund onboarding passed through Hermes.
- Release 0.4.2 passed timezone readiness and one real opening-position commit for fund 005827.
- Release 0.5.0 GitHub bootstrap and updater remain to be validated on the target Windows host.

## Next gate

Merge and publish release 0.5.0, run the one-command GitHub bootstrap on the target host, then
verify both scheduled tasks and a no-op update check. Continue importing legacy holdings one at a time.
