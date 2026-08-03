from __future__ import annotations

from datetime import date
from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.performance import PerformanceService


def _client(tmp_path: Path) -> tuple[TestClient, Settings, str]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    client = TestClient(create_app(settings))
    portfolio = client.post("/v1/portfolios", json={"name": "研究组合"}).json()["data"]
    client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "研究账户",
            "platform": "测试平台",
        },
    )
    client.post(
        "/v1/instruments",
        json={"code": "FUND001", "name": "公共候选基金", "asset_type": "FUND"},
    )
    for index in range(130):
        nav_date = date(2026, 1, 1).fromordinal(date(2026, 1, 1).toordinal() + index)
        response = client.post(
            "/v1/market-nav-snapshots",
            json={
                "instrument_code": "FUND001",
                "nav_date": nav_date.isoformat(),
                "nav": f"{1 + index / 1000:.6f}",
                "source_type": "OFFICIAL",
                "source_name": "基金管理人",
                "source_ref": "https://official.example/fund001",
                "source_lineage": "FUND_MANAGER_OFFICIAL",
                "verification_status": "VERIFIED",
                "observed_at": f"{nav_date.isoformat()}T18:00:00+08:00",
            },
        )
        assert response.status_code == 200
    return client, settings, str(portfolio["id"])


def test_sourced_research_and_discovery_are_immutable_facts(tmp_path: Path) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    source_contract = client.get("/v1/research-source-contract").json()["data"]
    assert source_contract["contract_version"] == "research-source-v4"
    assert source_contract["collection_run_tool"] == "research_collection_run_record"
    assert source_contract["configured_connectors"] == []
    assert source_contract["automatic_sync"] is False
    assert source_contract["model_may_fill_missing_facts"] is False
    evidence_payload = {
        "instrument_code": "FUND001",
        "evidence_date": "2026-05-10",
        "evidence_type": "FEES",
        "source_name": "基金管理人",
        "source_ref": "https://official.example/fund001/fees",
        "source_lineage": "FUND_MANAGER_OFFICIAL",
        "facts": {"management_fee_bps": 50},
    }
    first = client.post("/v1/market-research-evidence", json=evidence_payload)
    replay = client.post("/v1/market-research-evidence", json=evidence_payload)

    assert first.status_code == 200
    assert first.json()["data"]["automatic_trade"] is False
    assert first.json()["data"]["change"]["change_type"] == "INITIAL"
    assert replay.json()["data"]["idempotent_replay"] is True

    changed_evidence = client.post(
        "/v1/market-research-evidence",
        json={
            **evidence_payload,
            "evidence_date": "2026-05-11",
            "facts": {"management_fee_bps": 45, "custody_fee_bps": 10},
        },
    )
    assert changed_evidence.status_code == 200
    assert changed_evidence.json()["data"]["change"]["change_type"] == "CHANGED"
    assert changed_evidence.json()["data"]["change"]["added_keys"] == [
        "custody_fee_bps"
    ]
    assert changed_evidence.json()["data"]["change"]["changed_keys"] == [
        "management_fee_bps"
    ]
    evidence_changes = client.get(
        "/v1/market-research-evidence-changes",
        params={"instrument_code": "FUND001", "change_type": "CHANGED"},
    ).json()["data"]["items"]
    assert len(evidence_changes) == 1
    assert (
        evidence_changes[0]["change_boundary"]
        == "SOURCE_FACT_CHANGE_NOT_INVESTMENT_ADVICE"
    )

    payload = {
        "portfolio_id": portfolio_id,
        "instrument_codes": ["FUND001"],
        "as_of_date": "2026-05-10",
        "lookback_days": 180,
    }
    scan = client.post("/v1/market-discovery-runs", json=payload)
    scan_replay = client.post("/v1/market-discovery-runs", json=payload)

    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["items"][0]["state"] == "REVIEW"
    assert data["items"][0]["return_20d_bps"] is not None
    assert data["items"][0]["return_60d_bps"] is not None
    assert data["items"][0]["return_120d_bps"] is not None
    assert data["items"][0]["selection_boundary"] == "FACTS_ONLY_NOT_A_RECOMMENDATION"
    assert data["strategy_changed"] is False
    assert data["contribution_eligibility_changed"] is False
    assert data["automatic_trade"] is False
    assert data["change_summary"]["previous_run_id"] is None
    assert data["change_summary"]["initial_count"] == 1
    assert data["change_summary"]["attention_count"] == 1
    assert scan_replay.json()["data"]["idempotent_replay"] is True

    second_evidence = {
        **evidence_payload,
        "evidence_date": "2026-05-11",
        "evidence_type": "HOLDINGS",
        "source_ref": "https://official.example/fund001/holdings",
        "facts": {"top_holding_count": 10},
    }
    assert client.post(
        "/v1/market-research-evidence",
        json=second_evidence,
    ).status_code == 200
    changed = client.post(
        "/v1/market-discovery-runs",
        json={**payload, "as_of_date": "2026-05-11"},
    ).json()["data"]

    assert changed["change_summary"]["previous_run_id"] == data["id"]
    assert changed["change_summary"]["changed_count"] == 1
    assert changed["change_summary"]["attention_count"] == 1
    changes = client.get(
        "/v1/market-discovery-changes",
        params={
            "portfolio_id": portfolio_id,
            "run_id": changed["id"],
            "attention_only": True,
        },
    ).json()["data"]["items"]
    assert len(changes) == 1
    assert changes[0]["change_type"] == "CHANGED"
    assert changes[0]["metric_deltas"]["research_evidence_count"] == 2
    assert changes[0]["change_boundary"] == "FACTUAL_CHANGE_NOT_A_RECOMMENDATION"


def test_external_research_collection_run_records_exact_item_outcomes(
    tmp_path: Path,
) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    payload = {
        "portfolio_id": portfolio_id,
        "connector_key": "official-facts-adapter",
        "adapter_version": "1.0.0",
        "source_name": "基金管理人",
        "source_lineage": "FUND_MANAGER_OFFICIAL",
        "started_at": "2026-05-12T01:00:00Z",
        "finished_at": "2026-05-12T01:00:05Z",
        "items": [
            {
                "instrument_code": "FUND001",
                "evidence_date": "2026-05-12",
                "evidence_type": "FUND_PROFILE",
                "source_ref": "https://official.example/fund001/profile",
                "facts": {"fund_type": "混合型"},
            },
            {
                "instrument_code": "UNKNOWN",
                "evidence_date": "2026-05-12",
                "evidence_type": "FEES",
                "source_ref": "https://official.example/unknown/fees",
                "facts": {"management_fee_bps": 50},
            },
        ],
    }

    first = client.post("/v1/research-collection-runs", json=payload)
    replay = client.post("/v1/research-collection-runs", json=payload)

    assert first.status_code == 200
    result = first.json()["data"]
    assert result["execution_status"] == "PARTIAL"
    assert result["recorded_count"] == 1
    assert result["replayed_count"] == 0
    assert result["rejected_count"] == 1
    assert [item["ingestion_status"] for item in result["items"]] == [
        "RECORDED",
        "REJECTED",
    ]
    assert result["items"][1]["error_code"] == "INSTRUMENT_NOT_FOUND"
    assert result["strategy_changed"] is False
    assert result["transactions_created"] is False
    assert replay.json()["data"]["id"] == result["id"]
    assert replay.json()["data"]["idempotent_replay"] is True

    listed = client.get(
        "/v1/research-collection-runs",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"]
    assert len(listed) == 1
    assert listed[0]["connector_key"] == "OFFICIAL-FACTS-ADAPTER"


def test_discovery_requires_explicit_registered_universe(tmp_path: Path) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    response = client.post(
        "/v1/market-discovery-runs",
        json={
            "portfolio_id": portfolio_id,
            "instrument_codes": ["UNKNOWN"],
            "as_of_date": "2026-05-10",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DISCOVERY_INSTRUMENT_NOT_FOUND"


def test_source_configuration_and_coverage_tasks_are_confirmation_gated(
    tmp_path: Path,
) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    draft_response = client.post(
        "/v1/research-source-config-drafts",
        json={
            "portfolio_id": portfolio_id,
            "connector_key": "official-facts-adapter",
            "display_name": "基金管理人事实适配器",
            "enabled": True,
            "evidence_types": ["FEES", "HOLDINGS"],
            "source_lineages": ["FUND_MANAGER_OFFICIAL"],
            "credential_ref": "FUND_RESEARCH_API_KEY",
            "reason": "为本地研究范围启用已审查适配器能力",
        },
    )
    assert draft_response.status_code == 200
    draft = draft_response.json()["data"]

    assert client.get(
        "/v1/research-source-configs",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"] == []
    read_back = client.get(
        f"/v1/research-source-config-drafts/{draft['draft']['id']}"
    ).json()["data"]
    assert "confirmation_token" not in read_back
    assert read_back["draft"]["credential_ref"] == "FUND_RESEARCH_API_KEY"

    committed = client.post(
        f"/v1/research-source-config-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    ).json()["data"]
    assert committed["config"]["version"] == 1
    assert committed["automatic_collection"] is False
    assert committed["strategy_changed"] is False
    assert committed["transactions_created"] is False

    contract = client.get(
        "/v1/research-source-contract",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]
    assert contract["contract_version"] == "research-source-v4"
    assert contract["configured_connectors"][0]["connector_key"] == (
        "OFFICIAL-FACTS-ADAPTER"
    )
    assert contract["configured_connectors"][0]["credential_configured"] is True

    payload = {
        "portfolio_id": portfolio_id,
        "instrument_codes": ["FUND001"],
        "as_of_date": "2026-05-12",
        "required_evidence_types": ["FEES", "HOLDINGS"],
        "max_age_days": 120,
    }
    first = client.post("/v1/research-coverage-snapshots", json=payload)
    replay = client.post("/v1/research-coverage-snapshots", json=payload)
    assert first.status_code == 200
    coverage = first.json()["data"]
    assert coverage["status"] == "PARTIAL"
    assert coverage["reason_code"] == "RESEARCH_COLLECTION_REQUIRED"
    assert coverage["summary"]["missing_count"] == 2
    assert coverage["summary"]["collection_task_count"] == 2
    assert all(
        task["eligible_connectors"][0]["connector_key"]
        == "OFFICIAL-FACTS-ADAPTER"
        for task in coverage["collection_tasks"]
    )
    assert coverage["automatic_collection"] is False
    assert coverage["automatic_trade"] is False
    assert replay.json()["data"]["idempotent_replay"] is True

    evidence_payload = {
        "instrument_code": "FUND001",
        "evidence_date": "2026-05-12",
        "source_name": "基金管理人",
        "source_lineage": "FUND_MANAGER_OFFICIAL",
        "facts": {"available": True},
    }
    for evidence_type in ["FEES", "HOLDINGS"]:
        response = client.post(
            "/v1/market-research-evidence",
            json={
                **evidence_payload,
                "evidence_type": evidence_type,
                "source_ref": f"https://official.example/{evidence_type.lower()}",
            },
        )
        assert response.status_code == 200
    completed = client.post(
        "/v1/research-coverage-snapshots",
        json={**payload, "as_of_date": "2026-05-13"},
    ).json()["data"]
    assert completed["status"] == "COMPLETE"
    assert completed["data_quality"] == "PASS"
    assert completed["summary"]["current_count"] == 2
    assert completed["collection_tasks"] == []


def test_collection_task_claim_result_and_coverage_closure_are_audited(
    tmp_path: Path,
) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    draft = client.post(
        "/v1/research-source-config-drafts",
        json={
            "portfolio_id": portfolio_id,
            "connector_key": "official-facts-adapter",
            "display_name": "基金管理人事实适配器",
            "enabled": True,
            "evidence_types": ["FEES"],
            "source_lineages": ["FUND_MANAGER_OFFICIAL"],
            "credential_ref": "FUND_RESEARCH_API_KEY",
            "reason": "为研究采集任务启用已审查事实来源",
        },
    ).json()["data"]
    client.post(
        f"/v1/research-source-config-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    coverage = client.post(
        "/v1/research-coverage-snapshots",
        json={
            "portfolio_id": portfolio_id,
            "instrument_codes": ["FUND001"],
            "as_of_date": "2026-05-12",
            "required_evidence_types": ["FEES"],
            "max_age_days": 120,
        },
    ).json()["data"]

    built = client.post(
        "/v1/research-collection-tasks/build",
        json={"coverage_snapshot_id": coverage["id"]},
    ).json()["data"]
    replayed = client.post(
        "/v1/research-collection-tasks/build",
        json={"coverage_snapshot_id": coverage["id"]},
    ).json()["data"]
    assert built["created_count"] == 1
    assert replayed["created_count"] == 0
    assert replayed["idempotent_replay"] is True
    task_id = built["items"][0]["id"]

    claim = client.post(
        f"/v1/research-collection-tasks/{task_id}/claim",
        json={
            "connector_key": "official-facts-adapter",
            "executor_ref": "test-connector",
            "lease_minutes": 15,
        },
    )
    assert claim.status_code == 200
    lease = claim.json()["data"]
    assert lease["task"]["status"] == "CLAIMED"
    assert lease["task_package"]["maximum_items"] == 20
    assert "FUND_RESEARCH_API_KEY" not in str(lease)
    duplicate_claim = client.post(
        f"/v1/research-collection-tasks/{task_id}/claim",
        json={
            "connector_key": "official-facts-adapter",
            "executor_ref": "other-connector",
        },
    )
    assert duplicate_claim.status_code == 409
    assert duplicate_claim.json()["error"]["code"] == (
        "RESEARCH_COLLECTION_TASK_ALREADY_CLAIMED"
    )

    base_result = {
        "lease_token": lease["lease_token"],
        "adapter_version": "1.0.0",
        "source_name": "基金管理人",
        "source_lineage": "FUND_MANAGER_OFFICIAL",
        "started_at": "2026-05-12T01:00:00Z",
        "finished_at": "2026-05-12T01:00:05Z",
    }
    out_of_scope = client.post(
        f"/v1/research-collection-tasks/{task_id}/result",
        json={
            **base_result,
            "items": [
                {
                    "instrument_code": "FUND001",
                    "evidence_date": "2026-05-12",
                    "evidence_type": "HOLDINGS",
                    "source_ref": "https://official.example/fund001/holdings",
                    "facts": {"top_holding_count": 10},
                }
            ],
        },
    )
    assert out_of_scope.status_code == 409
    assert out_of_scope.json()["error"]["code"] == (
        "RESEARCH_COLLECTION_RESULT_OUT_OF_SCOPE"
    )

    result_payload = {
        **base_result,
        "items": [
            {
                "instrument_code": "FUND001",
                "evidence_date": "2026-05-12",
                "evidence_type": "FEES",
                "source_ref": "https://official.example/fund001/fees",
                "facts": {"management_fee_bps": 50},
            }
        ],
    }
    result = client.post(
        f"/v1/research-collection-tasks/{task_id}/result",
        json=result_payload,
    )
    assert result.status_code == 200
    completed = result.json()["data"]
    assert completed["task"]["status"] == "COMPLETED"
    assert completed["attempt"]["status"] == "SUCCEEDED"
    assert completed["collection_run"]["recorded_count"] == 1
    assert completed["coverage_change"]["gap_closed"] is True
    assert completed["coverage_change"]["previous_snapshot_id"] == coverage["id"]
    assert completed["coverage_change"]["followup_snapshot_id"] != coverage["id"]
    assert completed["automatic_trade"] is False

    result_replay = client.post(
        f"/v1/research-collection-tasks/{task_id}/result",
        json=result_payload,
    ).json()["data"]
    assert result_replay["idempotent_replay"] is True
    assert result_replay["collection_run"]["id"] == completed["collection_run"]["id"]
    listed = client.get(
        "/v1/research-collection-tasks",
        params={"portfolio_id": portfolio_id, "status": "COMPLETED"},
    ).json()["data"]["items"]
    assert [item["id"] for item in listed] == [task_id]
    runtime = client.get(
        "/v1/research-collection-runtime-status",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]
    assert runtime["task_counts"]["COMPLETED"] == 1
    assert runtime["active_lease_count"] == 0
    assert runtime["connectors"][0]["success_count"] == 1
    assert runtime["automatic_trade"] is False


def test_collection_task_failure_is_retryable_without_fake_evidence(
    tmp_path: Path,
) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    draft = client.post(
        "/v1/research-source-config-drafts",
        json={
            "portfolio_id": portfolio_id,
            "connector_key": "official-facts-adapter",
            "display_name": "基金管理人事实适配器",
            "enabled": True,
            "evidence_types": ["FEES"],
            "source_lineages": ["FUND_MANAGER_OFFICIAL"],
            "reason": "测试采集失败回执",
        },
    ).json()["data"]
    client.post(
        f"/v1/research-source-config-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    coverage = client.post(
        "/v1/research-coverage-snapshots",
        json={
            "portfolio_id": portfolio_id,
            "instrument_codes": ["FUND001"],
            "as_of_date": "2026-05-12",
            "required_evidence_types": ["FEES"],
        },
    ).json()["data"]
    task = client.post(
        "/v1/research-collection-tasks/build",
        json={"coverage_snapshot_id": coverage["id"]},
    ).json()["data"]["items"][0]
    claim = client.post(
        f"/v1/research-collection-tasks/{task['id']}/claim",
        json={"connector_key": "official-facts-adapter"},
    ).json()["data"]
    failed = client.post(
        f"/v1/research-collection-tasks/{task['id']}/result",
        json={
            "lease_token": claim["lease_token"],
            "adapter_version": "1.0.0",
            "source_name": "基金管理人",
            "source_lineage": "FUND_MANAGER_OFFICIAL",
            "started_at": "2026-05-12T01:00:00Z",
            "finished_at": "2026-05-12T01:00:05Z",
            "items": [],
            "failure_code": "SOURCE_UNAVAILABLE",
        },
    )
    assert failed.status_code == 200
    data = failed.json()["data"]
    assert data["task"]["status"] == "PENDING"
    assert data["attempt"]["status"] == "FAILED"
    assert data["collection_run"] is None
    assert data["coverage_change"] is None
    assert data["evidence_recorded"] is False
    assert client.get(
        "/v1/market-research-evidence",
        params={"instrument_code": "FUND001", "evidence_type": "FEES"},
    ).json()["data"]["items"] == []


def test_review_action_requires_confirmed_decision(tmp_path: Path) -> None:
    client, settings, portfolio_id = _client(tmp_path)
    # A temporary opening balance makes the existing performance review produce
    # deterministic action items while keeping market discovery independent.
    account_id = client.get("/v1/accounts").json()["data"]["items"][0]["id"]
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND001",
            "as_of_date": "2026-01-01",
            "total_shares": "10",
            "average_cost_nav": "1",
            "platform": "测试平台",
            "idempotency_key": "research-review-opening",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    review = PerformanceService(settings).prepare_review(
        portfolio_id=portfolio_id,
        review_type="MONTHLY",
        anchor_date=date(2026, 1, 31),
    )
    action = review["action_items"][0]
    draft = client.post(
        f"/v1/review-action-items/{action['id']}/decision-drafts",
        json={
            "decision": "ACKNOWLEDGE",
            "reason": "已看到该数据缺口, 后续补齐",
        },
    ).json()["data"]

    unchanged = client.get(
        "/v1/periodic-reviews",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"][0]
    assert next(
        item for item in unchanged["action_items"] if item["id"] == action["id"]
    )["status"] == "OPEN"

    committed = client.post(
        f"/v1/review-action-decision-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert committed.status_code == 200
    assert committed.json()["data"]["decision"]["new_status"] == "ACKNOWLEDGED"
    assert committed.json()["data"]["holdings_changed"] is False
    assert committed.json()["data"]["transactions_created"] is False


def test_watchlist_lifecycle_requires_confirmation_and_never_changes_strategy(
    tmp_path: Path,
) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    draft = client.post(
        "/v1/research-watchlist-transition-drafts",
        json={
            "portfolio_id": portfolio_id,
            "instrument_code": "FUND001",
            "new_state": "CANDIDATE",
            "reason": "加入显式研究候选池",
        },
    ).json()["data"]

    assert client.get(
        "/v1/research-watchlist",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"] == []

    committed = client.post(
        f"/v1/research-watchlist-transition-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    ).json()["data"]
    assert committed["transition"]["new_state"] == "CANDIDATE"
    assert committed["strategy_changed"] is False
    assert committed["contribution_eligibility_changed"] is False
    assert committed["transactions_created"] is False

    observing = client.post(
        "/v1/research-watchlist-transition-drafts",
        json={
            "portfolio_id": portfolio_id,
            "instrument_code": "FUND001",
            "new_state": "OBSERVING",
            "reason": "开始持续观察来源事实",
            "review_due_date": "2026-08-31",
        },
    ).json()["data"]
    client.post(
        f"/v1/research-watchlist-transition-drafts/{observing['draft']['id']}/commit",
        json={
            "confirmation_token": observing["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    item = client.get(
        "/v1/research-watchlist",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"][0]
    assert item["state"] == "OBSERVING"
    assert (
        item["watchlist_boundary"]
        == "RESEARCH_CLASSIFICATION_ONLY_NO_STRATEGY_OR_TRADE_CHANGE"
    )

    snapshot = client.post(
        "/v1/research-watchlist-review-snapshots",
        json={
            "portfolio_id": portfolio_id,
            "as_of_date": "2026-09-01",
        },
    ).json()["data"]
    replay = client.post(
        "/v1/research-watchlist-review-snapshots",
        json={
            "portfolio_id": portfolio_id,
            "as_of_date": "2026-09-01",
        },
    ).json()["data"]
    assert snapshot["status"] == "REVIEW_REQUIRED"
    assert snapshot["reason_code"] == "WATCHLIST_REVIEW_DUE"
    assert snapshot["summary"]["due_count"] == 1
    assert snapshot["items"][0]["due_status"] == "DUE"
    assert snapshot["items"][0]["observation_started_at"] is not None
    assert snapshot["items"][0]["observation_days"] is not None
    assert snapshot["strategy_changed"] is False
    assert snapshot["transactions_created"] is False
    assert replay["idempotent_replay"] is True

    unchanged = client.get(
        "/v1/research-watchlist",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"][0]
    assert unchanged["state"] == "OBSERVING"
    listed = client.get(
        "/v1/research-watchlist-review-snapshots",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"]
    assert len(listed) == 1
    assert listed[0]["snapshot_boundary"].startswith("REVIEW_FACTS_ONLY")


def test_resolved_review_action_accepts_confirmed_outcome_and_trend_reports_it(
    tmp_path: Path,
) -> None:
    client, settings, portfolio_id = _client(tmp_path)
    account_id = client.get("/v1/accounts").json()["data"]["items"][0]["id"]
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND001",
            "as_of_date": "2026-01-01",
            "total_shares": "10",
            "average_cost_nav": "1",
            "platform": "测试平台",
            "idempotency_key": "research-outcome-opening",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    review = PerformanceService(settings).prepare_review(
        portfolio_id=portfolio_id,
        review_type="MONTHLY",
        anchor_date=date(2026, 1, 31),
    )
    action_id = review["action_items"][0]["id"]
    resolution = client.post(
        f"/v1/review-action-items/{action_id}/decision-drafts",
        json={
            "decision": "RESOLVE",
            "reason": "该复盘检查已完成",
        },
    ).json()["data"]
    client.post(
        f"/v1/review-action-decision-drafts/{resolution['draft']['id']}/commit",
        json={
            "confirmation_token": resolution["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    outcome = client.post(
        f"/v1/review-action-items/{action_id}/outcome-drafts",
        json={
            "outcome": "COMPLETED",
            "evidence_quality": "VERIFIED",
            "evidence_ref": "https://evidence.example/review-result",
            "note": "已核对并补齐对应事实",
        },
    ).json()["data"]
    committed = client.post(
        f"/v1/review-action-outcome-drafts/{outcome['draft']['id']}/commit",
        json={
            "confirmation_token": outcome["confirmation_token"],
            "confirmed_by": "test-user",
        },
    ).json()["data"]
    assert committed["outcome"]["outcome"] == "COMPLETED"
    assert committed["strategy_changed"] is False
    assert committed["transactions_created"] is False

    listed = client.get(
        "/v1/review-action-outcomes",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"]
    assert len(listed) == 1
    assert listed[0]["evidence_quality"] == "VERIFIED"

    trend = client.post(
        "/v1/review-trend-snapshots",
        json={
            "portfolio_id": portfolio_id,
            "as_of_date": "2026-07-30",
            "lookback_reviews": 12,
        },
    ).json()["data"]
    summary = trend["action_summary"]["outcome_summary"]
    assert summary["resolved_count"] == 1
    assert summary["recorded_outcome_count"] == 1
    assert summary["missing_outcome_count"] == 0
    assert summary["outcome_coverage_bps"] == 10000
    assert summary["outcome_counts"]["COMPLETED"] == 1
