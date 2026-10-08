"""Approved R1.1 C rules. Rule clarity is not predictive confidence."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Any

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.r11_inputs import (
    VERSION,
    Calendar,
    Context,
    D,
    Series,
    base_output,
    clip,
    monthly,
)
from investor_core.r11_rules import rules


class Member(StrictModel):
    code: str
    listed_on: date
    listing_source: str
    corporate_action_source: str
    wealth: Series


class MacroInput(StrictModel):
    context: Context
    scope: str = "REGION:CN_A_BROAD"
    index_identity: str = Field(min_length=1)
    identity_source: str
    calendar: Calendar
    pb: Series
    pmi: Series
    social_financing_yoy: Series
    price: Series
    constituents_date: date
    constituents_source: str
    members: list[Member]

    @model_validator(mode="after")
    def identity(self) -> MacroInput:
        if self.scope != "REGION:CN_A_BROAD":
            raise ValueError("approved scope only")
        if len({m.code for m in self.members}) != len(self.members):
            raise ValueError("duplicate constituent")
        return self


def stable_state(
    output: dict[str, Any], history: list[dict[str, Any]], params: dict[str, Any]
) -> dict[str, Any]:
    # Callers of the persistence adapter cannot submit their own history.
    current_day = date.fromisoformat(output["as_of"][:10])
    by_day = {
        row["as_of"][:10]: row
        for row in history
        if row.get("definition_id") == VERSION
        and row.get("method") == "C"
        and row.get("computation_version") == output["computation_version"]
        and row.get("evidence_class") == output["evidence_class"]
        and row.get("dataset_kind") == output["dataset_kind"]
        and row["as_of"][:10] < output["as_of"][:10]
    }
    previous = by_day.get((current_day - timedelta(days=7)).isoformat())
    latest = by_day[max(by_day)] if by_day else None
    last = latest.get("last_stable") if latest else None
    established = latest.get("stable_since") if latest else None
    if output["gaps"]:
        return dict(
            dominant_season="UNKNOWN",
            last_stable=last,
            stable_since=established,
            pending_season=None,
            pending_weeks=0,
            confidence="LOW",
            ambiguity="HIGH",
        )
    candidate = output["candidate_season"]
    weeks = 0
    if candidate != "TRANSITION":
        weeks = 1
        if previous and previous.get("pending_season") == candidate:
            weeks += previous.get("pending_weeks", 0)
    held = (
        established is None
        or (current_day - date.fromisoformat(established)).days >= 7 * params["hold_weeks"]
    )
    if weeks >= params["entry_weeks"] and held and candidate != last:
        last, established = candidate, current_day.isoformat()
    x, y = D(output["axes"]["X"]), D(output["axes"]["Y"])
    signs = {"SPRING": (-1, 1), "SUMMER": (1, 1), "AUTUMN": (1, -1), "WINTER": (-1, -1)}
    clear = (
        abs(x) >= params["confidence_threshold"]
        and abs(y) >= params["confidence_threshold"]
        and candidate == last
    )
    if last is None:
        dominant = "TRANSITION"
    else:
        sx, sy = signs[last]
        dominant = "TRANSITION" if min(x * sx, y * sy) < params["exit_threshold"] else last
    return dict(
        dominant_season=dominant,
        last_stable=last,
        stable_since=established,
        pending_season=None if candidate == "TRANSITION" else candidate,
        pending_weeks=weeks,
        confidence="MEDIUM" if clear else "LOW",
        ambiguity="LOW" if clear else "HIGH",
        uncertain=not clear,
    )


def calculate_macro(
    data: MacroInput,
    history: list[dict[str, Any]] | None = None,
    *,
    diagnostic_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with localcontext() as context:
        context.prec = 34
        return _calculate(data, history or [], diagnostic_rules or rules("C"))


def _calculate(
    data: MacroInput, history: list[dict[str, Any]], params: dict[str, Any]
) -> dict[str, Any]:
    ctx = data.context
    output = base_output(ctx, data, "C")
    gaps = ctx.source_gaps(data.identity_source)
    if data.pb.identity != data.index_identity or data.price.identity != data.index_identity:
        gaps.append("INDEX_IDENTITY_MISMATCH")
    scores: dict[str, Decimal | None] = dict.fromkeys(["V", "F", "L", "B", "P"])
    dimension_gaps: dict[str, list[str]] = {}
    pb, pb_days, missing = data.pb.window(
        ctx, data.calendar, params["pb_window"] + 1, 1, "MULTIPLE"
    )
    if any(v <= 0 for v in pb):
        missing.append("PB_NONPOSITIVE")
    if not missing:
        p = (
            sum(x < pb[-1] for x in pb[:-1]) + D(".5") * sum(x == pb[-1] for x in pb[:-1])
        ) / params["pb_window"]
        scores["V"] = 1 - 2 * p
    dimension_gaps["V"] = missing
    pmi, missing = monthly(data.pmi, ctx, "PMI_POINTS", params["monthly_window"])
    if data.pmi.identity != "NBS_MANUFACTURING_PMI":
        missing.append("PMI_IDENTITY_MISMATCH")
    if not missing:
        scores["F"] = params["pmi_weights"][0] * clip(
            (pmi[-1] - params["pmi_neutral"]) / params["pmi_level_scale"]
        ) + params["pmi_weights"][1] * clip((pmi[-1] - pmi[0]) / params["pmi_change_scale"])
    dimension_gaps["F"] = missing
    financing, missing = monthly(
        data.social_financing_yoy, ctx, "PERCENT", params["monthly_window"]
    )
    if data.social_financing_yoy.identity != "PBC_TSF_STOCK_YOY_SAME_BASIS":
        missing.append("FINANCING_IDENTITY_OR_BASIS_MISMATCH")
    if not missing:
        scores["L"] = clip((financing[-1] - financing[0]) / params["liquidity_scale"])
    dimension_gaps["L"] = missing
    grid, missing = data.calendar.grid(ctx)
    missing += ctx.source_gaps(data.constituents_source)
    if not grid or data.constituents_date != grid[-1]:
        missing.append("CURRENT_CONSTITUENTS_REQUIRED")
    if len(grid) < params["breadth_window"] or not data.members:
        missing.append("BREADTH_HISTORY_MISSING")
    excluded = []
    above = eligible = 0
    if len(grid) >= params["breadth_window"]:
        for member in data.members:
            missing += ctx.source_gaps(member.listing_source)
            if member.listed_on > grid[-1]:
                missing.append("FUTURE_LISTING:" + member.code)
            elif member.listed_on > grid[-params["breadth_window"]]:
                excluded.append(member.code)
                continue
            missing += ctx.source_gaps(member.corporate_action_source)
            values, member_gaps = member.wealth.values(
                ctx, grid[-params["breadth_window"] :], "TOTAL_RETURN_POINT"
            )
            if member.wealth.identity != member.code or any(v <= 0 for v in values):
                member_gaps.append("CONSTITUENT_IDENTITY_OR_PATH_INVALID")
            missing += [member.code + ":" + x for x in member_gaps]
            if not member_gaps:
                eligible += 1
                above += values[-1] > sum(values) / params["breadth_window"]
    if eligible < D(".9") * len(data.members):
        missing.append("BREADTH_COVERAGE_BELOW_90_PERCENT")
    if not missing and eligible:
        scores["B"] = clip(
            (D(above) / eligible - params["breadth_neutral"]) / params["breadth_scale"]
        )
    dimension_gaps["B"] = sorted(set(missing))
    prices, price_days, missing = data.price.window(
        ctx, data.calendar, params["price_window"] + 1, 1, "PRICE_INDEX_POINT"
    )
    if any(v <= 0 for v in prices):
        missing.append("PRICE_NONPOSITIVE")
    if not missing:
        scores["P"] = clip((prices[-1] / prices[0] - 1) * 100 / params["price_scale"])
    dimension_gaps["P"] = missing
    gaps += [key + ":" + gap for key, reasons in dimension_gaps.items() for gap in reasons]
    axes: dict[str, str] = {}
    candidate = "UNKNOWN"
    if not gaps:
        v, f, liquidity, breadth, price = [scores[k] for k in ["V", "F", "L", "B", "P"]]
        assert all(x is not None for x in (v, f, liquidity, breadth, price))
        x = params["x_weights"][0] * price - params["x_weights"][1] * v
        y = (
            params["y_weights"][0] * f
            + params["y_weights"][1] * liquidity
            + params["y_weights"][2] * breadth
        )
        axes = {
            "X": str(x),
            "Y": str(y),
            "position_score": str(50 * (x + 1)),
            "improvement_score": str(50 * (y + 1)),
        }
        candidate = "TRANSITION"
        if abs(x) >= params["entry_threshold"] and abs(y) >= params["entry_threshold"]:
            candidate = (
                ("SUMMER" if y > 0 else "AUTUMN") if x > 0 else ("SPRING" if y > 0 else "WINTER")
            )
    output.update(
        scope=data.scope,
        dimension_scores={k: str(v) if v is not None else None for k, v in scores.items()},
        dimension_gaps=dimension_gaps,
        gaps=sorted(set(gaps)),
        axes=axes,
        candidate_season=candidate,
        breadth=dict(
            total=len(data.members), eligible=eligible, above=above, new_listing_exclusions=excluded
        ),
        valuation_end=pb_days[-1].isoformat() if pb_days else None,
        price_end=price_days[-1].isoformat() if price_days else None,
        status="INSUFFICIENT_DATA" if gaps else "CALCULATED_SHADOW",
        display_text="A股宽基规则影子研究;非预测概率,不改变金额或投资资格。",
    )
    output.update(stable_state(output, history, params))
    return output
