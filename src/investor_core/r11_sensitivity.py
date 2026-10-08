"""Per-scenario fixed-denominator diagnostics; never selects new parameters."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from investor_core.r11_candidates import CandidateInput, calculate_candidates
from investor_core.r11_macro import MacroInput, calculate_macro
from investor_core.r11_rules import registry, scenarios, stress_spec
from investor_core.r11_validation import ValidationWindow, _complete, _selection, weeks
from investor_core.scheduler import digest


def outcome(output: dict[str, Any], method: str) -> str | None:
    if output.get("status") != "CALCULATED_SHADOW" or output.get("gaps"):
        return None
    if method == "C":
        value = output.get("dominant_season")
        return str(value) if value not in {None, "UNKNOWN", "TRANSITION"} else None
    value = output.get("leader")
    return str(value) if value and not output.get("tied") else None


def baseline_sets(window: ValidationWindow, runs: list[dict[str, Any]]) -> dict[str, list[str]]:
    result = {}
    for kind, start, end in [
        ("H", window.history_start, window.history_end),
        ("F", window.forward_start, window.forward_end),
    ]:
        selected = _selection(runs, window.method, kind, window.dataset_kind)
        result[kind] = [
            selected[d]["id"]
            for d in weeks(start, end)
            if _complete(selected.get(d)) and outcome(selected[d]["output"], window.method)
        ]
    return result


def sensitivity(
    window: ValidationWindow, runs: list[dict[str, Any]], frozen_sets: dict[str, list[str]]
) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 34
        return _sensitivity(window, runs, frozen_sets)


def _sensitivity(
    window: ValidationWindow, runs: list[dict[str, Any]], frozen_sets: dict[str, list[str]]
) -> dict[str, Any]:
    if frozen_sets != baseline_sets(window, runs):
        raise ValueError("Baseline set drift; freeze the complete eligible set before diagnostics")
    by_id = {row["id"]: row for row in runs}
    summaries: list[dict[str, Any]] = []
    for name, values in scenarios(window.method).items():
        strata: dict[str, Any] = {}
        for kind in ("H", "F"):
            selected = _selection(runs, window.method, kind, window.dataset_kind)
            history: list[dict[str, Any]] = []
            outputs = {}
            end = window.history_end if kind == "H" else window.forward_end
            for day, record in sorted(selected.items()):
                if day > end:
                    continue
                data = record["input"]
                computed = (
                    calculate_macro(
                        MacroInput.model_validate(data), history, diagnostic_rules=values
                    )
                    if window.method == "C"
                    else calculate_candidates(
                        CandidateInput.model_validate(data), history, diagnostic_rules=values
                    )
                )
                history.append(computed)
                outputs[record["id"]] = computed
            fixed = frozen_sets[kind]
            minimum = 26 if kind == "H" else 8
            matched = []
            failures = []
            incomplete = False
            for run_id in fixed:
                output = outputs.get(run_id, {})
                baseline = by_id[run_id]["output"]
                if outcome(output, window.method) == outcome(baseline, window.method):
                    matched.append(run_id)
                else:
                    failures.append(
                        dict(
                            run_id=run_id,
                            as_of=baseline["as_of"],
                            reason=output.get("gaps") or ["STATE_CHANGED_OR_TIED_OR_TRANSITION"],
                        )
                    )
                incomplete |= output.get("status") != "CALCULATED_SHADOW" or bool(
                    output.get("gaps")
                )
            ratio = Decimal(len(matched)) / len(fixed) if fixed else None
            strata[kind] = dict(
                numerator=len(matched),
                denominator=len(fixed),
                ratio=str(ratio) if ratio is not None else None,
                dates=[by_id[key]["output"]["as_of"] for key in fixed],
                failures=failures,
                complete=not incomplete,
                result="INSUFFICIENT_SAMPLE"
                if len(fixed) < minimum
                else "INCOMPLETE"
                if incomplete
                else "PASS"
                if ratio is not None and ratio >= Decimal(".8")
                else "FAIL",
            )
        summaries.append(dict(scenario=name, strata=strata))
    ratios = [
        (Decimal(v["ratio"]), s["scenario"], kind)
        for s in summaries
        for kind, v in s["strata"].items()
        if v["ratio"] is not None
    ]
    worst = min(ratios) if ratios else None
    return dict(
        registry=registry(window.method),
        frozen_baseline_sets=frozen_sets,
        baseline_set_hash=digest(frozen_sets),
        scenarios=summaries,
        worst=dict(ratio=str(worst[0]), scenario=worst[1], evidence_class=worst[2])
        if worst
        else None,
        result="PASS"
        if summaries and all(v["result"] == "PASS" for s in summaries for v in s["strata"].values())
        else "NOT_PASSED",
        selects_new_parameters=False,
    )


def assess_stress_rows(
    method: str, expected_ids: list[str], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    spec = stress_spec(method)
    required = {(run_id, scenario) for run_id in expected_ids for scenario in spec}
    actual = {(row["run_id"], row["scenario"]) for row in rows}
    missing = sorted(required - actual)
    covered = required == actual and len(rows) == len(required) and bool(required)
    covered = covered and all(set(row["checks"]) == set(spec[row["scenario"]]) for row in rows)
    passed = covered and all(all(row["checks"].values()) for row in rows)
    return dict(
        rows=rows,
        required_scenarios=spec,
        expected_observation_ids=expected_ids,
        missing=[dict(run_id=key, scenario=name) for key, name in missing],
        result="PASS" if passed else "INCOMPLETE" if not covered else "FAIL",
        baseline="NO_MODEL_NO_REPLACEMENT_NO_FINANCIAL_ACTION",
        realized_return_difference=None,
        limitation="No real trades or predictive/cost-benefit efficacy inferred",
    )


def stress_diagnostics(window: ValidationWindow, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Every registered scenario must actually execute for every complete F record."""
    selected = _selection(runs, window.method, "F", window.dataset_kind)
    rows = []
    expected_ids = []
    for day, record in sorted(selected.items()):
        if not window.forward_start <= day <= window.forward_end or not _complete(record):
            continue
        expected_ids.append(record["id"])
        original_hash = digest(record["input"])
        history = record.get("previous_outputs", [])
        for scenario in stress_spec(window.method):
            checks = {}
            data: MacroInput | CandidateInput
            if window.method == "C":
                data = MacroInput.model_validate(record["input"])
                if scenario == "IDENTITY_CHANGE":
                    data.index_identity = "STRESS_INVALID_INDEX_IDENTITY"
                elif scenario == "SOURCE_REVISION":
                    point = data.pb.points[-1]
                    assert point.value is not None
                    point.value *= Decimal("1.01")
            else:
                data = CandidateInput.model_validate(record["input"])
                if scenario == "IDENTITY_CHANGE":
                    data.products[0].nav.identity = "STRESS_WRONG_SHARE_CLASS"
                elif scenario == "SOURCE_REVISION":
                    point = data.products[0].nav.points[-1]
                    assert point.value is not None
                    point.value *= Decimal("1.01")
                elif scenario == "DOUBLE_FEE_RATES":
                    for product in data.products:
                        assert product.fees is not None
                        product.fees.subscription_value *= 2
                        product.fees.redemption_rate_pct_at_365_days *= 2
                    checks["exact_fee_rate_doubling"] = all(
                        p.fees is not None
                        and p.fees.subscription_value
                        == Decimal(old["fees"]["subscription_value"]) * 2
                        and p.fees.redemption_rate_pct_at_365_days
                        == Decimal(old["fees"]["redemption_rate_pct_at_365_days"]) * 2
                        for p, old in zip(data.products, record["input"]["products"], strict=True)
                    )
                elif scenario == "DOUBLE_DELAY":
                    for product in data.products:
                        assert product.subscription_confirmation_max_trading_days is not None
                        assert product.redemption_arrival_max_trading_days is not None
                        product.subscription_confirmation_max_trading_days *= 2
                        product.redemption_arrival_max_trading_days *= 2
                    checks["exact_delay_doubling"] = all(
                        p.subscription_confirmation_max_trading_days
                        == old["subscription_confirmation_max_trading_days"] * 2
                        and p.redemption_arrival_max_trading_days
                        == old["redemption_arrival_max_trading_days"] * 2
                        for p, old in zip(data.products, record["input"]["products"], strict=True)
                    )
            if scenario == "SOURCE_OUTAGE":
                for source in data.context.sources.values():
                    source.quality = "UNVERIFIED"
            output = (
                calculate_macro(data, history)
                if isinstance(data, MacroInput)
                else calculate_candidates(data, history)
            )
            checks["no_financial_action"] = not output["money_action"]
            if scenario in {"SOURCE_OUTAGE", "IDENTITY_CHANGE"}:
                checks["must_be_incomplete"] = output["status"] != "CALCULATED_SHADOW"
            if scenario == "SOURCE_REVISION":
                repeated = (
                    calculate_macro(data, history)
                    if isinstance(data, MacroInput)
                    else calculate_candidates(data, history)
                )
                checks.update(
                    input_changed=digest(data.model_dump(mode="json")) != original_hash,
                    baseline_preserved=digest(record["input"]) == original_hash,
                    repeat_deterministic=repeated == output,
                )
                field = "pending_weeks" if window.method == "C" else "leading_weeks"
                prior = [h for h in history if h["as_of"] < output["as_of"]]
                bound = (prior[-1].get(field, 0) if prior else 0) + 1
                checks["single_cutoff_count"] = output.get(field, 0) <= bound
            rows.append(
                dict(
                    run_id=record["id"],
                    as_of=str(day),
                    scenario=scenario,
                    status=output["status"],
                    leader=output.get("leader"),
                    replacement=output.get("replacement"),
                    gaps=output["gaps"],
                    output_hash=digest(output),
                    checks=checks,
                )
            )
    return assess_stress_rows(window.method, expected_ids, rows)
