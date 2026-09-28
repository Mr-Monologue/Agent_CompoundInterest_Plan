"""Longitudinal semantics and append-only HTTP boundaries; all writes use fixtures."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_daily_client import dump
from test_holding_review import business, capture, setup

from investor_core.api.app import create_app
from investor_core.benchmarks import DiagnosticInput
from investor_core.delta_review_service import ObservationSave, ReviewService, SnapshotService
from investor_core.review_delta import DeltaEngine, observation, project, state_hash

SOURCE = dict(
    id="e1",
    source_lineage="official",
    source_ref="https://example.test/report",
    facts=dict(original_sha256="original-content", quality="SINGLE_SOURCE"),
    evidence_date="2026-06-30",
)


def record(category="DATA_GAP", **kwargs):
    return observation(
        "003096",
        "disclosure_coverage",
        category,
        kwargs.pop("value", {"covered": False}),
        evidence=[deepcopy(SOURCE)],
        **kwargs,
    )


def state(*records, day="2026-09-28"):
    return dict(
        records=list(records),
        observed_on=day,
        holdings=[dict(code="003096", name="example")],
        window_count=1,
    )


def compare(b, a):
    return DeltaEngine().evaluate(state(b), state(a))


def test_identical_and_description_replay():
    b = record(description="2026Q2 数据缺失")
    a = deepcopy(b)
    a["description"] = "截至二季度尚未找到数据"
    result = compare(b, a)
    assert result["summary"]["materiality"] == dict(MATERIAL=0, MINOR=0, NONE=1)
    assert result["summary"]["states"]["UNCHANGED"] == 1
    assert state_hash(state(b)) == state_hash(state(a))


def test_classification_transition_is_one_supported_resolution():
    b = record()
    a = record("OBSERVATION", value=dict(covered=True), sufficient=True)
    result = compare(b, a)
    assert len(result["deltas"]) == 1
    d = result["deltas"][0]
    assert d["status"] == "RESOLVED"
    assert d["transition"] == dict(
        type="CLASSIFICATION_CHANGED", **{"from": "DATA_GAP", "to": "OBSERVATION"}
    )
    assert result["summary"]["states"]["NEW"] == 0


def test_new_gap_and_disappearance_not_resolution():
    d = DeltaEngine().evaluate(state(), state(record()))["deltas"][0]
    assert d["status"] == "NEW"
    d = DeltaEngine().evaluate(state(record()), state())["deltas"][0]
    assert d["status"] == "REGRESSED" and "NOT_RESOLVED" in d["change_reason"]


@pytest.mark.parametrize("quality", ["UNAVAILABLE", "INVALID", "CONFLICT"])
def test_source_failure_is_regression(quality):
    b = record("OBSERVATION", quality="VALID")
    a = deepcopy(b)
    a["quality"] = quality
    assert compare(b, a)["deltas"][0]["status"] == "REGRESSED"


def test_window_update_and_small_values_are_minor_but_changed_conclusion_material():
    b = record(
        "OBSERVATION",
        value=dict(excess_percentage_points=-0.10001, conclusion="LAG"),
        dates=["2026-09-20"],
    )
    a = deepcopy(b)
    a["data_dates"] = ["2026-09-21"]
    d = compare(b, a)["deltas"][0]
    assert d["materiality"] == "MINOR" and d["status"] == "CHANGED"
    a["value"]["excess_percentage_points"] = -0.10002
    assert compare(b, a)["deltas"][0]["materiality"] == "MINOR"
    a["value"] = dict(excess_percentage_points=0.5, conclusion="LEAD")
    assert compare(b, a)["deltas"][0]["materiality"] == "MATERIAL"


def test_repeated_context_is_never_problem_or_risk():
    b = record("CONTEXT_OBSERVATION")
    d = compare(b, deepcopy(b))["deltas"][0]
    assert d["status"] == "UNCHANGED"
    assert not d["risk_alert"] and not d["unresolved_problem"]


def test_new_evidence_does_not_approve_mapping():
    b = record("CONFIG_PENDING", approval=False)
    a = deepcopy(b)
    a["evidence"][0]["facts"]["original_sha256"] = "new-official-content"
    d = compare(b, a)["deltas"][0]
    assert d["status"] == "CHANGED" and not d["after"]["approval_state"]
    assert not d["approval_mutation"] and d["unresolved_problem"]
    a["category"] = "OBSERVATION"
    a["resolution_supported"] = True
    assert compare(b, a)["deltas"][0]["status"] != "RESOLVED"


def test_expiry_only_with_rule_history_and_no_invented_rule():
    b = record("OBSERVATION", valid_until="2026-09-28", freshness_basis="confirmed watch")
    result = DeltaEngine().evaluate(state(b), state(b, day="2026-09-29"))
    assert result["deltas"][0]["status"] == "REGRESSED"
    assert (
        DeltaEngine().evaluate(state(b, day="2026-09-29"), state(b, day="2026-09-30"))["deltas"][0][
            "status"
        ]
        == "STALE"
    )
    b["freshness_basis"] = None
    assert (
        DeltaEngine().evaluate(state(b), state(b, day="2030-01-01"))["deltas"][0]["status"]
        == "UNCHANGED"
    )


def test_reordered_duplicated_and_refetched_source_no_change():
    b = record()
    a = deepcopy(b)
    a["evidence"] = [dict(a["evidence"][0], id="new-archive-id", created_at="tomorrow")] * 2
    d = compare(b, a)["deltas"][0]
    assert d["status"] == "UNCHANGED"
    a["evidence"][0]["source_ref"] = "https://example.test/new-url"
    assert compare(b, a)["deltas"][0]["materiality"] == "MINOR"


def test_unknowns_do_not_mean_resolution_and_quality_regresses():
    b = record("OBSERVATION", sufficient=True)
    a = record("DATA_GAP", sufficient=False)
    assert compare(b, a)["deltas"][0]["status"] == "REGRESSED"
    b = record("DATA_GAP")
    a = record("OBSERVATION", sufficient=False)
    assert compare(b, a)["deltas"][0]["status"] == "CHANGED"


def test_http_review_save_replay_history_and_financial_boundaries(tmp_path):
    settings, pid, aid, _, svc, inputs = setup(tmp_path)
    saved = svc.capture(capture(svc, pid, aid))
    history = deepcopy(svc.history(pid, aid))
    original = business(settings.db_path)
    web = TestClient(create_app(settings))
    q = dict(portfolio_id=pid, account_id=aid)
    before = dump(settings.db_path)
    result = web.get("/v1/holding-review-delta", params=q).json()["data"]
    assert result["summary"]["materiality"]["MATERIAL"] == 0
    assert result["baseline"]["snapshot_id"] == saved["snapshot_id"]
    assert "没有发现需要你处理的实质变化" in result["display_text"]
    for status in ["NEW", "RESOLVED", "CHANGED", "REGRESSED", "UNCHANGED", "STALE"]:
        assert web.get("/v1/holding-review-delta", params=dict(q, status=status)).status_code == 200
    assert dump(settings.db_path) == before
    # An identical explicit save is reused, not even a second event.
    req = dict(
        q,
        expected_state_hash=result["current_state_hash"],
        expected_baseline_id=saved["snapshot_id"],
        explicit_save=True,
        idempotency_key="save-same",
    )
    assert web.post("/v1/holding-review-observations", json=req).json()["data"]["reused"]
    assert dump(settings.db_path) == before
    # Production never receives this fixture mutation.
    inputs["idempotency_key"] = "changed-window"
    inputs["fund"]["points"][-1]["value"] = "0.6"
    svc.benchmarks.run(DiagnosticInput.model_validate(inputs))
    original = business(settings.db_path)
    result = web.get("/v1/holding-review-delta", params=q).json()["data"]
    assert result["summary"]["materiality"]["MATERIAL"] > 0
    assert len(svc.history(pid, aid)["snapshots"]) == 1
    assert (
        web.post("/v1/holding-review-observations", json=dict(req, explicit_save=False)).status_code
        == 422
    )
    assert web.post("/v1/holding-review-observations", json=req).status_code == 400
    req.update(expected_state_hash=result["current_state_hash"], idempotency_key="save-changed")
    with ThreadPoolExecutor(max_workers=3) as pool:
        events = list(
            pool.map(lambda _: SnapshotService(svc).save(ObservationSave(**req)), range(3))
        )
    assert len({e["snapshot_id"] for e in events}) == 1
    after = svc.history(pid, aid)
    assert len(after["snapshots"]) == 2 and len(after["events"]) == 2
    assert len(after["tasks"]) == len(history["tasks"])
    assert after["snapshots"][0] == history["snapshots"][0]
    assert business(settings.db_path) == original
    assert ReviewService(svc).read(pid, aid)["baseline"]["snapshot_id"] == saved["snapshot_id"]
    repeated = web.post(
        "/v1/holding-review-observations", json=dict(req, idempotency_key="another")
    ).json()["data"]
    assert repeated["snapshot_id"] == events[0]["snapshot_id"] and repeated["reused"]
    recomputed = web.get(
        "/v1/holding-review-observations/" + events[0]["snapshot_id"] + "/delta", params=q
    ).json()["data"]
    assert recomputed["summary"] == events[0]["comparison"]["delta_summary"]
    assert recomputed["persistence"] == "persisted"
    assert not recomputed["approval_mutation"] and not recomputed["holding_mutation"]
    assert (
        web.get(
            "/v1/holding-review-delta", params=dict(q, instrument_code="CORE01", view="DETAIL")
        ).status_code
        == 200
    )


def test_no_baseline_is_explicit_not_zero_changes(tmp_path):
    settings, pid, aid, _, _, _ = setup(tmp_path)
    web = TestClient(create_app(settings))
    result = web.get("/v1/holding-review-delta", params=dict(portfolio_id=pid, account_id=aid))
    assert result.status_code == 409


def test_legacy_projection_prose_and_order_does_not_change_identity(tmp_path):
    _, pid, aid, _, svc, _ = setup(tmp_path)
    raw = svc.build(pid, aid)
    changed = deepcopy(raw)
    changed["items"].reverse()
    for i in changed["items"]:
        i["reasons"].reverse()
        i["windows"].reverse()
        for r in i["reasons"]:
            r["text"] = "重新描述"
            r["reason_key"] = "old prose-derived key"
            r["evidence_version"] = "changed"
    assert state_hash(project(raw)) == state_hash(project(changed))
    assert (
        DeltaEngine().evaluate(project(raw), project(changed))["summary"]["materiality"]["MATERIAL"]
        == 0
    )


def test_observation_to_conflict_and_explicit_reconciliation():
    b = record("OBSERVATION", sufficient=True)
    a = record("EVIDENCE_DIFFERENCE", quality="CONFLICT")
    d = compare(b, a)["deltas"][0]
    assert d["status"] == "REGRESSED" and d["transition"]["to"] == "EVIDENCE_DIFFERENCE"
    solved = record("OBSERVATION", value=dict(reconciled=True), sufficient=True, quality="VALID")
    d = compare(a, solved)["deltas"][0]
    assert d["status"] == "RESOLVED" and len(compare(a, solved)["deltas"]) == 1


def test_source_removed_and_explicit_rolling_window_identity(tmp_path):
    b = record("OBSERVATION")
    a = deepcopy(b)
    a["evidence"] = []
    assert compare(b, a)["deltas"][0]["status"] == "REGRESSED"
    _, pid, aid, _, svc, _ = setup(tmp_path)
    old = svc.build(pid, aid)
    i = next(i for i in old["items"] if i["windows"])
    i["windows"][0]["window_key"] = "explicit-rolling-year"
    new = deepcopy(old)
    j = next(j for j in new["items"] if j["instrument_code"] == i["instrument_code"])
    j["windows"][0]["end"] = "2026-09-24"
    diffs = DeltaEngine().evaluate(project(old), project(new))["deltas"]
    d = next(d for d in diffs if d["window"] == "explicit-rolling-year")
    assert d["status"] == "CHANGED" and d["materiality"] == "MINOR"
    assert d["before"]["observation_key"] == d["after"]["observation_key"]


def test_ambiguous_identity_blocks_instead_of_silent_overwrite():
    with pytest.raises(ValueError, match="Ambiguous"):
        DeltaEngine().evaluate(state(record(), record()), state(record()))


def test_same_key_conflict_and_saved_history_recompute_after_later_changes(tmp_path):
    _, pid, aid, _, svc, inputs = setup(tmp_path)
    first = svc.capture(capture(svc, pid, aid))
    inputs["idempotency_key"] = "change-one"
    inputs["fund"]["points"][-1]["value"] = "0.5"
    svc.benchmarks.run(DiagnosticInput.model_validate(inputs))
    preview = ReviewService(svc).read(pid, aid)
    req = ObservationSave(
        portfolio_id=pid,
        account_id=aid,
        expected_baseline_id=first["snapshot_id"],
        expected_state_hash=preview["current_state_hash"],
        explicit_save=True,
        idempotency_key="one",
    )
    saved = SnapshotService(svc).save(req)
    before = SnapshotService(svc).recompute(pid, aid, saved["snapshot_id"])
    inputs["idempotency_key"] = "change-two"
    inputs["fund"]["points"][-1]["value"] = "0.4"
    svc.benchmarks.run(DiagnosticInput.model_validate(inputs))
    assert SnapshotService(svc).recompute(pid, aid, saved["snapshot_id"]) == before
    from investor_core.ledger import LedgerError

    with pytest.raises(LedgerError, match="key content"):
        SnapshotService(svc).save(req.model_copy(update={"expected_state_hash": "changed"}))


def test_client_review_queries_do_not_create_journal_and_lost_save_response_not_retried(tmp_path):
    import httpx

    from investor_core.daily_client import AssistantError, DailyClient

    settings, pid, aid, _, svc, inputs = setup(tmp_path)
    first = svc.capture(capture(svc, pid, aid))
    web = TestClient(create_app(settings))
    client = DailyClient(client=web, journal=tmp_path / "requests.db")
    q = dict(portfolio_id=pid, account_id=aid)
    result = client.get("/v1/holding-review-delta", **q)
    assert not (tmp_path / "requests.db").exists()
    inputs["idempotency_key"] = "change"
    inputs["fund"]["points"][-1]["value"] = "0.5"
    svc.benchmarks.run(DiagnosticInput.model_validate(inputs))
    result = client.get("/v1/holding-review-delta", **q)
    payload = dict(
        q,
        explicit_save=True,
        expected_baseline_id=first["snapshot_id"],
        expected_state_hash=result["current_state_hash"],
        idempotency_key="lost-response",
    )
    original = web.post

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise httpx.ReadTimeout("fixture response loss")

    web.post = lost
    with pytest.raises(AssistantError):
        client.workflow("review-save", payload)
    assert len(svc.history(pid, aid)["snapshots"]) == 2
    web.post = original
    with pytest.raises(AssistantError, match="禁止盲目重发"):
        client.workflow("review-save", payload)
    assert len(svc.history(pid, aid)["snapshots"]) == 2


def test_approved_mapping_new_candidate_stays_pending(tmp_path):
    from test_benchmarks import input_for, setup_mapping

    from investor_core.benchmarks import MappingApproval, MappingDraft
    from investor_core.holding_review import HoldingReviewService
    from investor_core.market_data import MarketDataService
    from investor_core.thesis import ThesisService

    settings, pid, aid, _, eid, bench, payload = setup_mapping(tmp_path)
    first = bench.create(MappingDraft.model_validate(payload))
    bench.approve(
        first["id"],
        MappingApproval(confirmation_token=first["confirmation_token"], confirmed_by="fixture"),
    )
    bench.run(DiagnosticInput.model_validate(input_for(first, eid)))
    svc = HoldingReviewService(bench, ThesisService(bench.notebook), MarketDataService(settings))
    svc.capture(capture(svc, pid, aid))
    payload.update(
        expected_previous_version=1, idempotency_key="candidate-two", rationale="new candidate only"
    )
    bench.create(MappingDraft.model_validate(payload))
    before = dump(settings.db_path)
    result = ReviewService(svc).read(pid, aid)
    d = next(d for d in result["deltas"] if d["subject"] == "mapping_candidate")
    assert d["status"] == "NEW" and d["after"]["category"] == "CONFIG_PENDING"
    assert d["after"]["approval_state"] is False
    assert bench.read(pid, "CORE01")["approved_research_version"] == 1
    assert dump(settings.db_path) == before


def test_legacy_source_without_original_digest_ignores_retrieval_metadata():
    b = record("OBSERVATION")
    b["evidence"][0]["facts"] = {"quality": "OFFICIAL", "metric": 12, "retrieved_at": "first"}
    b["evidence"][0]["facts_hash"] = "metadata-sensitive-first"
    a = deepcopy(b)
    a["evidence"][0]["facts"]["retrieved_at"] = "second"
    a["evidence"][0]["facts_hash"] = "metadata-sensitive-second"
    assert compare(b, a)["deltas"][0]["status"] == "UNCHANGED"
    a["evidence"][0]["facts"]["metric"] = 13
    assert compare(b, a)["deltas"][0]["materiality"] == "MATERIAL"


def test_previously_supported_provenance_becomes_unverified():
    valid = dict(SOURCE, facts=dict(quality="OFFICIAL", original_sha256="content"))
    unknown = dict(SOURCE, facts=dict(quality="UNVERIFIED", original_sha256="content"))
    b = observation("003096", "source", "OBSERVATION", {}, evidence=[valid])
    a = observation("003096", "source", "OBSERVATION", {}, evidence=[unknown])
    assert compare(b, a)["deltas"][0]["status"] == "REGRESSED"


def test_same_gap_code_with_changed_missing_dates_is_material(tmp_path):
    _, pid, aid, _, svc, _ = setup(tmp_path)
    old = svc.build(pid, aid)
    item = old["items"][0]
    item["reasons"].append(
        dict(
            kind="COVERAGE_GAP",
            basis=dict(missing={"INDEX": ["2026-09-21"]}),
            sources=[],
            category="DATA_GAP",
            text="missing",
            data_dates=[],
        )
    )
    new = deepcopy(old)
    new["items"][0]["reasons"][-1]["basis"]["missing"]["INDEX"] = ["2026-09-21", "2026-09-22"]
    differences = DeltaEngine().evaluate(project(old), project(new))["deltas"]
    row = next(
        d
        for d in differences
        if d["holding_id"] == item["instrument_code"] and d["subject"] == "comparison_coverage"
    )
    assert row["status"] == "CHANGED" and row["materiality"] == "MATERIAL"


def test_explicit_save_state_keeps_minor_publication_change_but_not_refetch_time():
    b = record()
    a = deepcopy(b)
    a["evidence"][0]["facts"]["published_date"] = "2026-09-28"
    assert compare(b, a)["deltas"][0]["materiality"] == "MINOR"
    assert state_hash(state(b)) != state_hash(state(a))
    b = deepcopy(a)
    a["evidence"][0]["facts"]["retrieved_at"] = "2026-09-29"
    assert state_hash(state(b)) == state_hash(state(a))
