"""R1.1 evidence denominators and stability. Never confers promotion permission."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

from pydantic import ValidationError, model_validator

from investor_core.execution import StrictModel
from investor_core.r11_inputs import (
    COMPUTATION_VERSION,
    TZ,
    VERSION,
    Context,
    Series,
    monthly_points,
)


class ValidationWindow(StrictModel):
    method: Literal["C", "MEDICAL", "A500"]
    dataset_kind: Literal["REAL", "SYNTHETIC"] = "REAL"
    history_start: date
    history_end: date
    forward_start: date
    forward_end: date

    @model_validator(mode="after")
    def periods(self) -> ValidationWindow:
        days = [self.history_start, self.history_end, self.forward_start, self.forward_end]
        if any(d.weekday() != 5 for d in days):
            raise ValueError("planned weekly cutoffs must be Saturdays")
        if self.history_start > self.history_end or self.forward_start > self.forward_end:
            raise ValueError("ordered periods required")
        if self.history_end >= self.forward_start:
            raise ValueError("pre-registered history and forward periods must not overlap")
        return self


def weeks(start: date, end: date) -> list[date]:
    return [start + timedelta(weeks=i) for i in range((end - start).days // 7 + 1)]


def _selection(
    runs: list[dict[str, Any]], method: str, kind: str, dataset_kind: str = "REAL"
) -> dict[date, dict[str, Any]]:
    selected = {}
    for record in runs:
        row = record["output"]
        if (
            row.get("definition_id") == VERSION
            and row.get("computation_version") == COMPUTATION_VERSION
            and row.get("dataset_kind") == dataset_kind
            and row.get("evidence_class") == kind
            and row.get("method") == method
        ):
            selected[date.fromisoformat(row["as_of"][:10])] = record
    return selected


def _complete(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    output = record["output"]
    return (
        output["status"] == "CALCULATED_SHADOW"
        and not output.get("gaps")
        and output.get("mapping_qualified", True)
    )


def _coverage(days: list[date], records: dict[date, dict[str, Any]]) -> dict[str, Any]:
    valid = [d for d in days if _complete(records.get(d))]
    return dict(
        planned_weeks=len(days),
        valid_weeks=len(valid),
        ratio=str(Decimal(len(valid)) / len(days)),
        missing_dates=[d.isoformat() for d in days if d not in valid],
        last_four_complete=len(days) >= 4 and all(d in valid for d in days[-4:]),
    )


def _switches(values: list[str | None]) -> int:
    known = [v for v in values if v]
    return sum(a != b for a, b in pairwise(known))


def evidence_summary(
    window: ValidationWindow, runs: list[dict[str, Any]], replay_checks: dict[str, bool]
) -> dict[str, Any]:
    history_days = weeks(window.history_start, window.history_end)
    forward_days = weeks(window.forward_start, window.forward_end)
    historical = _selection(runs, window.method, "H", window.dataset_kind)
    forward = _selection(runs, window.method, "F", window.dataset_kind)
    h = _coverage(history_days, historical)
    f = _coverage(forward_days, forward)
    required_history = 104 if window.method == "C" else 52
    checks: dict[str, bool] = dict(
        history_sample=len(history_days) >= required_history,
        history_coverage=Decimal(h["ratio"]) >= Decimal(".95") and h["last_four_complete"],
        forward_sample=len(forward_days) >= 13 and f["valid_weeks"] >= 12,
        forward_last_four=f["last_four_complete"],
        replay_all_forward=all(
            d in forward and replay_checks.get(forward[d]["id"], False) for d in forward_days
        ),
        replay_all_existing_real_archives=all(
            replay_checks.get(r["id"], False)
            for r in runs
            if r["output"].get("definition_id") == VERSION
            and r["output"].get("method") == window.method
            and r["output"].get("dataset_kind") == window.dataset_kind
            and r["output"].get("evidence_class") in {"F", "K"}
        ),
    )
    outputs = [forward[d]["output"] if d in forward else None for d in forward_days]
    unique_windows = [
        row.get("window_end") for row in outputs if row and row["status"] == "CALCULATED_SHADOW"
    ]
    if window.method != "C":
        checks["forward_coverage"] = Decimal(f["ratio"]) >= Decimal(".95")
        checks["distinct_forward_windows"] = len(unique_windows) == len(set(unique_windows))
        leaders = [row.get("leader") if row else None for row in outputs]
        checks["ranking_stability"] = all(
            _switches(leaders[i : i + 13]) <= 3 for i in range(max(1, len(leaders) - 12))
        )
    else:
        seasons = [row.get("dominant_season") if row else "UNKNOWN" for row in outputs]
        uncertain = sum(s in {"UNKNOWN", "TRANSITION"} for s in seasons)
        checks["season_clarity"] = Decimal(uncertain) / len(seasons) <= Decimal(".25")
        known = [s if s not in {"UNKNOWN", "TRANSITION"} else None for s in seasons]
        checks["season_switches"] = all(
            _switches(known[i : i + 13]) <= 2 for i in range(max(1, len(known) - 12))
        )
        changes = [(day, season) for day, season in zip(forward_days, known, strict=True) if season]
        reversals = False
        for index, (day, season) in enumerate(changes):
            within = [s for d, s in changes[index + 1 :] if (d - day).days <= 28]
            different = False
            for value in within:
                different |= value != season
                if different and value == season:
                    reversals = True
        checks["no_four_week_reversal"] = not reversals
        for field in ["pmi", "social_financing_yoy"]:
            releases = set()
            for day in forward_days:
                if day not in forward or not _complete(forward[day]):
                    continue
                inputs = forward[day]["input"]
                try:
                    context = Context.model_validate(
                        dict(
                            inputs["context"],
                            as_of=forward[day]["output"]["as_of"],
                            evidence_class="F",
                        )
                    )
                    # Share the calculator's as-of selection; unused future releases
                    # must not hide already-known selected months.
                    points = monthly_points(Series.model_validate(inputs[field]), context)
                except ValidationError:
                    continue
                if points:
                    latest = points[-1]
                    source = context.sources.get(latest.source or "")
                    if source is None:
                        continue
                    start = datetime.combine(window.forward_start, time.min, TZ)
                    if start <= source.published_at <= source.known_at() <= context.as_of:
                        releases.add(latest.day.strftime("%Y-%m"))
            checks[field + "_three_new_months"] = len(releases) >= 3
    # Preconditions for robustness, never treat an empty/all-tied set as 100%.
    eligible_counts = {}
    for label, days, selected in [("H", history_days, historical), ("F", forward_days, forward)]:
        count = 0
        for day in days:
            record = selected.get(day)
            if not _complete(record):
                continue
            assert record is not None
            output = record["output"]
            if window.method == "C":
                count += output.get("dominant_season") not in {"UNKNOWN", "TRANSITION", None}
            else:
                count += bool(output.get("leader")) and not output.get("tied", True)
        eligible_counts[label] = count
    checks["robustness_history_sample"] = eligible_counts["H"] >= 26
    checks["robustness_forward_sample"] = eligible_counts["F"] >= 8
    return dict(
        definition_id=VERSION,
        method=window.method,
        dataset_kind=window.dataset_kind,
        simulation_only=window.dataset_kind == "SYNTHETIC",
        history=h,
        forward=f,
        robustness_eligible=eligible_counts,
        computed_checks=checks,
        computed_blockers=[k for k, v in checks.items() if not v],
        not_assessed_by_this_preview=[
            "PRE_REGISTERED_WINDOW_RECEIPT",
            "PER_SCENARIO_SENSITIVITY",
            "INDEPENDENT_SOURCE_RECONCILIATION",
            "INDEPENDENT_REVIEW_RECEIPT",
            "ENGINEERING_RELEASE_RECEIPT",
            "BASELINE_COST_DELAY_STRESS",
        ],
        promotion_eligible=False,
        actual_promotion_authorized=False,
        active_reachable=False,
        display_text="证据统计未等于晋级通过;缺少的验证适配与审批仍阻断。",
    )
