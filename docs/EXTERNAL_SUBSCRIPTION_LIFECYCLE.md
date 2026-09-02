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

## Confirmation time precision

From v0.31.4 every confirmation draft and committed confirmation carries
`confirmed_at_precision`:

- `EXACT` means the source supplied a real timezone-aware confirmation timestamp. Its
  date in the configured business timezone must equal `confirmation_business_date`.
- `DATE_ONLY` means the source supplied only the confirmation date. Core stores business
  date midnight as a normalized internal timestamp for ordering compatibility, but
  `confirmation_business_date` is authoritative. API and MCP output include
  `confirmed_at_precision=DATE_ONLY`, `confirmed_at_is_exact=false`, and a
  `confirmed_at_display` containing only the date. User-facing reports must use the
  display value and must not describe the normalized timestamp as a sourced time.

Existing API and MCP clients that omit precision retain the old contract: precision
defaults to `EXACT` and `confirmed_at` remains required. A new date-level confirmation
must explicitly pass `DATE_ONLY`; it may omit `confirmed_at`, in which case Core creates
the normalized internal value. Migration classifies pre-v0.31.4 rows and CONFIRM drafts
as `EXACT` for backward compatibility. This is not an inference from a midnight value and
does not prove that every historical source exposed time-of-day precision.

## Uncommitted confirmation draft revision

`POST /v1/external-subscription-confirmation-drafts/{draft_id}/revise` and MCP tool
`external_subscription_confirmation_draft_revise` revise only an uncommitted CONFIRM
draft whose state is `PENDING` or `EXPIRED`. The request must carry the latest
`expected_payload_hash`. Omitted confirmation fields retain their current values, so a
precision-only correction can pass only `DATE_ONLY`; supplied fields still undergo the
complete timestamp, amount/share, subscription-state and business-date validation.

The transaction preserves `draft_id`, `subscription_id`, `idempotency_key` and the saved
external reference. Account, fund and plan identity continue to come from that immutable
subscription. A successful revision replaces payload and hash, increments
`revision_count`, records `revised_at` plus old-to-new hash audit evidence, rotates the
confirmation digest and returns one new 24-hour credential. The previous token fails
immediately. A stale expected hash, committed draft, wrong action, invalid payload or
concurrent loser is rejected. A no-op revision is also rejected so revision cannot be
used as an implicit renewal operation.

Revision creates no confirmation, transaction, holding, cash or plan-execution fact.
The caller must show the revised business preview and obtain a fresh explicit user
confirmation before calling the existing commit operation.

## Cash and holding semantics

Amounts use integer minor currency units. NAV and shares use integer millionths. The
subscription invariant is:

`requested = confirmed principal + fee + pending + cancelled or refunded`

The external platform facts available for this product treat fees as part of the cash
submitted. `requested_amount` is therefore submitted gross cash,
`confirmed_amount` is the net amount that acquired units, and `fee` remains a separate
audited fact. From v0.31.5 an external-subscription BUY records gross cash as the ledger
transaction amount. The same gross amount increments holding cost, reduces derived cash
and consumes the frozen plan; the fee is not deducted or added again. Confirmed shares,
NAV and NAV date are unchanged, and trade date remains NAV date.

Core compares all three values in integer minor units. A fully attributable confirmation
must satisfy `submitted gross ~= confirmed net + fee`; the existing currency-rounding
tolerance is one minor unit. A larger difference returns an auditable mismatch containing
gross, net, fee and the difference, and creates no transaction, holding, cash or plan fact.
This scoped rule does not change the amount contract of ordinary trades or opening
positions.

A confirmation is posted to the BUY ledger only after a second, explicit user
confirmation. That posting reuses the v0.30.0 transaction ledger and partial plan
execution rules.

## Uncommitted external transaction draft revision

`POST /v1/external-subscription-confirmations/{confirmation_id}/transaction-drafts/{draft_id}/revise`
and MCP tool `external_subscription_transaction_draft_revise` are the only supported way
to repair a pre-v0.31.5 uncommitted net-amount BUY draft. The operation accepts the latest
`expected_payload_hash` and the gross amount the caller expects Core to derive. It is
limited to the transaction draft already linked to that confirmation and cannot replace
the confirmation, subscription, account, instrument, order reference, idempotency key or
trade facts.

Revision is allowed only for an uncommitted `PENDING` or `EXPIRED` draft. Core holds a
write transaction, verifies the old hash and immutable identity, revalidates gross, net,
fee, shares, NAV and NAV date, and performs a conditional update. A successful revision
keeps the draft ID and idempotency key, stores gross as transaction amount, increments the
revision audit counter, records old and new hashes, rotates the credential and expiry, and
invalidates the old token immediately. Concurrent callers can produce at most one result.
Committed drafts, stale hashes, wrong gross amounts and mismatched business identities are
rejected.

Revision creates no transaction, holding, cash or plan-execution fact. The revised preview
must be shown to the user and requires a fresh explicit confirmation before the scoped
external-subscription commit endpoint may post it. The generic transaction commit endpoint
rejects external-subscription-origin drafts.

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

Migration `0032_confirmation_time_precision_revision` adds precision to committed
confirmations and adds precision, `revised_at` and non-negative `revision_count` to
drafts. It adds the backward-compatible `EXACT` field to existing CONFIRM payloads and
recomputes their payload hashes, while preserving draft IDs, idempotency keys, tokens,
expiries and commit state. Clients must read the post-upgrade draft before revision and
use its current hash. Downgrade removes structured precision and revision audit columns;
it cannot preserve a `DATE_ONLY` distinction, so production downgrade requires an
explicit operator decision.

Migration `0033_external_subscription_gross_transaction` adds transaction-draft origin
and revision audit fields and records gross, net and fee on the confirmation-to-transaction
link. Existing linked drafts retain their ID, idempotency key, payload, digest and expiry;
the migration marks their origin but deliberately does not silently rewrite a net amount.
After upgrade, callers must re-read an uncommitted legacy draft, use its current payload
hash and invoke the scoped revision operation. Downgrade removes the new metadata and does
not reverse a committed gross-cost transaction.
