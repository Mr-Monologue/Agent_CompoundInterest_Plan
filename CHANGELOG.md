# Changelog

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
