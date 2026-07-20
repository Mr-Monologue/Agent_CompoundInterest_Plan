# Safety policy

## State-changing requests

Portfolio, account, and instrument setup tools are immediate idempotent configuration mutations.
Call them only with user-supplied identifying attributes, disclose that they do not move money or
change holdings, and stop on any setup conflict. An `INDEX` record is a benchmark and must never be
used as the instrument in a BUY or SELL transaction draft.

Use the exact draft tool for the requested action. Present the returned preview, object ID, expiry,
and material effects. Ask for an explicit confirmation that uniquely identifies the pending draft.
Commit only with the matching one-time token. If content changed or the token expired, create a new draft.

An opening position is a historical balance import, not a purchase. Use
`opening_position_draft_create` only when the user supplied the exact platform-reported as-of date,
shares, and exactly one cost basis: total cost amount or per-share average cost. Never send both.
Show the supplied cost basis, every Core-derived value, and any currency-rounding warning, then use only
`opening_position_draft_commit` after explicit confirmation. Never substitute
`transaction_draft_create`, never infer missing values, and never import an `INDEX`. An opening
position must be the first active ledger event for that account and instrument.

Do not expose confirmation tokens in logs or unrelated messages.

## Sell lifecycle

Maintain these states separately:

1. A rule creates a sell proposal.
2. The user approves, defers, or rejects the proposal.
3. The user executes outside the system.
4. A separately confirmed `SELL` transaction records the actual execution.

Only step 4 changes holdings. Always state `未成交` when the context reports
`execution_status = NOT_EXECUTED`.

## Scheduled jobs

Use only read-only tools. Never freeze a plan, approve a proposal, acknowledge an alert, update a
review item, or commit a transaction from a Cron Agent session.
