"""R1.2 medical-only isolated calculator; frozen R1.1 engine bytes stay untouched.

Reuse R1.1 inputs, NAV/fee formulas, rules and replacement checks. The versioned
filter/calculation orchestration is forked to preserve old governance fingerprints.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from investor_core.r11_candidates import CandidateInput, Product, nav_returns, path_stats
from investor_core.r11_inputs import D, base_output, down, up
from investor_core.r11_replacement import corroboration_gaps, platform_gaps, replacement_result
from investor_core.scheduler import digest


def product_checks(
    p: Product, data: CandidateInput, start: date, *, arrival_required: bool = True
) -> tuple[list[str], list[str]]:
    ctx = data.context
    gaps = ctx.source_gaps(p.facts_source)
    excluded = []
    required: dict[str, Any] = dict(
        currency="CNY",
        share_class="C",
        product_type="ACTIVE_MEDICAL",
        legally_operating=True,
        subscription_open=True,
        redemption_open=True,
    )
    required["mainland_only"] = True
    for key, expected in required.items():
        actual = getattr(p, key)
        if actual is None:
            gaps.append(key + ":MISSING")
        elif actual != expected:
            excluded.append(key + ":OUT_OF_SCOPE")
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
        if p.fund_total_assets_cny < 100000000:
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
    delays = [p.subscription_confirmation_max_trading_days]
    if arrival_required:
        delays.append(p.redemption_arrival_max_trading_days)
    for delay in delays:
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
    return (sorted(set(gaps)), sorted(set(excluded)))


def _calculate(
    data: CandidateInput,
    history: list[dict[str, Any]],
    params: dict[str, Any],
    core_bindings: dict[str, Any] | None = None,
    *,
    medical_r12: bool = False,
) -> dict[str, Any]:
    if not medical_r12 or data.cohort != "MEDICAL":
        raise ValueError("R1.2 is restricted to the medical cohort")
    ctx = data.context
    points_required = params["return_window"] + 1
    known_sources = {
        k: s
        for k, s in ctx.sources.items()
        if s.published_at is not None and s.first_retrieved_at is not None
    }
    # Presentation-only provenance list; calculation still uses the unchanged full context.
    known_context = ctx.model_copy(update={"sources": known_sources})
    output = base_output(known_context, data, data.cohort)
    output["unknown_knowledge_times"] = sorted(set(ctx.sources) - set(known_sources))
    if medical_r12:
        from investor_core.r12_medical import COMPUTATION_VERSION as R12_COMPUTATION
        from investor_core.r12_medical import DEFINITION as R12_DEFINITION

        output.update(
            definition_id=R12_DEFINITION["definition_id"],
            definition_source=R12_DEFINITION,
            computation_version=R12_COMPUTATION,
        )
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
        own_gaps[product.code] = sorted(set(missing + series_gaps))
        if days:
            endpoints.append(days[-1])
    common = sorted(set.intersection(*grids)) if grids else []
    end = min(endpoints) if endpoints else None
    days = [d for d in common if end is not None and d <= end][-points_required:]
    if len(days) != points_required:
        gaps.append("COMMON_252_RETURNS_MISSING")
    if days and len([d for d in common if d > days[-1]]) > 2:
        gaps.append("COMMON_WINDOW_STALE")
    prior = [
        row
        for row in history
        if row.get("definition_id") == output["definition_id"]
        and row.get("method") == data.cohort
        and (row.get("computation_version") == output["computation_version"])
        and (row.get("evidence_class") == ctx.evidence_class)
        and (row.get("dataset_kind") == ctx.dataset_kind)
        and (row["as_of"][:10] < ctx.day.isoformat())
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
    if end_text and prior_ends and (end_text <= max(prior_ends)):
        gaps.append("NO_NEW_WINDOW")
    rows: list[dict[str, Any]] = []
    for p in sorted(data.products, key=lambda p: p.code):
        missing, exclusions = product_checks(
            p, data, days[0] if days else ctx.day, arrival_required=not medical_r12
        )
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
        if p.fees:
            try:
                fee = p.fees.loss_pct()
                parts["cost"] = down(fee, "0", str(params["fee_scale"]))
                raw["standard_external_cost_pct"] = str(fee)
            except ValueError:
                missing.append("FEE_FORMULA_INVALID")
        names = ["return", "risk", "cost", "management"]
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
        if medical_r12:
            from investor_core.r12_medical import arrival_status

            arrival = arrival_status(p, data)
            if arrival["state"] != "SATISFIED":
                warnings.append("REDEMPTION_ARRIVAL_" + arrival["state"])
        rows.append(
            dict(
                code=p.code,
                **{"redemption_arrival": arrival} if medical_r12 else {},
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
        spread, tied = (abs(delta), abs(delta) <= params["tie_threshold"])
        if not tied:
            leader = rows[0 if delta > 0 else 1]["code"]
            for row in rows:
                row["rank"] = 1 if row["code"] == leader else 2
    leading_weeks = 0
    if (
        leader
        and spread is not None
        and (spread >= params["lead_threshold"])
        and (not any(r["warnings"] for r in rows))
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
            previous.get("leading_score_history", []) if previous and leading_weeks > 1 else []
        )
        + (
            [dict(as_of=ctx.as_of.isoformat(), spread=str(spread), leader=leader)]
            if leading_weeks
            else []
        ),
        display_text="既有两产品研究比较;未验证影子试算,不是可投资资格或交易安排。",
    )
    output.update(
        replacement_result(
            ctx, rows, leader, leading_weeks, data.replacement_evidence, params["lead_weeks"]
        )
    )
    return output
