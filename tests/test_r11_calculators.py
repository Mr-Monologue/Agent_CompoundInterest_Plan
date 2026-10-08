"""Isolated numerical hypotheses, not market or account evidence."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from investor_core.r11_candidates import CandidateInput, calculate_candidates
from investor_core.r11_inputs import Context, Source, down, up
from investor_core.r11_macro import MacroInput, calculate_macro


def fixture_context(cutoff=date(2026, 10, 10)):
    return dict(
        as_of=f"{cutoff}T12:00:00+08:00",
        evidence_class="F",
        dataset_kind="SYNTHETIC",
        sources={
            "official": dict(
                archive_id="synthetic-only",
                document_hash="a" * 64,
                url="https://example.test/fixture",
                lineage="SYNTHETIC",
                published_at="2020-01-01T09:00:00+08:00",
                publication_precision="INSTANT",
                publication_timezone="Asia/Shanghai",
                first_retrieved_at="2020-01-01T10:00:00+08:00",
                quality="OFFICIAL",
            )
        },
    )


def grid(cutoff=date(2026, 10, 10)):
    days = [cutoff - timedelta(days=i) for i in range(1, 2000)]
    days = sorted(d for d in days if d.weekday() < 5)[-1261:]
    return dict(
        source="official",
        covered_from=str(days[0]),
        covered_to=str(cutoff),
        dates=[str(d) for d in days],
    )


def series(identity, unit, dates, values):
    return dict(
        identity=identity,
        unit=unit,
        points=[
            dict(day=d, value=str(v), source="official") for d, v in zip(dates, values, strict=True)
        ],
    )


def macro(cutoff=date(2026, 10, 10)):
    calendar = grid(cutoff)
    days = calendar["dates"]
    prices = [Decimal(".999") ** i for i in range(1261)]
    return dict(
        context=fixture_context(cutoff),
        index_identity="SYNTHETIC_CSI_ALL",
        identity_source="official",
        calendar=calendar,
        pb=series("SYNTHETIC_CSI_ALL", "MULTIPLE", days, [2] * 1260 + [1]),
        pmi=series(
            "NBS_MANUFACTURING_PMI",
            "PMI_POINTS",
            ["2026-06-30", "2026-07-31", "2026-08-31", "2026-09-30"],
            [48, 49, 51, 52],
        ),
        social_financing_yoy=series(
            "PBC_TSF_STOCK_YOY_SAME_BASIS",
            "PERCENT",
            ["2026-06-30", "2026-07-31", "2026-08-31", "2026-09-30"],
            [8, 9, 9, 10],
        ),
        price=series("SYNTHETIC_CSI_ALL", "PRICE_INDEX_POINT", days, prices),
        constituents_date=days[-1],
        constituents_source="official",
        members=[
            dict(
                code="SYNTHETIC_STOCK",
                listed_on="2010-01-01",
                listing_source="official",
                corporate_action_source="official",
                wealth=series("SYNTHETIC_STOCK", "TOTAL_RETURN_POINT", days[-60:], range(1, 61)),
            )
        ],
    )


def candidates(cohort="MEDICAL", cutoff=date(2026, 10, 10)):
    calendar = grid(cutoff)
    days = calendar["dates"]
    codes = ["003096", "009163"] if cohort == "MEDICAL" else ["022463", "022424"]
    products = []
    for code in codes:
        products.append(
            dict(
                code=code,
                facts_source="official",
                currency="CNY",
                share_class="C" if cohort == "MEDICAL" else "A",
                product_type="ACTIVE_MEDICAL" if cohort == "MEDICAL" else "A500_FEEDER",
                same_class_since="2020-01-01",
                legally_operating=True,
                subscription_open=True,
                redemption_open=True,
                fund_total_assets_cny="100000000",
                assets_report_date="2026-09-30",
                stock_min_pct="60",
                stock_max_pct="95",
                medical_non_cash_min_pct="80",
                mainland_only=True,
                target_etf={"022463": "563220", "022424": "563800"}.get(code),
                manager_team_since="2020-01-01",
                manager_source="official",
                fees=dict(
                    source="official",
                    effective_from="2020-01-01",
                    subscription_method="GROSS_DIVIDE_ONE_PLUS_RATE",
                    subscription_value="0",
                    redemption_rate_pct_at_365_days="0",
                    standard_10000_365_day_scenario_supported=True,
                ),
                delay_source="official",
                subscription_confirmation_max_trading_days=1,
                redemption_arrival_max_trading_days=1,
                calendar=calendar,
                nav=series(code, "NAV_CNY_NET_INTERNAL_FEES", days, [1] * len(days)),
                dividend_coverage_source="official",
                dividend_coverage_from=days[0],
                dividend_coverage_to=days[-1],
                dividends=[],
                benchmark_mapping_source="official",
                benchmark_identity="SYNTHETIC_PUBLISHED_PATH",
                benchmark_currency="CNY",
                benchmark_return_basis="TOTAL_RETURN",
                benchmark_effective_from=days[0],
                benchmark_research_approved=False,
            )
        )
    return dict(
        context=fixture_context(cutoff),
        cohort=cohort,
        products=products,
        benchmark=series("000510CNY010", "TOTAL_RETURN_INDEX_POINT", days, [1] * len(days))
        if cohort == "A500"
        else None,
        benchmark_calendar=calendar if cohort == "A500" else None,
    )


def test_c_scores_stability_and_deterministic_replay():
    history = []
    for i in range(3):
        data = MacroInput.model_validate(macro(date(2026, 10, 10) + timedelta(weeks=i)))
        before = data.model_dump()
        output = calculate_macro(data, history)
        assert output == calculate_macro(data, history)
        assert data.model_dump() == before
        assert output["gaps"] == []
        assert {k: output["dimension_scores"][k] for k in ["V", "F", "L", "B"]} == {
            "V": "1.0",
            "F": "1.0",
            "L": "1",
            "B": "1",
        }
        assert output["candidate_season"] == "SPRING"
        assert output["dominant_season"] == ("SPRING" if i == 2 else "TRANSITION")
        assert output["probabilities"] is None and not output["money_action"]
        history.append(output)
    assert output["confidence"] == "MEDIUM"


@pytest.mark.parametrize("field", ["pb", "price"])
def test_c_stale_daily_is_unknown_without_hiding_other_dimensions(field):
    data = macro()
    data[field]["points"] = data[field]["points"][:-3]
    result = calculate_macro(MacroInput.model_validate(data))
    assert result["dominant_season"] == "UNKNOWN"
    assert any("STALE_DATA" in g for g in result["gaps"])
    assert result["dimension_scores"]["F"] == "1.0"


def test_source_time_classes_conflicts_and_no_backfill():
    data = macro()
    data["context"]["sources"]["official"]["first_retrieved_at"] = "2026-10-11T00:00:00+08:00"
    result = calculate_macro(MacroInput.model_validate(data))
    assert result["dominant_season"] == "UNKNOWN"
    assert any("NOT_KNOWN_AS_OF" in g for g in result["gaps"])
    data["context"]["evidence_class"] = "H"
    result = calculate_macro(MacroInput.model_validate(data))
    assert result["status"] == "CALCULATED_SHADOW" and result["retrospective_vintages"]
    data["context"]["sources"]["official"]["quality"] = "CONFLICT"
    assert calculate_macro(MacroInput.model_validate(data))["status"] == "INSUFFICIENT_DATA"


def test_month_gap_and_missing_constituent_path_are_not_zero():
    data = macro()
    data["pmi"]["points"][1]["day"] = "2026-05-31"
    data["members"][0]["wealth"]["points"].pop(10)
    result = calculate_macro(MacroInput.model_validate(data))
    assert result["dimension_scores"]["F"] is None
    assert result["dimension_scores"]["B"] is None
    assert result["dominant_season"] == "UNKNOWN"


@pytest.mark.parametrize("cohort,expected", [("MEDICAL", Decimal("87.5")), ("A500", Decimal(100))])
def test_two_pools_absolute_scores_and_ties(cohort, expected):
    data = CandidateInput.model_validate(candidates(cohort))
    output = calculate_candidates(data)
    assert output == calculate_candidates(data)
    assert output["status"] == "CALCULATED_SHADOW", output
    assert [Decimal(r["total_score"]) for r in output["rows"]] == [expected, expected]
    assert output["tied"] and output["leader"] is None
    assert all(r["rank"] is None for r in output["rows"])
    assert output["replacement"] == "BLOCKED"
    assert all("MAPPING_DRAFT" in r["warnings"] for r in output["rows"])


def test_d_own_series_and_common_window_staleness_block():
    data = candidates("A500")
    data["benchmark"]["points"] = data["benchmark"]["points"][:-3]
    output = calculate_candidates(CandidateInput.model_validate(data))
    assert output["status"] == "INSUFFICIENT_DATA"
    assert "COMMON_WINDOW_STALE" in output["gaps"]
    assert all(row["rank"] is None for row in output["rows"])


@pytest.mark.parametrize("gap_weeks", [1, 2])
def test_duplicate_week_window_does_not_count_as_fresh(gap_weeks):
    data = CandidateInput.model_validate(candidates())
    output = calculate_candidates(data)
    previous = deepcopy(output)
    previous["as_of"] = (data.context.as_of - timedelta(weeks=gap_weeks)).isoformat()
    previous["leading_weeks"] = 3
    current = calculate_candidates(data, [previous])
    assert "NO_NEW_WINDOW" in current["gaps"]
    assert current["leading_weeks"] == 0
    assert all(r["total_score"] is None for r in current["rows"])


def test_unavailable_fee_never_reweights_and_wrong_share_excluded():
    data = candidates()
    data["products"][0]["fees"] = None
    data["products"][1]["share_class"] = "A"
    output = calculate_candidates(CandidateInput.model_validate(data))
    assert output["rows"][0]["filter_result"] == "UNKNOWN"
    assert output["rows"][1]["filter_result"] == "EXCLUDED"
    assert all(row["total_score"] is None and row["rank"] is None for row in output["rows"])


def test_dividend_reinvested_once_and_numeric_context_reproducible():
    data = candidates()
    p = data["products"][0]
    p["dividends"] = [
        dict(day=p["nav"]["points"][-1]["day"], cash_per_unit=".1", source="official")
    ]
    request = CandidateInput.model_validate(data)
    output = calculate_candidates(request)
    assert Decimal(output["rows"][0]["metrics"]["return_pct"]) == 10
    with localcontext() as ctx:
        ctx.prec = 6
        assert calculate_candidates(request) == output


def test_exact_scopes_cutoff_and_nonfinite_rejected():
    data = macro()
    data["scope"] = "SECTOR:MEDICAL"
    with pytest.raises(ValidationError):
        MacroInput.model_validate(data)
    with pytest.raises(ValidationError):
        Context.model_validate({**fixture_context(), "as_of": "2026-10-10T11:59:00+08:00"})
    data = candidates()
    data["products"][0]["code"] = "003095"
    with pytest.raises(ValidationError):
        CandidateInput.model_validate(data)
    data = macro()
    data["pb"]["points"][0]["value"] = "NaN"
    with pytest.raises(ValidationError):
        MacroInput.model_validate(data)


def test_date_precision_uses_explicit_timezone_day_end():
    source = Source.model_validate(fixture_context()["sources"]["official"])
    source = source.model_copy(update={"publication_precision": "DATE"})
    assert source.known_at() == datetime.fromisoformat("2020-01-01T23:59:59.999999+08:00")
    assert up(Decimal(10), "0", "20") == down(Decimal(10), "0", "20") == 50
