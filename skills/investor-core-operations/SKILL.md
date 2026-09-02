---
name: investor-core-operations
description: Apply governed Investor Core operational workflows for external-subscription ledger posting and partial weekly-plan closure without executing investments.
---

# Investor Core Operations

This is the version-controlled operational contract installed with Investor Core. Runtime copies
must be refreshed by the release installer/updater and must not be edited as an independent source.

## External subscription transaction posting

- Treat the submitted amount as the actual gross cash paid by the user.
- A confirmation's confirmed amount is the net amount that acquired shares; keep its fee separate.
- The generated BUY transaction amount and holding-cost increment use gross submitted amount.
- Count the same gross amount toward weekly-plan execution. Never add or deduct the fee twice.
- Require gross amount to equal confirmed net amount plus fee within Core's Decimal money-rounding
  contract. Stop on a mismatch; do not infer a correction.
- Correct an uncommitted wrong transaction draft only through the formal revision tool. Preserve the
  subscription, confirmation, draft, order identity, and idempotency identity; rotate the token and
  invalidate the old one. Never create a replacement identity to bypass governance.

## Ending a partially executed weekly plan

Use this workflow only when Core reports `PARTIALLY_EXECUTED`, executed amount is positive, remaining
amount is positive, and the user explicitly abandons the remainder without carry-forward.

1. Read the plan and show period, planned/executed/remaining amounts, item-level progress, in-flight
   facts, closure reason, `carry_forward=false`, and future blocking after closure.
2. Create `weekly_plan_partial_close_draft_create` with a formal reason code and user-readable note.
   This is preview-only and creates no transaction, subscription, cash, or holding facts.
3. Ask for explicit user confirmation. A general “continue” is not approval.
4. On confirmation, commit the exact draft with
   `weekly_plan_partial_close_draft_commit`. The result must be
   `PARTIALLY_EXECUTED_CLOSED`, preserve planned/executed/remaining amounts, record the remaining
   amount as abandoned, keep `carry_forward=false`, and stop blocking future plans.
5. If the token expires, call `weekly_plan_partial_close_draft_renew`. Renew only the unchanged
   expired draft; never recover or reuse the old token.

Never use this flow for zero execution (`SKIPPED` governs that case) or full execution (`EXECUTED`
governs it). Never fabricate execution, delete plan items, carry abandoned money into another week,
or generate a new weekly plan automatically.
