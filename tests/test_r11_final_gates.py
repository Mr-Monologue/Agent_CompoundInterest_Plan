"""Integration regressions for exact scoring scope and current health revisions."""

import json
from datetime import UTC, datetime

from test_r11_calculators import candidates, macro
from test_r11_complete_simulation import core_mappings
from test_r11_second_review import new_bundle
from test_r11_service import archive_bundle, isolated  # noqa: F401

from investor_core.r11_candidates import CandidateInput
from investor_core.r11_core_bindings import capture
from investor_core.r11_governance import R11Governance
from investor_core.r11_service import R11Request


def test_approved_mapping_must_match_index_actually_used_for_a500_score(isolated):  # noqa: F811
    _, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service)
    data = CandidateInput.model_validate(candidates("A500"))
    with service.research._connect() as c:
        for product in data.products:
            product.benchmark_identity = "OTHER_SYNTHETIC_INDEX"
            mid = scope["mapping_ids"][product.code]
            row = json.loads(
                c.execute(
                    "SELECT payload_json FROM research_benchmark_mappings WHERE id=?", (mid,)
                ).fetchone()[0]
            )
            row["diagnostic_mapping"][0]["components"][0]["code"] = product.benchmark_identity
            c.execute(
                "UPDATE research_benchmark_mappings SET payload_json=? WHERE id=?",
                (json.dumps(row), mid),
            )
        result = capture(c, data, scope["portfolio_id"], scope["mapping_ids"], "2020-01-01")
    assert all(
        "MAPPING_DIFFERS_FROM_SCORED_BENCHMARK" in row["blockers"]
        for row in result["mappings"].values()
    )


def test_latest_same_clock_correction_is_current_and_missing_original_is_critical(isolated):  # noqa: F811
    _, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 10, 4, tzinfo=UTC)
    body = macro()
    good = archive_bundle(service, body)
    bad = new_bundle(
        service,
        good,
        "bad",
        lambda f: f["r11_bindings"].update(
            {"/pb/points/1260/value": dict(source="missing", pointer="/value")}
        ),
    )
    old = service.observe(R11Request(method="C", bundle_evidence_id=bad, idempotency_key="old"))
    corrected = service.observe(
        R11Request(
            method="C", bundle_evidence_id=good, idempotency_key="corrected", supersedes=old["id"]
        )
    )
    gate = R11Governance(service)
    with service.research._connect() as c:
        rows = service.shadow._rows(c, corrected["model_id"])
        assert "INPUT_QUALITY_OR_MAPPING_FAILED" not in gate._critical(c, rows)
        source_id = body["context"]["sources"]["official"]["archive_id"]
        c.execute("DELETE FROM market_research_evidence WHERE id=?", (source_id,))
        assert "SOURCE_ARCHIVE_CHANGED" in gate._critical(c, rows)
