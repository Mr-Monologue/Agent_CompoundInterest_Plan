"""Incremental independent-review metamorphic regressions; synthetic only."""

from copy import deepcopy
from datetime import date, timedelta

from test_r11_calculators import macro
from test_r11_validation import setup

from investor_core.r11_macro import MacroInput, calculate_macro
from investor_core.r11_quality import apply_extraction
from investor_core.r11_rules import rules
from investor_core.r11_sensitivity import baseline_sets, sensitivity
from investor_core.r11_validation import ValidationWindow, evidence_summary


def test_extraction_failed_warmup_resets_default_and_perturbed_sensitivity(monkeypatch):
    history = []
    runs = []
    for i in range(6):
        day = date(2026, 10, 10) + timedelta(weeks=i)
        data = macro(day)
        if i >= 3:
            for point, value in zip(data["pmi"]["points"], [54, 52, 49, 48], strict=True):
                point["value"] = str(value)
            for point, value in zip(
                data["social_financing_yoy"]["points"], [10, 9, 9, 8], strict=True
            ):
                point["value"] = str(value)
            for j, point in enumerate(data["members"][0]["wealth"]["points"]):
                point["value"] = str(60 - j)
        typed = MacroInput.model_validate(data)
        output = calculate_macro(typed, history)
        output = apply_extraction(output, {"status": "FAILED" if i == 2 else "MATCHED"}, history)
        runs.append(dict(id=str(i), input=typed.model_dump(mode="json"), output=output))
        history.append(output)
    assert runs[2]["output"]["last_stable"] is None
    assert runs[-1]["output"]["dominant_season"] == "WINTER"
    periods = ValidationWindow(
        method="C",
        dataset_kind="SYNTHETIC",
        history_start="2026-09-26",
        history_end="2026-10-03",
        forward_start="2026-10-10",
        forward_end="2026-11-14",
    )
    default = rules("C")
    altered = deepcopy(default)
    altered["x_weights"] = tuple(reversed(default["x_weights"]))
    monkeypatch.setattr(
        "investor_core.r11_sensitivity.scenarios",
        lambda _: {"unperturbed_control": default, "weight_shift": altered},
    )
    result = sensitivity(periods, runs, baseline_sets(periods, runs))
    for scenario in result["scenarios"]:
        assert scenario["strata"]["F"]["denominator"] == 1
        assert scenario["strata"]["F"]["numerator"] == 1, scenario


def test_unused_future_month_cannot_hide_three_known_monthly_releases():
    periods, records, replays = setup("C")
    baseline = evidence_summary(periods, records, replays)
    assert baseline["computed_checks"]["pmi_three_new_months"]
    assert baseline["computed_checks"]["social_financing_yoy_three_new_months"]
    changed = deepcopy(records)
    for record in changed:
        if record["output"]["evidence_class"] != "F":
            continue
        sources = record["input"]["context"]["sources"]
        sources["future"] = dict(
            sources["official"],
            published_at="2030-01-01T09:00:00+08:00",
            first_retrieved_at="2030-01-01T10:00:00+08:00",
        )
        for field in ["pmi", "social_financing_yoy"]:
            record["input"][field]["points"].append(
                dict(day=record["output"]["as_of"][:10], value="0", source="future")
            )
    assert evidence_summary(periods, changed, replays) == baseline
