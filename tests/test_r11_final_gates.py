"""Integration regressions for exact scoring scope and current health revisions."""

import json
from datetime import UTC, datetime

import pytest
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


@pytest.mark.parametrize("reverse", [False, True])
def test_latest_same_clock_correction_is_current_and_missing_original_is_critical(
    isolated,  # noqa: F811
    reverse,
):
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
    first, second = (good, bad) if reverse else (bad, good)
    old = service.observe(R11Request(method="C", bundle_evidence_id=first, idempotency_key="old"))
    corrected = service.observe(
        R11Request(
            method="C", bundle_evidence_id=second, idempotency_key="corrected", supersedes=old["id"]
        )
    )
    gate = R11Governance(service)
    with service.research._connect() as c:
        rows = service.shadow._rows(c, corrected["model_id"])
        assert ("INPUT_QUALITY_OR_MAPPING_FAILED" in gate._critical(c, rows)) is reverse
        source_id = body["context"]["sources"]["official"]["archive_id"]
        # Isolated corruption injection: preserve normal production FK protection.
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("DELETE FROM market_research_evidence WHERE id=?", (source_id,))
        assert "SOURCE_ARCHIVE_CHANGED" in gate._critical(c, rows)


def test_new_native_draft_is_safe_but_approved_version_downgrades_and_old_run_replays(isolated):  # noqa: F811
    from investor_core.benchmarks import BenchmarkService, MappingApproval, MappingDraft
    from investor_core.notebook import NotebookService
    from investor_core.r11_core_bindings import changed

    _, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service, ("003096", "009163"))
    body = candidates("MEDICAL")
    eid = archive_bundle(service, body)
    clock[0] = datetime(2026, 10, 10, 4, tzinfo=UTC)
    run = service.observe(
        R11Request(method="MEDICAL", bundle_evidence_id=eid, idempotency_key="original", **scope)
    )
    original = run["evidence"]["core_bindings"]
    payload = {
        key: value
        for key, value in original["mappings"]["003096"]["snapshot"].items()
        if key in MappingDraft.model_fields
    }
    payload.update(expected_previous_version=1, idempotency_key="version-two")
    native = BenchmarkService(NotebookService(service.research))
    draft = native.create(MappingDraft.model_validate(payload))
    governance = R11Governance(service)
    with service.research._connect() as c:
        assert not changed(c, original, clock[0])
        assert not governance._critical(c, service.shadow._rows(c, run["model_id"]))
        # Seed a simulated already-advisory state to test automatic downgrade,
        # never to claim it passed the qualification pipeline.
        service.shadow._append(
            c,
            run["model_id"],
            "REVIEW_CONFIRMATION",
            "sim-advisory",
            dict(
                request_hash="fixture",
                draft_id="fixture",
                target="ADVISORY",
                confirmed_by="SYNTHETIC_TEST",
            ),
        )
    native.approve(
        draft["id"],
        MappingApproval(
            confirmation_token=draft["confirmation_token"], confirmed_by="SYNTHETIC_TEST"
        ),
    )
    with service.research._connect() as c:
        assert changed(c, original, clock[0])
    result = governance.assess("MEDICAL", "mapping-superseded", "SYNTHETIC")
    assert "CORE_MAPPING_OR_ACCOUNT_CHANGED" in result["critical"]
    assert governance.gate("MEDICAL", "ADVISORY", dataset_kind="SYNTHETIC")["current"] == "SHADOW"
    assert service.replay("MEDICAL", run["id"])["result"] == "PASS"
