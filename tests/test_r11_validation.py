"""Fixed denominators, no synthetic observation credits or implicit promotion."""

from datetime import date, timedelta

from test_r11_calculators import fixture_context

from investor_core.r11_inputs import VERSION
from investor_core.r11_validation import ValidationWindow, evidence_summary, weeks


def setup(method="A500"):
    forward = date(2026, 10, 10)
    count = 104 if method == "C" else 52
    window = ValidationWindow(
        method=method,
        history_start=forward - timedelta(weeks=count),
        history_end=forward - timedelta(weeks=1),
        forward_start=forward,
        forward_end=forward + timedelta(weeks=12),
    )
    runs = []
    for kind, days in [
        ("H", weeks(window.history_start, window.history_end)),
        ("F", weeks(window.forward_start, window.forward_end)),
    ]:
        for i, day in enumerate(days):
            runs.append(
                dict(
                    id=f"{kind}-{i}",
                    output=dict(
                        definition_id=VERSION,
                        dataset_kind="REAL",
                        evidence_class=kind,
                        method=method,
                        as_of=f"{day}T12:00:00+08:00",
                        status="CALCULATED_SHADOW",
                        gaps=[],
                        window_end=str(day - timedelta(days=1)),
                        leader="022463",
                        tied=False,
                        dominant_season="SPRING",
                    ),
                    input={
                        field: {"points": [{"day": f"2026-{10 + min(i // 4, 2):02d}-01"}]}
                        for field in ("pmi", "social_financing_yoy")
                    },
                )
            )
    for row in runs:
        row["input"]["context"] = fixture_context()
        i = int(row["id"].split("-")[1])
        published = forward + timedelta(weeks=4 * min(i // 4, 2))
        source = row["input"]["context"]["sources"]["official"]
        source["published_at"] = source["first_retrieved_at"] = f"{published}T09:00:00+08:00"
        for field in ("pmi", "social_financing_yoy"):
            row["input"][field]["points"][0]["source"] = "official"
    return window, runs, {r["id"]: True for r in runs}


def test_complete_statistics_never_promote_without_missing_adapters():
    window, runs, checks = setup()
    result = evidence_summary(window, runs, checks)
    assert all(result["computed_checks"].values())
    assert result["forward"]["valid_weeks"] == 13
    assert result["robustness_eligible"] == {"H": 52, "F": 13}
    assert result["not_assessed_by_this_preview"]
    assert not result["promotion_eligible"]
    assert not result["actual_promotion_authorized"]
    assert not result["active_reachable"]


def test_missing_week_remains_denominator_twelve_of_thirteen_fails_d():
    window, runs, checks = setup()
    runs = [r for r in runs if r["id"] != "F-3"]
    result = evidence_summary(window, runs, checks)
    assert result["forward"]["planned_weeks"] == 13
    assert result["forward"]["valid_weeks"] == 12
    assert result["computed_checks"]["forward_sample"]
    assert not result["computed_checks"]["forward_coverage"]
    assert not result["computed_checks"]["replay_all_forward"]


def test_synthetic_or_wrong_model_never_contributes():
    window, runs, checks = setup()
    for row in runs:
        row["output"]["dataset_kind"] = "SYNTHETIC"
    result = evidence_summary(window, runs, checks)
    assert result["forward"]["valid_weeks"] == 0
    assert result["history"]["valid_weeks"] == 0
    assert result["robustness_eligible"] == {"H": 0, "F": 0}
    for row in runs:
        row["output"]["dataset_kind"] = "REAL"
        row["output"]["method"] = "MEDICAL"
    assert evidence_summary(window, runs, checks)["history"]["valid_weeks"] == 0


def test_revisions_count_once_and_latest_failure_is_not_hidden():
    window, runs, checks = setup()
    prior = next(r for r in runs if r["id"] == "F-12")
    runs.append(
        dict(prior, id="revision", output=dict(prior["output"], status="INSUFFICIENT_DATA"))
    )
    checks["revision"] = True
    result = evidence_summary(window, runs, checks)
    assert result["forward"]["valid_weeks"] == 12
    assert not result["forward"]["last_four_complete"]


def test_ties_are_covered_but_not_robustness_samples_and_replay_failure_blocks():
    window, runs, checks = setup()
    for row in runs:
        row["output"].update(leader=None, tied=True)
    checks["F-2"] = False
    result = evidence_summary(window, runs, checks)
    assert result["forward"]["valid_weeks"] == 13
    assert result["robustness_eligible"] == {"H": 0, "F": 0}
    assert not result["computed_checks"]["robustness_forward_sample"]
    assert not result["computed_checks"]["replay_all_forward"]


def test_repeated_windows_and_excessive_rank_flips_fail():
    window, runs, checks = setup()
    for i, row in enumerate(r for r in runs if r["output"]["evidence_class"] == "F"):
        row["output"]["window_end"] = "2026-10-09"
        row["output"]["leader"] = "022463" if i % 2 else "022424"
    result = evidence_summary(window, runs, checks)
    assert not result["computed_checks"]["distinct_forward_windows"]
    assert not result["computed_checks"]["ranking_stability"]


def test_macro_reversals_uncertainty_and_month_counts():
    window, runs, checks = setup("C")
    result = evidence_summary(window, runs, checks)
    assert all(result["computed_checks"].values())
    forward = [r for r in runs if r["output"]["evidence_class"] == "F"]
    forward[1]["output"]["dominant_season"] = "SUMMER"
    for row in forward[5:9]:
        row["output"]["dominant_season"] = "TRANSITION"
    for row in forward:
        row["input"]["pmi"]["points"] = [{"day": "2026-09-30"}]
    result = evidence_summary(window, runs, checks)
    assert not result["computed_checks"]["no_four_week_reversal"]
    assert not result["computed_checks"]["season_clarity"]
    assert not result["computed_checks"]["pmi_three_new_months"]


def test_old_k_archive_replay_failure_blocks_and_carried_releases_do_not_count():
    window, runs, checks = setup("C")
    row = runs[0]
    runs.append(dict(row, id="old-k", output=dict(row["output"], evidence_class="K")))
    for record in runs:
        source = record["input"]["context"]["sources"]["official"]
        source["published_at"] = "2026-09-01T09:00:00+08:00"
    result = evidence_summary(window, runs, checks)
    assert not result["computed_checks"]["replay_all_existing_real_archives"]
    assert not result["computed_checks"]["pmi_three_new_months"]
    assert not result["computed_checks"]["social_financing_yoy_three_new_months"]
