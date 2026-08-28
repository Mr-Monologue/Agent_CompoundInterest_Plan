# External Subscription Lifecycle

v0.31.0 records facts about fund subscriptions that Ryan has already submitted on an
external platform. It does not connect to that platform and does not place an order.

## State contract

- `SUBMITTED`: the user confirmed that the external submission happened.
- `PENDING_CONFIRMATION`: the platform explicitly reported that processing is pending.
- `PARTIALLY_CONFIRMED`: one or more confirmation facts exist and an amount remains pending.
- `CONFIRMED`: the full requested cash amount is explained by principal, fees and refunds.
- `CANCELLED`: the user or platform explicitly cancelled the still-pending amount.
- `REJECTED`: a sourced platform rejection fact exists. Time alone never creates this state.

Every write uses an expiring draft, an exact confirmation token, an idempotency key and
an audit event. Corrections are appended as reversal facts; history is not overwritten.

## Expiry and renewal contract

From v0.31.3, `SUBMIT` and `CONFIRM` drafts have a configurable 24-hour default TTL.
Deployments may override it with
`INVESTOR_EXTERNAL_SUBSCRIPTION_CONFIRMATION_TTL_MINUTES` (1 to 10080 minutes).
Changing that setting affects only newly created or explicitly renewed drafts; it never
extends an existing row. An uncommitted draft whose `expires_at` has passed is exposed as
`EXPIRED`, even when its stored pre-migration status is still `PENDING`.

`POST /v1/external-subscription-drafts/{draft_id}/renew` and the MCP tool
`external_subscription_draft_renew` are the only supported recovery path. Renewal is
allowed only after expiry and before a successful commit. It keeps the draft ID,
idempotency key, payload and payload hash unchanged, then atomically rotates the token
digest, sets a new expiry, increments `renewal_count`, and records `renewed_at` plus an
audit event. The old token stops working immediately.

The renewal transaction uses a conditional update while holding a write transaction.
Consequently, concurrent calls can produce at most one new valid token. Active drafts,
committed drafts, changed payloads and non-renewable states are rejected. The renewal
request accepts no payload, order number, subscription ID or idempotency-key replacement.
It creates no subscription, confirmation, transaction, plan-execution link, cash event or
holding fact.

## Cash and holding semantics

Amounts use integer minor currency units. NAV and shares use integer millionths. The
subscription invariant is:

`requested = confirmed principal + fee + pending + cancelled or refunded`

The external platform facts available for this product treat fees as part of the cash
submitted. Therefore principal plus fee consumes the frozen plan amount. The existing
BUY ledger continues to record principal, NAV and shares; the plan link records the cash
amount of principal plus fee. Pending and confirmed-but-unposted amounts reserve plan
capacity but never change holdings.

A confirmation is posted to the BUY ledger only after a second, explicit user
confirmation. That posting reuses the v0.30.0 transaction ledger and partial plan
execution rules.

## Cross-week behavior

An unfinished earlier frozen plan remains an outstanding commitment. A later weekly
preview suppresses the corresponding amount before allocation and reports the requested,
outstanding, suppressed and newly available amounts. Cancellation or refund releases the
amount back to the earlier plan's unsubmitted remainder; it never creates another order.

An optional expected confirmation date can mark an item for review. It cannot infer a
failure, cancellation or rejection.

## Migration and downgrade boundary

Migration `0028_external_subscription_lifecycle` only creates new tables and indexes. It
does not rewrite existing plans, holdings, transactions or strategy configuration. A
downgrade removes the v0.31.0 subscription facts, so production downgrade requires an
export and explicit operator decision; it is not an automatic data-preserving rollback.

Migration `0031_external_subscription_draft_renewal` adds only nullable `renewed_at` and
non-negative `renewal_count` audit fields. Existing drafts retain their IDs, payloads,
digests, expiries and stored statuses; the read API derives expiry without rewriting them.
