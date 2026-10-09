"""R1.1's two fixed small research cohorts. No investment eligibility or orders."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Any, Literal

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.r11_inputs import (
    VERSION,
    Calendar,
    Context,
    D,
    Series,
    base_output,
    down,
    sample_sd,
    up,
)
from investor_core.r11_replacement import (
    Corroboration,
    ReplacementEvidence,
    corroboration_gaps,
    platform_gaps,
    replacement_result,
)
from investor_core.r11_rules import rules
from investor_core.scheduler import digest

COHORTS = {"MEDICAL": ("003096", "009163"), "A500": ("022463", "022424")}


class Distribution(StrictModel):
    day: date
    cash_per_unit: Decimal = Field(ge=0)
    source: str


class Fees(StrictModel):
    source: str
    effective_from: date
    effective_to: date | None = None
    subscription_method: Literal["GROSS_DIVIDE_ONE_PLUS_RATE", "GROSS_TIMES_RATE", "FIXED"]
    subscription_value: Decimal = Field(ge=0)
    redemption_rate_pct_at_365_days: Decimal = Field(ge=0, le=100)
    standard_10000_365_day_scenario_supported: bool

    def loss_pct(self) -> Decimal:
        gross = D(10000)
        if self.subscription_method == "FIXED":
            net = gross - self.subscription_value
        elif self.subscription_method == "GROSS_TIMES_RATE":
            net = gross * (1 - self.subscription_value / 100)
        else:
            net = gross / (1 + self.subscription_value / 100)
        if not 0 < net <= gross:
            raise ValueError("invalid standard subscription fee")
        recovered = net * (1 - self.redemption_rate_pct_at_365_days / 100)
        return (gross - recovered) / gross * 100


class Product(StrictModel):
    code: str
    facts_source: str | None = None
    currency: str | None = None
    share_class: str | None = None
    product_type: Literal["ACTIVE_MEDICAL", "A500_FEEDER", "OTHER"] | None = None
    same_class_since: date | None = None
    legally_operating: bool | None = None
    subscription_open: bool | None = None
    redemption_open: bool | None = None
    fund_total_assets_cny: Decimal | None = Field(default=None, ge=0)
    assets_report_date: date | None = None
    stock_min_pct: Decimal | None = None
    stock_max_pct: Decimal | None = None
    medical_non_cash_min_pct: Decimal | None = None
    mainland_only: bool | None = None
    target_etf: str | None = None
    manager_team_since: date | None = None
    manager_source: str | None = None
    fees: Fees | None = None
    delay_source: str | None = None
    subscription_confirmation_max_trading_days: int | None = Field(default=None, ge=0)
    redemption_arrival_max_trading_days: int | None = Field(default=None, ge=0)
    calendar: Calendar
    nav: Series
    dividend_coverage_source: str | None = None
    dividend_coverage_from: date | None = None
    dividend_coverage_to: date | None = None
    dividends: list[Distribution] = Field(default_factory=list)
    benchmark_mapping_source: str | None = None
    benchmark_identity: str | None = None
    benchmark_currency: str | None = None
    benchmark_return_basis: str | None = None
    benchmark_effective_from: date | None = None
    benchmark_effective_to: date | None = None
    benchmark_research_approved: bool = False
    corroboration: Corroboration | None = None

    @model_validator(mode="after")
    def dividends_unique(self) -> Product:
        if len({d.day for d in self.dividends}) != len(self.dividends):
            raise ValueError("dividend duplicates must be resolved from original evidence")
        return self


class CandidateInput(StrictModel):
    context: Context
    cohort: Literal["MEDICAL", "A500"]
    products: list[Product] = Field(min_length=2, max_length=2)
    benchmark: Series | None = None
    benchmark_calendar: Calendar | None = None
    replacement_evidence: ReplacementEvidence | None = None

    @model_validator(mode="after")
    def fixed_pool(self) -> CandidateInput:
        if sorted(p.code for p in self.products) != sorted(COHORTS[self.cohort]):
            raise ValueError("exact approved cohort and share classes required")
        return self


def product_checks(p: Product, data: CandidateInput, start: date) -> tuple[list[str], list[str]]:
    ctx = data.context
    gaps = ctx.source_gaps(p.facts_source)
    excluded = []
    required: dict[str, Any] = dict(
        currency="CNY",
        share_class="C" if data.cohort == "MEDICAL" else "A",
        product_type="ACTIVE_MEDICAL" if data.cohort == "MEDICAL" else "A500_FEEDER",
        legally_operating=True,
        subscription_open=True,
        redemption_open=True,
    )
    if data.cohort == "MEDICAL":
        required["mainland_only"] = True
    else:
        required["target_etf"] = {"022463": "563220", "022424": "563800"}[p.code]
    for key, expected in required.items():
        actual = getattr(p, key)
        if actual is None:
            gaps.append(key + ":MISSING")
        elif actual != expected:
            excluded.append(key + ":OUT_OF_SCOPE")
    if data.cohort == "MEDICAL":
        for field, floor in [("stock_min_pct", 60), ("medical_non_cash_min_pct", 80)]:
            value = getattr(p, field)
            if value is None:
                gaps.append(field + ":MISSING")
            elif value < floor:
                excluded.append(field + ":BELOW_SCOPE_FLOOR")
        if p.stock_max_pct is None:
            gaps.append("STOCK_RANGE_MISSING")
    if p.same_class_since is None:
        gaps.append("SAME_CLASS_START_MISSING")
    elif p.same_class_since > start or (ctx.day - p.same_class_since).days < 365:
        excluded.append("SAME_CLASS_HISTORY_TOO_SHORT")
    if p.fund_total_assets_cny is None or p.assets_report_date is None:
        gaps.append("TOTAL_FUND_ASSETS_MISSING")
    else:
        if not 0 <= (ctx.day - p.assets_report_date).days <= 120:
            gaps.append("ASSETS_REPORT_STALE_OR_FUTURE")
        if p.fund_total_assets_cny < 100_000_000:
            excluded.append("FUND_ASSETS_BELOW_MINIMUM")
    gaps += ctx.source_gaps(p.benchmark_mapping_source)
    if not all(
        [
            p.benchmark_identity,
            p.benchmark_currency,
            p.benchmark_return_basis,
            p.benchmark_effective_from,
        ]
    ):
        gaps.append("BENCHMARK_MAPPING_INCOMPLETE")
    if p.benchmark_currency and p.benchmark_currency != "CNY":
        gaps.append("BENCHMARK_CURRENCY_MISMATCH")
    if p.benchmark_effective_from and p.benchmark_effective_from > start:
        gaps.append("HISTORICAL_MAPPING_NOT_EFFECTIVE")
    if p.benchmark_effective_to and p.benchmark_effective_to < ctx.day:
        gaps.append("BENCHMARK_MAPPING_EXPIRED")
    gaps += ctx.source_gaps(p.delay_source)
    for delay in [
        p.subscription_confirmation_max_trading_days,
        p.redemption_arrival_max_trading_days,
    ]:
        if delay is None:
            gaps.append("DELAY_UNKNOWN")
        elif delay > 5:
            excluded.append("DELAY_ABOVE_FIVE_TRADING_DAYS")
    if p.fees is None:
        gaps.append("FEE_SCENARIO_MISSING")
    else:
        gaps += ctx.source_gaps(p.fees.source)
        if not p.fees.standard_10000_365_day_scenario_supported:
            gaps.append("FEE_SCENARIO_NOT_APPLICABLE")
        if p.fees.effective_from > ctx.day or (
            p.fees.effective_to and p.fees.effective_to < ctx.day
        ):
            gaps.append("FEE_RULE_NOT_EFFECTIVE")
        try:
            if p.fees.loss_pct() > 2:
                excluded.append("EXTERNAL_FEES_ABOVE_TWO_PERCENT")
        except ValueError:
            gaps.append("FEE_FORMULA_INVALID")
    return sorted(set(gaps)), sorted(set(excluded))


def nav_returns(p: Product, ctx: Context, days: list[date]) -> tuple[list[Decimal], list[str]]:
    nav, gaps = p.nav.values(ctx, days, "NAV_CNY_NET_INTERNAL_FEES")
    gaps += ctx.source_gaps(p.dividend_coverage_source)
    if p.nav.identity != p.code:
        gaps.append("EXACT_SHARE_IDENTITY_MISMATCH")
    if (
        not p.dividend_coverage_from
        or not p.dividend_coverage_to
        or (p.dividend_coverage_from > days[0] or p.dividend_coverage_to < days[-1])
    ):
        gaps.append("DIVIDEND_COVERAGE_INCOMPLETE")
    dividends = {}
    for distribution in p.dividends:
        if days[0] < distribution.day <= days[-1]:
            if distribution.day not in days:
                gaps.append("DIVIDEND_OUTSIDE_COMMON_CALENDAR")
            gaps += ctx.source_gaps(distribution.source)
            dividends[distribution.day] = distribution.cash_per_unit
    if any(x <= 0 for x in nav):
        gaps.append("NAV_NONPOSITIVE")
    if gaps:
        return [], sorted(set(gaps))
    return [
        (nav[i] + dividends.get(days[i], D(0))) / nav[i - 1] - 1 for i in range(1, len(days))
    ], []


def path_stats(returns: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    wealth = peak = D(1)
    max_drawdown = D(0)
    for value in returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        max_drawdown = max(max_drawdown, (1 - wealth / peak) * 100)
    return (wealth - 1) * 100, max_drawdown, sample_sd(returns) * D(252).sqrt() * 100


def calculate_candidates(
    data: CandidateInput,
    history: list[dict[str, Any]] | None = None,
    *,
    diagnostic_rules: dict[str, Any] | None = None,
    core_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with localcontext() as context:
        context.prec = 34
        return _calculate(
            data, history or [], diagnostic_rules or rules(data.cohort), core_bindings
        )


def _calculate(
    data: CandidateInput,
    history: list[dict[str, Any]],
    params: dict[str, Any],
    core_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ctx = data.context
    points_required = params["return_window"] + 1
    output = base_output(ctx, data, data.cohort)
    grids = []
    gaps = []
    endpoints = []
    own_gaps: dict[str, list[str]] = {}
    for product in data.products:
        grid, missing = product.calendar.grid(ctx)
        grids.append(set(grid))
        _, days, series_gaps = product.nav.window(
            ctx, product.calendar, points_required, 2, "NAV_CNY_NET_INTERNAL_FEES"
        )
        gaps += [product.code + ":" + g for g in missing]
        own_gaps[product.code] = series_gaps
        if days:
            endpoints.append(days[-1])
    benchmark_returns = []
    if data.cohort == "A500":
        if data.benchmark is None or data.benchmark_calendar is None:
            gaps.append("TOTAL_RETURN_BENCHMARK_MISSING")
        else:
            if data.benchmark.identity != "000510CNY010":
                gaps.append("TOTAL_RETURN_BENCHMARK_IDENTITY_MISMATCH")
            grid, missing = data.benchmark_calendar.grid(ctx)
            grids.append(set(grid))
            _, days, series_gaps = data.benchmark.window(
                ctx, data.benchmark_calendar, points_required, 2, "TOTAL_RETURN_INDEX_POINT"
            )
            gaps += missing + series_gaps
            if days:
                endpoints.append(days[-1])
    common = sorted(set.intersection(*grids)) if grids else []
    end = min(endpoints) if endpoints else None
    days = [d for d in common if end is not None and d <= end][-points_required:]
    if len(days) != points_required:
        gaps.append("COMMON_252_RETURNS_MISSING")
    if days and len([d for d in common if d > days[-1]]) > 2:
        gaps.append("COMMON_WINDOW_STALE")
    if data.cohort == "A500" and data.benchmark and days:
        points, missing = data.benchmark.values(ctx, days, "TOTAL_RETURN_INDEX_POINT")
        gaps += missing
        if any(v <= 0 for v in points):
            gaps.append("BENCHMARK_NONPOSITIVE")
        elif not missing and len(points) == points_required:
            benchmark_returns = [points[i] / points[i - 1] - 1 for i in range(1, len(points))]
    prior = [
        row
        for row in history
        if row.get("definition_id") == VERSION
        and row.get("method") == data.cohort
        and row.get("computation_version") == output["computation_version"]
        and row.get("evidence_class") == ctx.evidence_class
        and row.get("dataset_kind") == ctx.dataset_kind
        and row["as_of"][:10] < ctx.day.isoformat()
    ]
    previous = next(
        (
            row
            for row in reversed(prior)
            if row["as_of"][:10] == (ctx.day - timedelta(days=7)).isoformat()
        ),
        None,
    )
    end_text = days[-1].isoformat() if days else None
    prior_ends = [row["window_end"] for row in prior if row.get("window_end")]
    if end_text and prior_ends and end_text <= max(prior_ends):
        gaps.append("NO_NEW_WINDOW")
    rows: list[dict[str, Any]] = []
    for p in sorted(data.products, key=lambda p: p.code):
        missing, exclusions = product_checks(p, data, days[0] if days else ctx.day)
        missing += gaps + own_gaps[p.code]
        returns, path_gaps = nav_returns(p, ctx, days) if days else ([], ["NO_WINDOW"])
        missing += path_gaps
        parts: dict[str, Decimal] = {}
        raw: dict[str, str] = {}
        if len(returns) == params["return_window"]:
            total, drawdown, volatility = path_stats(returns)
            raw.update(
                return_pct=str(total),
                max_drawdown_pct=str(drawdown),
                volatility_pct=str(volatility),
            )
            if data.cohort == "MEDICAL":
                parts["return"] = up(total, str(params["return_low"]), str(params["return_high"]))
                parts["risk"] = params["risk_weights"][0] * down(
                    drawdown, str(params["drawdown_low"]), str(params["drawdown_high"])
                ) + params["risk_weights"][1] * down(
                    volatility, str(params["volatility_low"]), str(params["volatility_high"])
                )
                missing += ctx.source_gaps(p.manager_source)
                if p.manager_team_since is None or p.manager_team_since > ctx.day:
                    missing.append("MANAGER_CONTINUITY_UNKNOWN")
                else:
                    parts["management"] = up(
                        D((ctx.day - p.manager_team_since).days) / 365 * 12,
                        "0",
                        str(params["management_scale"]),
                    )
            else:
                parts["risk"] = down(
                    drawdown, str(params["drawdown_low"]), str(params["drawdown_high"])
                )
                if len(benchmark_returns) == params["return_window"]:
                    benchmark_total = path_stats(benchmark_returns)[0]
                    error = sample_sd(
                        [a - b for a, b in zip(returns, benchmark_returns, strict=True)]
                    )
                    error *= D(252).sqrt() * 100
                    difference = abs(total - benchmark_total)
                    parts["tracking"] = params["tracking_weights"][0] * down(
                        error, "0", str(params["tracking_scale"])
                    ) + params["tracking_weights"][1] * down(
                        difference, "0", str(params["tracking_scale"])
                    )
                    raw.update(
                        tracking_error_pct=str(error),
                        absolute_tracking_difference_pp=str(difference),
                    )
                a, b = (
                    p.subscription_confirmation_max_trading_days,
                    p.redemption_arrival_max_trading_days,
                )
                if a is not None and b is not None:
                    parts["confirmation"] = down(
                        D(a + b), str(params["confirmation_low"]), str(params["confirmation_high"])
                    )
        if p.fees:
            try:
                fee = p.fees.loss_pct()
                parts["cost"] = down(fee, "0", str(params["fee_scale"]))
                raw["standard_external_cost_pct"] = str(fee)
            except ValueError:
                missing.append("FEE_FORMULA_INVALID")
        names = (
            ["return", "risk", "cost", "management"]
            if data.cohort == "MEDICAL"
            else ["tracking", "risk", "cost", "confirmation"]
        )
        weights = dict(zip(names, params["score_weights"], strict=True))
        if set(parts) != set(weights):
            missing.append("REQUIRED_SCORE_DIMENSION_MISSING")
        total_score = None if missing or exclusions else sum(parts[k] * weights[k] for k in weights)
        warnings = corroboration_gaps(ctx, p.model_dump(mode="json"), p.corroboration)
        platform = (
            next((v for v in data.replacement_evidence.platforms if v.code == p.code), None)
            if data.replacement_evidence
            else None
        )
        warnings += (
            platform_gaps(ctx, platform, data.replacement_evidence.account_ref, p.share_class or "")
            if platform and data.replacement_evidence
            else ["EXECUTION_UNKNOWN"]
        )
        mapping_approved = (
            not core_bindings["mappings"][p.code]["blockers"]
            if core_bindings is not None
            else p.benchmark_research_approved
        )
        if not mapping_approved:
            warnings.append("MAPPING_DRAFT")
        rows.append(
            dict(
                code=p.code,
                filter_result="UNKNOWN"
                if missing
                else "EXCLUDED"
                if exclusions
                else "PASS_RESEARCH_ONLY",
                gaps=sorted(set(missing)),
                exclusions=exclusions,
                metrics=raw,
                dimension_scores={k: str(v) for k, v in parts.items()},
                weights={k: str(v) for k, v in weights.items()},
                total_score=str(total_score) if total_score is not None else None,
                rank=None,
                stock_range=[str(p.stock_min_pct), str(p.stock_max_pct)],
                warnings=sorted(set(warnings)),
            )
        )
    comparable = all(row["total_score"] is not None for row in rows)
    leader = None
    spread = None
    tied = False
    if comparable:
        delta = D(rows[0]["total_score"]) - D(rows[1]["total_score"])
        spread, tied = abs(delta), abs(delta) <= params["tie_threshold"]
        if not tied:
            leader = rows[0 if delta > 0 else 1]["code"]
            for row in rows:
                row["rank"] = 1 if row["code"] == leader else 2
    leading_weeks = 0
    if (
        leader
        and spread is not None
        and spread >= params["lead_threshold"]
        and not any(r["warnings"] for r in rows)
    ):
        leading_weeks = 1 + (
            previous.get("leading_weeks", 0) if previous and previous.get("leader") == leader else 0
        )
    output.update(
        status="CALCULATED_SHADOW" if comparable else "INSUFFICIENT_DATA",
        rows=rows,
        leader=leader,
        tied=tied,
        spread=str(spread) if spread is not None else None,
        leading_weeks=leading_weeks,
        window_end=end_text,
        window_hash=digest([d.isoformat() for d in days]),
        gaps=sorted(set(gaps)),
        leading_score_history=(
            (previous.get("leading_score_history", []) if previous and leading_weeks > 1 else [])
            + (
                [dict(as_of=ctx.as_of.isoformat(), spread=str(spread), leader=leader)]
                if leading_weeks
                else []
            )
        ),
        display_text="既有两产品研究比较;未验证影子试算,不是可投资资格或交易安排。",
    )
    output.update(
        replacement_result(
            ctx, rows, leader, leading_weeks, data.replacement_evidence, params["lead_weeks"]
        )
    )
    return output
