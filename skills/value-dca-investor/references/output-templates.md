# Output templates

## Query

1. Conclusion.
2. Key facts with the as-of date.
3. Data quality and limitations.

## Confirmation preview

1. Action and target.
2. Amount, shares, date, or state change exactly as returned by Core.
3. Consequences and expiry.
4. A request for explicit confirmation.

## Opening-position confirmation preview

1. State `期初持仓导入（不是买入交易）` and identify the account and instrument.
2. Show the as-of date, platform-reported total shares and supplied cost basis. Use the user-facing
   label `账面成本（按平台显示的份额和成本价换算）` for a calculated total; do not say
   `Core 推导总成本`. Disclose rounding to CNY 0.01 in one short note.
3. State that holdings change only after the exact opening-position commit tool succeeds.
4. Show the draft ID and expiry, disclose any source warning, and request explicit confirmation.

## Sell proposal

1. Trigger and evidence.
2. Thesis and proxy applicability.
3. Fees, portfolio impact, risk of holding, and fund destination.
4. Proposal state and `未执行交易` notice.
5. Available decisions: approve, defer, or reject.

## Scheduled report

Return `[SILENT]` when the job contract says to remain silent and no qualifying change exists.
Otherwise provide conclusion, changed facts, warnings, and the one next action the user may take.
