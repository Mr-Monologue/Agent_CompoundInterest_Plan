"""Natural effective-date expiry, native approvals, immutable historical replay."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from test_r11_calculators import candidates
from test_r11_complete_simulation import core_mappings
from test_r11_service import archive_bundle, isolated  # noqa: F401

from investor_core.benchmarks import BenchmarkService, MappingApproval, MappingDraft
from investor_core.notebook import NotebookService
from investor_core.r11_candidates import CandidateInput
from investor_core.r11_core_bindings import capture, changed
from investor_core.r11_governance import R11Governance
from investor_core.r11_service import R11Request


def approve_periods(service, scope, periods, previous, key):
    native = BenchmarkService(NotebookService(service.research))
    draft = native.create(
        MappingDraft(
            portfolio_id=scope["portfolio_id"],
            instrument_code="003096",
            expected_previous_version=previous,
            idempotency_key=key,
            official_disclosures=periods,
            diagnostic_mapping=periods,
            rationale="SYNTHETIC ONLY",
            limitations=["SYNTHETIC ONLY"],
        )
    )
    approved = native.approve(
        draft["id"],
        MappingApproval(
            confirmation_token=draft["confirmation_token"], confirmed_by="SYNTHETIC TEST"
        ),
    )
    scope["mapping_ids"]["003096"] = approved["id"]
    return approved


def period_from(service, scope):
    import json

    with service.research._connect() as c:
        return json.loads(
            c.execute(
                "SELECT payload_json FROM research_benchmark_mappings WHERE id=?",
                (scope["mapping_ids"]["003096"],),
            ).fetchone()[0]
        )["diagnostic_mapping"][0]


def test_source_timezone_day_boundary_downgrades_without_any_record_mutation(isolated):  # noqa: F811
    _, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service, ("003096", "009163"))
    period = period_from(service, scope)
    period["effective_to"] = "2026-10-12"
    approve_periods(service, scope, [period], 1, "expires")
    body = candidates("MEDICAL")
    eid = archive_bundle(service, body)
    clock[0] = datetime(2026, 10, 10, 4, tzinfo=UTC)
    run = service.observe(
        R11Request(
            method="MEDICAL", bundle_evidence_id=eid, idempotency_key="before-expiry", **scope
        )
    )
    assert run["output"]["status"] == "CALCULATED_SHADOW" and run["output"]["mapping_qualified"]
    bindings = run["evidence"]["core_bindings"]
    assert bindings["mappings"]["003096"]["source_timezone"] == "Asia/Shanghai"
    governance = R11Governance(service)
    with service.research._connect() as c:
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
    last = datetime(2026, 10, 12, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    clock[0] = last.astimezone(UTC)
    with service.research._connect() as c:
        assert not changed(c, bindings, clock[0])
    assert not governance.assess("MEDICAL", "last-day", "SYNTHETIC")["critical"]
    assert governance.gate("MEDICAL", "ADVISORY", dataset_kind="SYNTHETIC")["current"] == "ADVISORY"
    clock[0] += timedelta(seconds=1)
    assert clock[0].day == 12  # Host UTC date has NOT crossed; source date has.
    health = governance.assess("MEDICAL", "next-source-day", "SYNTHETIC")
    assert "CORE_MAPPING_OR_ACCOUNT_CHANGED" in health["critical"]
    assert governance.gate("MEDICAL", "ADVISORY", dataset_kind="SYNTHETIC")["current"] == "SHADOW"
    assert service.replay("MEDICAL", run["id"])["result"] == "PASS"


def test_future_start_gap_later_period_and_changed_path_matrix_with_native_approvals(isolated):  # noqa: F811
    _, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service, ("003096", "009163"))
    original = period_from(service, scope)
    initial = dict(deepcopy(original), effective_to="2026-10-12")
    later = dict(deepcopy(original), effective_from="2026-10-15")
    data = CandidateInput.model_validate(candidates("MEDICAL"))
    for version, periods, expected in [
        (
            2,
            [initial, later],
            {"2026-10-12": False, "2026-10-13": True, "2026-10-14": True, "2026-10-15": False},
        ),
        (3, [later], {"2026-10-14": True, "2026-10-15": False}),
        (
            4,
            [initial, dict(deepcopy(later), effective_from="2026-10-13")],
            {"2026-10-12": False, "2026-10-13": False},
        ),
    ]:
        approve_periods(service, scope, periods, version - 1, f"periods-{version}")
        with service.research._connect() as c:
            binding = capture(c, data, scope["portfolio_id"], scope["mapping_ids"], "2025-01-01")
            for day, invalid in expected.items():
                now = datetime.fromisoformat(day + "T12:00:00+08:00")
                assert changed(c, binding, now) is invalid, (version, day)
    altered = deepcopy(later)
    altered["components"][0]["code"] = "DIFFERENT_SYNTHETIC_PATH"
    approve_periods(service, scope, [initial, altered], 4, "changed-path")
    with service.research._connect() as c:
        binding = capture(c, data, scope["portfolio_id"], scope["mapping_ids"], "2025-01-01")
        assert not changed(c, binding, datetime(2026, 10, 12, tzinfo=UTC))
        assert changed(c, binding, datetime(2026, 10, 15, tzinfo=UTC))
        no_timezone = deepcopy(binding)
        no_timezone["mappings"]["003096"].pop("source_timezone")
        assert changed(c, no_timezone, datetime(2026, 10, 12, tzinfo=UTC))
