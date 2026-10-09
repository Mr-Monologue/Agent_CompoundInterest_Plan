"""Independent review reproducers and replacement gate scenarios; all synthetic."""

import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest
from test_r11_calculators import candidates, fixture_context, macro
from test_r11_service import archive_bundle, isolated  # noqa: F401

from investor_core.ledger import LedgerError
from investor_core.r11_candidates import CandidateInput, calculate_candidates
from investor_core.r11_inputs import Context, Series, Source, monthly
from investor_core.r11_provenance import leaves, publication_matches, reconcile
from investor_core.r11_service import R11Request
from investor_core.scheduler import digest


def test_single_missing_nav_preserves_other_product_score():
    body = candidates()
    body["products"][0]["nav"]["points"][-10]["value"] = None
    result = calculate_candidates(CandidateInput.model_validate(body))
    first, second = result["rows"]
    assert first["total_score"] is None
    assert Decimal(second["total_score"]) == Decimal("87.5")
    assert second["gaps"] == []
    assert result["leader"] is None
    assert all(r["rank"] is None for r in result["rows"])


def test_month_selection_ignores_not_yet_known_release_but_not_actual_hole():
    context = fixture_context()
    context["evidence_class"] = "K"
    later = deepcopy(context["sources"]["official"])
    later["published_at"] = later["first_retrieved_at"] = "2026-11-01T09:00:00+08:00"
    context["sources"]["later"] = later
    points = [
        dict(day=d, value="50", source="official")
        for d in ["2026-05-31", "2026-06-30", "2026-07-31", "2026-08-31"]
    ]
    points.append(dict(day="2026-09-30", value="49", source="later"))
    series = Series(identity="PMI", unit="PMI_POINTS", points=points)
    values, gaps = monthly(series, Context.model_validate(context), "PMI_POINTS")
    assert len(values) == 4 and not gaps
    series.points.pop(2)
    assert monthly(series, Context.model_validate(context), "PMI_POINTS")[1]
    context["evidence_class"] = "H"
    series.points.insert(2, Series(identity="x", unit="x", points=[points[2]]).points[0])
    assert monthly(series, Context.model_validate(context), "PMI_POINTS")[0][-1] == 49


@pytest.mark.parametrize("mode", ["future_date", "date_precision", "timezone_date"])
def test_archive_publication_contradiction_rejected(isolated, mode):  # noqa: F811
    _, _, service, _ = isolated
    body = macro()
    eid = archive_bundle(service, body)
    source_id = body["context"]["sources"]["official"]["archive_id"]
    with service.research._connect() as connection:
        row = connection.execute(
            "SELECT facts_json FROM market_research_evidence WHERE id=?", (source_id,)
        ).fetchone()
        stored = json.loads(row[0])
        if mode == "future_date":
            stored["published_date"] = "2026-10-17"
        elif mode == "date_precision":
            # Only nested timestamp assertion remains, no exact original timestamp.
            stored["facts"].pop("r11_publication_pointer")
        else:
            stored["published_date"] = "2019-12-31"
        connection.execute(
            "UPDATE market_research_evidence SET facts_json=? WHERE id=?",
            (json.dumps(stored), source_id),
        )
    with pytest.raises(LedgerError) as error:
        service.observe(R11Request(method="C", bundle_evidence_id=eid, idempotency_key=mode))
    assert error.value.code == "R11_PUBLICATION_CONFLICT"


def test_publication_date_uses_declared_source_timezone_and_day_end():
    source = fixture_context()["sources"]["official"]
    source.update(published_at="2026-10-09T17:00:00Z", publication_precision="DATE")
    parsed = Source.model_validate(source)
    assert publication_matches(parsed, {"published_date": "2026-10-10"})
    assert not publication_matches(parsed, {"published_date": "2026-10-09"})
    assert parsed.known_at().date() == date(2026, 10, 10)
    assert parsed.known_at().hour == 23


def enriched(cutoff):
    body = candidates(cutoff=cutoff)
    for name, char in [("secondary", "b"), ("review", "c")]:
        source = deepcopy(body["context"]["sources"]["official"])
        source.update(url="https://example.test/" + name, lineage=name, document_hash=char * 64)
        body["context"]["sources"][name] = source
    body["products"][0]["nav"]["points"][-253:] = [
        dict(point, value=str(Decimal("1.001") ** i))
        for i, point in enumerate(body["products"][0]["nav"]["points"][-253:])
    ]
    for product in body["products"]:
        product["benchmark_research_approved"] = True
    body["replacement_evidence"] = dict(
        account_ref="SYNTHETIC",
        reference_code="009163",
        held_since="2020-01-01",
        holding_source="official",
        thesis="WEAKENED",
        thesis_source="official",
        thesis_as_of=str(cutoff),
        platforms=[
            dict(
                code=p["code"],
                share_class="C",
                account_ref="SYNTHETIC",
                channel="SYNTHETIC",
                source="official",
                effective_from="2026-01-01T00:00:00+08:00",
                effective_to="2027-01-01T00:00:00+08:00",
                per_order_limit_cny=None,
                remaining_limit_cny=None,
                restriction="NONE",
                calendar_source="official",
                calendar_from=str(cutoff),
                calendar_to=str(cutoff + timedelta(days=7)),
                subscription_days=[str(cutoff + timedelta(days=2))],
                redemption_days=[str(cutoff + timedelta(days=2))],
            )
            for p in body["products"]
        ],
    )
    typed = CandidateInput.model_validate(body)
    body = typed.model_dump(mode="json")
    for product in body["products"]:
        fingerprint = digest({k: v for k, v in product.items() if k != "corroboration"})
        product["corroboration"] = dict(
            primary_source="official",
            independent_source="secondary",
            independence_evidence_source="review",
            primary_product_hash=fingerprint,
            corroborated_product_hash=fingerprint,
            reviewed_by="reviewer",
            prepared_by="author",
            review_source="review",
        )
    return body


def test_replacement_four_valid_weeks_and_warning_reset():
    history = []
    for i in range(4):
        body = enriched(date(2026, 10, 10) + timedelta(weeks=i))
        result = calculate_candidates(CandidateInput.model_validate(body), history)
        assert result["leading_weeks"] == i + 1
        history.append(result)
    assert result["replacement"] == "SHADOW_REPLACEMENT_REVIEW"
    assert not result["money_action"] and result["production_signal"] is None
    assert len(result["leading_score_history"]) == 4
    body = enriched(date(2026, 11, 7))
    body["context"]["sources"]["secondary"]["lineage"] = "SYNTHETIC"
    result = calculate_candidates(CandidateInput.model_validate(body), history)
    assert result["leading_weeks"] == 0 and result["replacement"] == "BLOCKED"
    assert "INDEPENDENT_UPSTREAM_NOT_ESTABLISHED" in result["rows"][0]["warnings"]


@pytest.mark.parametrize("change", ["thesis", "holding", "quota", "same_reviewer", "cost"])
def test_replacement_gate_rejects_incomplete_or_expensive_reference(change):
    body = enriched(date(2026, 10, 10))
    if change == "thesis":
        body["replacement_evidence"]["thesis"] = "INTACT"
    elif change == "holding":
        body["replacement_evidence"]["held_since"] = "2026-10-01"
    elif change == "quota":
        body["replacement_evidence"]["platforms"][0]["remaining_limit_cny"] = "0"
    elif change == "same_reviewer":
        body["products"][0]["corroboration"]["reviewed_by"] = "author"
    else:
        body["products"][0]["fees"]["subscription_value"] = "1.5"
    result = calculate_candidates(CandidateInput.model_validate(body))
    assert result["replacement"] == "BLOCKED"
    assert result["replacement_blockers"]


def test_original_value_binding_rejects_hash_mismatch_missing_and_bool_numeric_coercion():
    body = dict(code="003096", value="1.25", share_class="C", active=True)
    raw = json.dumps(body)
    archived = dict(
        excerpt=raw,
        original_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        facts={"r11_original_format": "JSON_UTF8"},
    )
    evidence = {"official": {"facts_json": json.dumps(archived)}}
    bindings = {p: dict(source="official", pointer=p) for p in leaves(body)}
    assert reconcile(body, evidence, bindings)["status"] == "MATCHED"
    assert reconcile(dict(body, value="1.26"), evidence, bindings)["status"] == "UNVERIFIED"
    assert reconcile(dict(body, active=1), evidence, bindings)["status"] == "UNVERIFIED"
    assert reconcile(body, evidence, {})["matched_count"] == 0
    archived["original_sha256"] = "0" * 64
    evidence["official"]["facts_json"] = json.dumps(archived)
    assert reconcile(body, evidence, bindings)["status"] == "UNVERIFIED"
