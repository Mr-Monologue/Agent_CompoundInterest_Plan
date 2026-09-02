# Partial weekly-plan closure

Version 0.31.6 adds a governed terminal outcome for a weekly plan that has real execution but whose
positive remainder will not be executed or carried forward.

## State and accounting contract

The only supported transition is:

`PARTIALLY_EXECUTED -> PARTIALLY_EXECUTED_CLOSED`

The terminal state preserves the original plan, linked transactions and item-level amounts. At
commit time, `executed_amount + abandoned_amount = planned_amount`; `abandoned_amount` equals the
remaining amount and `carry_forward` is always false. It is not full execution and is not a skipped
plan. It creates no subscription, transaction, cash, holding, strategy or successor-plan fact.

A closed partial plan no longer blocks future plans and its abandoned amount is excluded from prior
commitments. A later weekly preview therefore uses only the newly requested budget; it never adds
the abandoned remainder.

## Governed API

- `POST /v1/weekly-plans/{plan_id}/partial-close-drafts` creates an exact preview and short-lived
  confirmation credential.
- `GET /v1/weekly-plan-partial-close-drafts/{draft_id}` returns the preview and derived expiry state.
- `POST /v1/weekly-plan-partial-close-drafts/{draft_id}/renew` rotates only an expired, unchanged
  draft credential.
- `POST /v1/weekly-plan-partial-close-drafts/{draft_id}/commit` atomically applies the terminal
  outcome after explicit confirmation and supports idempotent replay.

The matching MCP tools use the `weekly_plan_partial_close_draft_*` names. Draft creation and commit
are rejected unless the plan has positive executed and remaining amounts, exact amount conservation,
no excess execution, no in-flight subscription and no related unexpired submittable draft.

Formal reason codes are `PLATFORM_LIMIT_REMAINDER_ABANDONED`,
`PERIOD_ENDED_REMAINDER_ABANDONED`, and `USER_DECLINED_REMAINDER`. A user-readable note and closure
business date are mandatory.

## Migration and reports

Migration `0034_partial_plan_closure` adds closure audit fields and the governed draft table.
Historical `PARTIALLY_EXECUTED` plans remain unchanged and require an explicit future decision.
Downgrade reopens a closed partial plan as `PARTIALLY_EXECUTED` because the earlier schema cannot
represent the terminal outcome.

Weekly reporting retains planned/executed/remaining amounts and execution rate and labels the result
“部分执行后结束” with the reason and no-carry-forward outcome. The daily workspace excludes this
terminal state from actionable or future-plan-blocking work.

## Skill deployment

`skills/investor-core-operations/SKILL.md` is the authoritative operational source for the v0.31.5
gross-cost posting contract and the v0.31.6 partial-close workflow. The Windows installer and release
updater copy both version-controlled skills into the selected Hermes profile. Runtime profile copies
are deployment artifacts, not independent sources, and must not be edited to create local drift.

## Hermes deployment handoff

Hermes owns production deployment and business confirmation. After v0.31.6 is published, Hermes
should run the existing `ValueDCAAgentUpdate` release upgrade, verify `/ready` reports version
`0.31.6` and database revision `0034_partial_plan_closure`, then reconnect the investor Gateway so
the four new `weekly_plan_partial_close_draft_*` tools and the updated skills are discovered.

Production acceptance should first be read-only: compare the existing partial plan, linked trades,
holdings, cash, subscriptions and prior commitments before and after migration. Only after that
comparison may Hermes create one closure draft for the exact historical plan, present its 200/170/30
preview and no-carry-forward effect, and wait for Ryan's separate explicit confirmation. Deployment
must not itself close the plan, generate a report, create a successor plan or change financial facts.
