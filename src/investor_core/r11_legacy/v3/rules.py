"""Frozen R1.1 rule values and exhaustive one-factor diagnostic variants.

Only the internal validation runner uses variants. No runtime tuning endpoint.
Safety/knowledge/scope, quality coverage and execution hard gates are excluded.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from investor_core.scheduler import digest

D = Decimal
C: dict[str, Any] = dict(
    pb_window=1260,
    breadth_window=60,
    price_window=63,
    monthly_window=4,
    entry_weeks=3,
    hold_weeks=4,
    pmi_neutral=D(50),
    pmi_level_scale=D(2),
    pmi_change_scale=D(2),
    liquidity_scale=D(2),
    breadth_neutral=D(".5"),
    breadth_scale=D(".2"),
    price_scale=D(10),
    entry_threshold=D(".2"),
    exit_threshold=D(".05"),
    confidence_threshold=D(".35"),
    pmi_weights=[D(".5"), D(".5")],
    x_weights=[D(".70"), D(".30")],
    y_weights=[D(".40"), D(".30"), D(".30")],
)
COMMON_D: dict[str, Any] = dict(
    return_window=252,
    lead_weeks=4,
    tie_threshold=D(2),
    lead_threshold=D(10),
    drawdown_low=D(5),
    drawdown_high=D(40),
    fee_scale=D(2),
)
MEDICAL: dict[str, Any] = dict(
    **COMMON_D,
    return_low=D(-20),
    return_high=D(20),
    volatility_low=D(10),
    volatility_high=D(50),
    management_scale=D(36),
    risk_weights=[D(".5"), D(".5")],
    score_weights=[D(".25"), D(".35"), D(".20"), D(".20")],
)
A500: dict[str, Any] = dict(
    **COMMON_D,
    tracking_scale=D(5),
    confirmation_low=D(2),
    confirmation_high=D(10),
    tracking_weights=[D(".5"), D(".5")],
    score_weights=[D(".50"), D(".20"), D(".20"), D(".10")],
)


def rules(method: str) -> dict[str, Any]:
    source = {"C": C, "MEDICAL": MEDICAL, "A500": A500}[method]
    return {k: list(v) if isinstance(v, list) else v for k, v in source.items()}


def scenarios(method: str) -> dict[str, dict[str, Any]]:
    with localcontext() as context:
        context.prec = 34
        return _scenarios(method)


def _scenarios(method: str) -> dict[str, dict[str, Any]]:
    baseline = rules(method)
    result = {}
    for name, value in baseline.items():
        for direction in (-1, 1):
            sign = "minus" if direction == -1 else "plus"
            if isinstance(value, list):
                for index in range(len(value)):
                    changed = rules(method)
                    weights = list(value)
                    weights[index] *= D(1) + D(direction) / 10
                    total = sum(weights, D(0))
                    changed[name] = [w / total for w in weights]
                    result[f"{name}.{index}.{sign}10pct"] = changed
            else:
                changed = rules(method)
                changed[name] = (
                    max(1, value + direction)
                    if isinstance(value, int)
                    else (value * (D(1) + D(direction) / 10))
                )
                suffix = "1period" if isinstance(value, int) else "10pct"
                result[f"{name}.{sign}{suffix}"] = changed
    return result


def stress_spec(method: str) -> dict[str, list[str]]:
    common = {
        "SOURCE_OUTAGE": ["no_financial_action", "must_be_incomplete"],
        "SOURCE_REVISION": [
            "no_financial_action",
            "input_changed",
            "baseline_preserved",
            "repeat_deterministic",
            "single_cutoff_count",
        ],
        "IDENTITY_CHANGE": ["no_financial_action", "must_be_incomplete"],
    }
    if method != "C":
        common.update(
            DOUBLE_FEE_RATES=["no_financial_action", "exact_fee_rate_doubling"],
            DOUBLE_DELAY=["no_financial_action", "exact_delay_doubling"],
        )
    return common


def registry(method: str) -> dict[str, Any]:
    # Decimal values are serialized as text; no binary float perturbation drift.
    def serial(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, list):
            return [serial(v) for v in value]
        if isinstance(value, dict):
            return {k: serial(v) for k, v in value.items()}
        return value

    body = dict(method=method, baseline=serial(rules(method)), scenarios=serial(scenarios(method)))
    body["stress_scenarios"] = stress_spec(method)
    return dict(body, registry_hash=digest(body))
