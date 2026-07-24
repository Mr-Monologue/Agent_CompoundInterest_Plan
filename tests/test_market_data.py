from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings


def _client_with_holding(tmp_path: Path) -> tuple[TestClient, str, str]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(
        environment=Environment.TEST,
        db_path=database_path,
        market_nav_max_age_days=7,
    )
    client = TestClient(create_app(settings))
    portfolio = client.post("/v1/portfolios", json={"name": "个人投资组合"}).json()["data"]
    account = client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "测试账户",
            "platform": "测试平台",
        },
    ).json()["data"]
    client.post(
        "/v1/instruments",
        json={"code": "FUND001", "name": "测试基金A"},
    )
    draft = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "instrument_code": "FUND001",
            "as_of_date": "2026-07-17",
            "total_shares": "100.00",
            "average_cost_nav": "1.2500",
            "platform": "测试平台",
            "idempotency_key": "opening-FUND001",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    return client, str(portfolio["id"]), str(account["id"])


def test_market_nav_is_idempotent_and_preserves_source_evidence(tmp_path: Path) -> None:
    client, _, _ = _client_with_holding(tmp_path)
    payload = {
        "instrument_code": "FUND001",
        "nav_date": "2026-07-20",
        "nav": "1.500000",
        "source_type": "PLATFORM",
        "source_name": "测试来源页",
        "source_ref": "user-screenshot-20260720",
        "verification_status": "VERIFIED",
        "observed_at": "2026-07-21T18:00:00+08:00",
    }

    first = client.post("/v1/market-nav-snapshots", json=payload)
    second = client.post("/v1/market-nav-snapshots", json=payload)

    assert first.status_code == 200
    assert first.json()["data"]["created"] is True
    assert first.json()["meta"]["data_quality"] == "PASS"
    assert second.json()["data"]["created"] is False
    assert (
        first.json()["data"]["snapshot"]["record_hash"]
        == second.json()["data"]["snapshot"]["record_hash"]
    )


def test_portfolio_valuation_is_deterministic_from_holding_and_nav(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-20",
            "nav": "1.500000",
            "source_type": "PLATFORM",
            "source_name": "测试来源页",
            "verification_status": "VERIFIED",
            "observed_at": "2026-07-21T18:00:00+08:00",
        },
    )

    response = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["meta"]["data_quality"] == "PASS"
    assert result["data"]["totals"] == {
        "market_value": "150.00",
        "cost_amount": "125.00",
        "unrealized_pnl": "25.00",
    }
    position = result["data"]["positions"][0]
    assert position["market_value"] == "150.00"
    assert position["return_pct"] == "20.00"
    assert position["weight_pct"] == "100.00"


def test_missing_or_stale_nav_blocks_portfolio_amount_conclusions(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    missing = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()
    assert missing["meta"]["data_quality"] == "SOURCE_ERROR"
    assert missing["data"]["totals"] is None
    assert missing["data"]["positions"][0]["market_value"] is None

    client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-01",
            "nav": "1.500000",
            "source_type": "AGGREGATOR",
            "source_name": "测试聚合源",
            "verification_status": "UNVERIFIED",
            "observed_at": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
        },
    )
    stale = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()
    assert stale["meta"]["data_quality"] == "SOURCE_ERROR"
    assert stale["data"]["totals"] is None
    assert stale["data"]["positions"][0]["error"] == "NAV_STALE"


def test_conflicting_latest_navs_block_amount_conclusions(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    common = {
        "instrument_code": "FUND001",
        "nav_date": "2026-07-20",
        "source_type": "PLATFORM",
        "verification_status": "VERIFIED",
        "observed_at": "2026-07-21T18:00:00+08:00",
    }
    client.post(
        "/v1/market-nav-snapshots",
        json={**common, "nav": "1.500000", "source_name": "测试平台"},
    )
    client.post(
        "/v1/market-nav-snapshots",
        json={**common, "nav": "1.510000", "source_name": "基金公司"},
    )

    result = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()

    assert result["meta"]["data_quality"] == "SOURCE_ERROR"
    assert result["data"]["totals"] is None
    assert result["data"]["positions"][0]["error"] == "NAV_CONFLICT"


def test_independent_matching_evidence_corroborates_aggregator_nav(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    primary = client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-21",
            "nav": "1.534500",
            "source_type": "AGGREGATOR",
            "source_name": "Primary market adapter",
            "source_ref": "primary:FUND001:2026-07-21",
            "source_lineage": "EASTMONEY",
            "observed_at": "2026-07-21T22:00:00+08:00",
        },
    )
    assert primary.json()["meta"]["data_quality"] == "WARNING"

    verification = client.post(
        "/v1/market-data/verifications",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-21",
            "nav": "1.534500",
            "source_type": "PLATFORM",
            "source_name": "Independent professional platform",
            "source_ref": "professional:FUND001:2026-07-21",
            "source_lineage": "WIND",
            "observed_at": "2026-07-21T22:05:00+08:00",
        },
    )

    assert verification.status_code == 200
    payload = verification.json()
    assert payload["meta"]["data_quality"] == "PASS"
    assert payload["data"]["status"] == "MATCH"
    assert payload["data"]["created"] is True
    assert payload["data"]["primary_snapshot"]["source_type"] == "AGGREGATOR"
    assert payload["data"]["evidence_snapshot"]["source_type"] == "PLATFORM"
    assert payload["data"]["primary_snapshot"]["source_lineage"] == "EASTMONEY"
    assert payload["data"]["evidence_snapshot"]["source_lineage"] == "WIND"

    valuation = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()
    assert valuation["meta"]["data_quality"] == "PASS"
    assert valuation["data"]["totals"]["market_value"] == "153.45"
    position = valuation["data"]["positions"][0]
    assert position["corroboration"]["status"] == "MATCH"
    assert position["corroboration"]["source_count"] == 2


def test_independent_conflicting_evidence_blocks_amount_conclusions(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-21",
            "nav": "1.534500",
            "source_type": "AGGREGATOR",
            "source_name": "Primary market adapter",
            "source_ref": "primary:FUND001:2026-07-21",
            "source_lineage": "EASTMONEY",
            "observed_at": "2026-07-21T22:00:00+08:00",
        },
    )

    verification = client.post(
        "/v1/market-data/verifications",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-21",
            "nav": "1.535000",
            "source_type": "OFFICIAL",
            "source_name": "Fund manager disclosure",
            "source_ref": "https://example.invalid/FUND001/2026-07-21",
            "source_lineage": "FUND_MANAGER_OFFICIAL",
            "observed_at": "2026-07-21T22:05:00+08:00",
        },
    )

    assert verification.status_code == 200
    payload = verification.json()
    assert payload["meta"]["data_quality"] == "SOURCE_ERROR"
    assert payload["data"]["status"] == "CONFLICT"
    assert payload["data"]["nav_delta"] == "0.000500"

    valuation = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()
    assert valuation["meta"]["data_quality"] == "SOURCE_ERROR"
    assert valuation["data"]["totals"] is None
    assert valuation["data"]["positions"][0]["error"] == "NAV_CONFLICT"


def test_same_upstream_alias_cannot_corroborate_eastmoney(tmp_path: Path) -> None:
    client, _, _ = _client_with_holding(tmp_path)
    client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-21",
            "nav": "1.534500",
            "source_type": "AGGREGATOR",
            "source_name": "Eastmoney open-fund NAV (AKShare-compatible)",
            "source_ref": "https://fund.eastmoney.com/FUND001",
            "observed_at": "2026-07-21T22:00:00+08:00",
        },
    )

    verification = client.post(
        "/v1/market-data/verifications",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-21",
            "nav": "1.534500",
            "source_type": "PLATFORM",
            "source_name": "天天基金",
            "source_ref": "https://fund.eastmoney.com/FUND001",
            "source_lineage": "EASTMONEY",
            "observed_at": "2026-07-21T22:05:00+08:00",
        },
    )

    assert verification.status_code == 400
    assert verification.json()["error"]["code"] == "SOURCE_NOT_INDEPENDENT"


def test_portfolio_brief_exposes_versioned_allocation_policy(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "FUND001",
            "nav_date": "2026-07-21",
            "nav": "1.500000",
            "source_type": "AGGREGATOR",
            "source_name": "Eastmoney open-fund NAV (AKShare-compatible)",
            "source_ref": "https://fund.eastmoney.com/FUND001",
            "observed_at": "2026-07-21T22:00:00+08:00",
        },
    )

    response = client.get(
        "/v1/portfolio-brief",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["narrative_contract"] == {
        "mode": "EXACT_TEXT",
        "response_field": "display_text",
        "additions_allowed": False,
        "prohibited_inferences": [
            "ALLOCATION_IMBALANCE",
            "PERFORMANCE_ADJECTIVE",
            "RISK_TRIGGER",
            "SELL_TRIGGER",
            "DCA_RECOMMENDATION",
            "UNAVAILABLE_MUTATION",
        ],
        "instruction": (
            "Return display_text exactly. Do not add headings, summaries, interpretations, "
            "priorities, recommendations, questions, or next actions."
        ),
    }
    assert data["capabilities"]["allocation_assessment"] == {
        "available": True,
        "reason_code": "VERSIONED_POLICY_CONFIGURED",
    }
    assert data["capabilities"]["instrument_role_update"] == {
        "available": True,
        "reason_code": "AVAILABLE_WITH_EXPECTED_CURRENT_ROLE",
    }
    assert data["allocation_assessment"]["state"] == "BLOCKED_UNASSIGNED"
    assert data["allocation_assessment"]["policy"]["version"] == 1
    assert data["allocation_assessment"]["policy"]["policy"]["core_target_pct"] == "65.00"
    assert data["role_summary"]["CORE"]["target_pct"] == "65.00"
    assert data["role_summary"]["CORE"]["assessment"] == "UNDER_TARGET"
    assert data["role_summary"]["SATELLITE"]["assessment"] == "UNDER_TARGET"
    assert data["factual_findings"][0]["mutation_available"] is True
    assert data["factual_findings"][0]["mutation_tool"] == "instrument_role_update"
    assert data["source_evidence"] == {
        "upstream_lineages": ["EASTMONEY"],
        "independence_assessment": "SINGLE_UPSTREAM",
    }
    assert data["valuation"]["positions"][0]["policy_assessment"]["sell_rule"] == "NOT_EVALUATED"
    serialized = str(data).casefold()
    assert "too high" not in serialized
    assert "too low" not in serialized
    assert "严重失衡" not in data["display_text"]
    assert "浮亏较深" not in data["display_text"]
    assert "建议" not in data["display_text"]
    assert "NAV is single-source" not in data["display_text"]
    assert "净值为单一来源或未经独立验证" in data["display_text"]
    assert "配置评估:" in data["display_text"]
    assert "BLOCKED_UNASSIGNED" in data["display_text"]
    assert data["display_text"].startswith("投资状况概览\n数据日期: 2026-07-21")


def test_portfolio_brief_deterministically_flags_transition_and_formats_losses(
    tmp_path: Path,
) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    client.patch(
        "/v1/instruments/FUND001/role",
        json={
            "role": "CORE",
            "expected_current_role": "UNASSIGNED",
            "reason": "测试核心角色",
        },
    )
    client.post(
        "/v1/instruments",
        json={"code": "FUND002", "name": "测试基金B", "role": "SATELLITE"},
    )
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND002",
            "as_of_date": "2026-07-17",
            "total_shares": "100.00",
            "average_cost_nav": "1.2500",
            "platform": "测试平台",
            "idempotency_key": "opening-FUND002",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    for code, nav in (("FUND001", "0.100000"), ("FUND002", "0.900000")):
        client.post(
            "/v1/market-nav-snapshots",
            json={
                "instrument_code": code,
                "nav_date": "2026-07-21",
                "nav": nav,
                "source_type": "AGGREGATOR",
                "source_name": "Eastmoney open-fund NAV (AKShare-compatible)",
                "source_ref": f"https://fund.eastmoney.com/{code}",
                "observed_at": "2026-07-21T22:00:00+08:00",
            },
        )

    data = client.get(
        "/v1/portfolio-brief",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()["data"]

    assert data["allocation_assessment"]["state"] == "TRANSITION_REQUIRED"
    assert data["allocation_assessment"]["reason_code"] == (
        "DEVIATION_EXCEEDS_TRANSITION_TRIGGER"
    )
    assert data["allocation_assessment"]["actual"] == {
        "CORE": "10.00",
        "SATELLITE": "90.00",
    }
    assert data["role_summary"]["CORE"]["assessment"] == "UNDER_TARGET"
    assert data["role_summary"]["SATELLITE"]["assessment"] == "OVER_TARGET"
    assert data["allocation_assessment"]["automatic_selling_allowed"] is False
    assert "TRANSITION_REQUIRED" in data["display_text"]
    assert "优先使用新增资金，不自动卖出" in data["display_text"]  # noqa: RUF001
    assert "未实现盈亏 -¥115.00" in data["display_text"]
    assert "未实现盈亏 ¥-" not in data["display_text"]


def test_weekly_plan_preview_routes_incremental_funds_to_underweight_role(
    tmp_path: Path,
) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    client.patch(
        "/v1/instruments/FUND001/role",
        json={
            "role": "CORE",
            "expected_current_role": "UNASSIGNED",
            "reason": "测试核心角色",
        },
    )
    client.post(
        "/v1/instruments",
        json={"code": "FUND002", "name": "测试基金B", "role": "SATELLITE"},
    )
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND002",
            "as_of_date": "2026-07-17",
            "total_shares": "100.00",
            "average_cost_nav": "1.2500",
            "platform": "测试平台",
            "idempotency_key": "opening-weekly-FUND002",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    for code, nav in (("FUND001", "0.100000"), ("FUND002", "0.900000")):
        client.post(
            "/v1/market-nav-snapshots",
            json={
                "instrument_code": code,
                "nav_date": "2026-07-21",
                "nav": nav,
                "source_type": "AGGREGATOR",
                "source_name": "Eastmoney open-fund NAV (AKShare-compatible)",
                "source_ref": f"https://fund.eastmoney.com/{code}",
                "observed_at": "2026-07-21T22:00:00+08:00",
            },
        )

    response = client.get(
        "/v1/weekly-plan-preview",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "contribution_amount": "100.00",
            "as_of_date": "2026-07-21",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available"] is True
    assert data["state"] == "TRANSITION_CONTRIBUTION"
    assert data["plan"]["role_allocations"] == {
        "CORE": "100.00",
        "SATELLITE": "0.00",
    }
    assert data["plan"]["projected"]["CORE"]["actual_pct"] == "55.00"
    assert data["plan"]["projected"]["SATELLITE"]["actual_pct"] == "45.00"
    assert data["plan"]["transition_exit_condition_met"] is True
    assert data["execution_boundary"] == {
        "instrument_selection": "NOT_INCLUDED",
        "transaction_draft_created": False,
        "trade_executed": False,
        "automatic_selling_allowed": False,
    }
    assert "CORE ¥100.00 | SATELLITE ¥0.00" in data["display_text"]
    assert "不选择具体基金" in data["display_text"]


def test_weekly_plan_preview_blocks_amount_conclusions_without_valuation(
    tmp_path: Path,
) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)

    response = client.get(
        "/v1/weekly-plan-preview",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "contribution_amount": "100.00",
            "as_of_date": "2026-07-21",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available"] is False
    assert data["reason_code"] == "VALUATION_UNAVAILABLE"
    assert "不能生成任何金额分配结论" in data["display_text"]
    assert "role_allocations" not in data
