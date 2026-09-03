# Governed weekly-plan reports

v0.31.7 adds immutable weekly reports bound to a specific weekly plan.

## Lifecycle

1. Preview any plan with `GET /v1/weekly-plans/{plan_id}/report-preview`.
2. For a terminal plan, create a draft with
   `POST /v1/weekly-plans/{plan_id}/report-drafts`.
3. Read the draft and explicitly confirm its exact facts.
4. Commit it with `POST /v1/weekly-report-drafts/{draft_id}/commit`.
5. Query immutable versions through `/v1/weekly-reports` and `/v1/weekly-reports/{id}`.

The matching MCP tools are `weekly_report_preview`, `weekly_report_draft_create`,
`weekly_report_draft_get`, `weekly_report_draft_renew`, `weekly_report_draft_commit`,
`weekly_report_list`, and
`weekly_report_get`.

Only `EXECUTED`, `SKIPPED`, and `PARTIALLY_EXECUTED_CLOSED` are final-report eligible.
Regeneration requires a reason, creates a new version, preserves the old version, and moves the
current pointer atomically. Report creation is read-only with respect to investments and defaults
to silent delivery.

## Facts and valuation

The report period is copied from the plan's persisted start and end dates. The report includes
per-fund planned, executed and unexecuted amounts; linked BUY facts; external subscription gross
amounts; confirmation net amounts and fees; shares, NAV dates, confirmation dates and time
precision; closure reason and carry-forward state.

End valuation requires an exact NAV observation on the plan end date for every open holding.
Current or adjacent-date NAV is never substituted. Missing, conflicting, or unverified evidence
produces `FACTS_COMPLETE_VALUATION_LIMITED`; the transaction-fact report is still valid.
`DATE_ONLY` confirmations never expose the normalized midnight timestamp.

## Automation and temporary files

The `WEEKLY_REPORT` automation checks terminal plans without a current formal report. It is
idempotent and creates no notification delivery by default. Historical reports use the same store.

The pre-existing local file `reports/weekly-2026-08-18.md` is not imported automatically and does
not count as a formal report. Its presence must not suppress formal generation. After a formal
report is generated and checked, the user may separately decide whether to retain that file.
