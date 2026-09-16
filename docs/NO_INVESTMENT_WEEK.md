# Governed no-investment weeks

v0.31.8 records an explicit decision not to invest during one seven-day period without fabricating
an allocation plan.

## Business meaning

A committed no-investment decision creates one terminal weekly-plan record with:

- `status=SKIPPED` and `decision_kind=NO_INVESTMENT`;
- the user's positive weekly budget as the planned budget;
- zero execution, the full budget abandoned, and `carry_forward=false`;
- no plan items, transaction, subscription, holding, cash, or strategy change.

The record uses the exact supplied `period_start` and the following six days as `period_end`. A past
or current week may be recorded; a future week cannot be pre-skipped.

## Governed lifecycle

1. Preview the exact period, budget, reason, and note.
2. Create a short-lived confirmation draft.
3. Show the preview and obtain explicit user confirmation.
4. Commit the exact draft, or renew the same draft after expiry and reconfirm.
5. Generate the formal weekly report through the existing weekly-report lifecycle.

The API endpoints are:

- `GET /v1/weekly-no-investment-preview`;
- `POST /v1/weekly-no-investment-drafts`;
- `GET /v1/weekly-no-investment-drafts/{draft_id}`;
- `POST /v1/weekly-no-investment-drafts/{draft_id}/renew`;
- `POST /v1/weekly-no-investment-drafts/{draft_id}/commit`.

Hermes receives matching `weekly_no_investment_*` MCP tools.

## Conflict checks

Draft creation and commit require all of the following for the exact portfolio, account, and
period:

- no overlapping plan other than an expired, unconfirmed plan draft;
- no posted, unreversed BUY transaction;
- no active external subscription.

These facts are hashed into the draft. If they change before commit or renewal, Core rejects the
operation. Multiple idempotency identities cannot be used to create parallel drafts for the same
period.

## Reporting and automation

The committed record is a normal final-report-eligible `SKIPPED` plan. Its report states the weekly
budget, zero execution, full abandoned amount, no carry-forward, reason, and empty item list. The
existing idempotent `WEEKLY_REPORT` automation can generate its formal report. Report creation does
not send a notification or execute any investment.
