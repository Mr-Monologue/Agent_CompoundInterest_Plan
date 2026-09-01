from __future__ import annotations

from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient
from test_subscriptions import frozen_plan, submit

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService
from investor_core.strategy import StrategyService


def test_health_is_process_only(tmp_path: Path) -> None:
    settings = Settings(environment=Environment.TEST, db_path=tmp_path / "missing.db")
    response = TestClient(create_app(settings)).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_after_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)

    response = TestClient(create_app(settings)).get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] in {"PASS", "DEGRADED"}
    checks = {item["name"]: item for item in response.json()["checks"]}
    assert checks["sqlite-integrity"]["status"] == "PASS"
    assert checks["sqlite-wal"]["status"] == "PASS"
    assert checks["database-schema"]["status"] == "PASS"


def test_weekly_plan_skip_reconfirmation_api_is_discoverable(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    client = TestClient(
        create_app(Settings(environment=Environment.TEST, db_path=database_path))
    )

    paths = client.get("/openapi.json").json()["paths"]

    assert "/v1/weekly-plans/{plan_id}/skip-drafts" in paths
    assert "/v1/weekly-plan-skip-drafts/{draft_id}" in paths
    assert "/v1/weekly-plan-skip-drafts/{draft_id}/commit" in paths


def test_notification_test_api_requires_explicit_confirmation(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    client = TestClient(
        create_app(Settings(environment=Environment.TEST, db_path=database_path))
    )

    rejected = client.post(
        "/v1/notification-tests",
        json={"idempotency_key": "api-test", "confirmation": "yes"},
    )
    created = client.post(
        "/v1/notification-tests",
        json={
            "idempotency_key": "api-test",
            "confirmation": "SEND_TEST_NOTIFICATION",
        },
    )
    test_id = created.json()["data"]["test_request"]["id"]
    status = client.get(f"/v1/notification-tests/{test_id}")

    assert rejected.status_code == 422
    assert created.status_code == 200
    assert created.json()["data"]["outbox"]["status"] == "PENDING"
    assert status.json()["data"]["safety"]["transactions_created"] is False


def test_ready_fails_before_migration(tmp_path: Path) -> None:
    settings = Settings(environment=Environment.TEST, db_path=tmp_path / "missing.db")

    response = TestClient(create_app(settings)).get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "FAIL"


def test_ready_fails_when_business_timezone_is_unavailable(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(
        environment=Environment.TEST,
        db_path=database_path,
        timezone="Missing/Timezone",
    )

    response = TestClient(create_app(settings)).get("/ready")

    assert response.status_code == 503
    checks = {item["name"]: item for item in response.json()["detail"]["checks"]}
    assert checks["business-timezone"]["status"] == "FAIL"


def test_instrument_role_update_api_is_deprecated_draft_alias(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    client = TestClient(create_app(settings))
    portfolio = client.post("/v1/portfolios", json={"name": "测试组合"}).json()["data"]
    StrategyService(settings).assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试角色实例",
    )
    client.post(
        "/v1/instruments",
        json={"code": "005827", "name": "易方达蓝筹精选混合"},
    )

    changed = client.patch(
        "/v1/strategy-instruments/005827/role",
        json={
            "portfolio_id": portfolio["id"],
            "role": "SATELLITE",
            "expected_current_role": "UNASSIGNED",
            "reason": "用户明确归入卫星角色",
        },
    )
    conflict = client.patch(
        "/v1/strategy-instruments/005827/role",
        json={
            "portfolio_id": portfolio["id"],
            "role": "CORE",
            "expected_current_role": "CORE",
            "reason": "陈旧请求",
        },
    )

    assert changed.status_code == 200
    data = changed.json()["data"]
    assert data["draft"]["status"] == "PENDING"
    assert data["role_change"]["mutation_applied"] is False
    assert data["deprecation"]["replacement"] == "strategy_instrument_role_draft_create"
    current = StrategyService(settings).get_assignment(portfolio_id=str(portfolio["id"]))
    assert current["instruments"] == []
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "ROLE_CONFLICT"


def test_instrument_list_distinguishes_registration_and_strategy_roles(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    ledger = LedgerService(settings)
    strategy = StrategyService(settings)
    portfolio = ledger.create_portfolio(name="role-contract")
    scenarios = (
        ("040046", "UNASSIGNED", "CORE", True, 2000),
        ("000083", "CORE", "SATELLITE", False, None),
        ("003765", "SATELLITE", "UNASSIGNED", False, None),
    )
    for code, registration_role, _strategy_role, _eligible, _weight in scenarios:
        ledger.create_instrument(
            code=code,
            name=f"fund-{code}",
            registration_role=registration_role,
        )
    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="role-contract",
    )
    for code, _registration_role, strategy_role, eligible, weight in scenarios:
        strategy.configure_instrument(
            portfolio_id=str(portfolio["id"]),
            instrument_code=code,
            role=strategy_role,
            contribution_eligible=eligible,
            target_weight_bps=weight,
            priority=1,
            minimum_amount_minor=1,
            maximum_amount_minor=None,
            benchmark_code=None,
            thesis_status="ACTIVE",
            approved_by="test-user",
            reason="role-contract",
        )

    response = TestClient(create_app(settings)).get(
        "/v1/instruments", params={"portfolio_id": portfolio["id"]}
    )

    assert response.status_code == 200
    items = {item["code"]: item for item in response.json()["data"]["items"]}
    for code, registration_role, strategy_role, _eligible, _weight in scenarios:
        assert items[code]["registration_role"] == registration_role
        assert items[code]["strategy_role"] == strategy_role
        assert items[code]["role"] == registration_role
        assert "deprecated" in items[code]["role_deprecation"]

    current = TestClient(create_app(settings)).get(
        "/v1/strategy-assignment", params={"portfolio_id": portfolio["id"]}
    )
    configs = {
        item["instrument_code"]: item for item in current.json()["data"]["instruments"]
    }
    for code, registration_role, strategy_role, _eligible, _weight in scenarios:
        assert configs[code]["registration_role"] == registration_role
        assert configs[code]["strategy_role"] == strategy_role
        assert configs[code]["role"] == strategy_role
        assert "deprecated" in configs[code]["role_deprecation"]


def test_strategy_config_api_preserves_omitted_field_and_clears_explicit_null(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    client = TestClient(create_app(settings))
    portfolio = client.post("/v1/portfolios", json={"name": "测试组合"}).json()["data"]
    client.post("/v1/instruments", json={"code": "014978", "name": "测试基金"})
    strategy = StrategyService(settings)
    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试显式清空",
    )
    strategy.configure_instrument(
        portfolio_id=str(portfolio["id"]),
        instrument_code="014978",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=2000,
        priority=1,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="建立旧目标权重",
    )
    base_payload = {
        "portfolio_id": portfolio["id"],
        "instrument_code": "014978",
        "contribution_eligible": False,
        "role": "UNASSIGNED",
        "reason": "退出核心舱",
    }

    omitted = client.post("/v1/strategy-instrument-config-drafts", json=base_payload)
    explicit_null = client.post(
        "/v1/strategy-instrument-config-drafts",
        json={**base_payload, "target_weight_bps": None},
    )

    assert omitted.status_code == 200
    assert omitted.json()["data"]["draft"]["proposed"]["target_weight_bps"] == 2000
    assert explicit_null.status_code == 200
    assert explicit_null.json()["data"]["draft"]["proposed"]["target_weight_bps"] is None


def test_strategy_api_is_read_only_and_requires_explicit_cli_assignment(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    client = TestClient(
        create_app(Settings(environment=Environment.TEST, db_path=database_path))
    )
    portfolio = client.post("/v1/portfolios", json={"name": "测试组合"}).json()["data"]

    definitions = client.get("/v1/strategies")
    current = client.get(
        "/v1/strategy-assignment",
        params={"portfolio_id": portfolio["id"]},
    )
    protected = client.put(
        f"/v1/allocation-policy/{portfolio['id']}",
        json={
            "core_target_pct": "70",
            "satellite_target_pct": "30",
            "tolerance_pct": "10",
            "transition_trigger_pct": "15",
            "transition_exit_core_min_pct": "60",
            "transition_exit_satellite_max_pct": "40",
            "expected_version": 1,
            "reason": "用户批准调整目标",
        },
    )
    assert definitions.status_code == 200
    assert definitions.json()["data"]["items"][0]["strategy_key"] == "value-dca"
    assert current.status_code == 409
    assert current.json()["error"]["code"] == "STRATEGY_NOT_ASSIGNED"
    assert protected.status_code == 404


def test_transaction_draft_api_requires_commit_before_holding_changes(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    client = TestClient(create_app(settings))

    portfolio = client.post(
        "/v1/portfolios", json={"name": "测试组合", "base_currency": "CNY"}
    ).json()["data"]
    account = client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "测试账户",
            "platform": "模拟平台",
        },
    ).json()["data"]
    instrument = client.post(
        "/v1/instruments",
        json={"code": "DEMO001", "name": "模拟基金"},
    )
    assert instrument.status_code == 200

    draft_response = client.post(
        "/v1/transaction-drafts",
        json={
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "instrument_code": "DEMO001",
            "side": "BUY",
            "trade_date": "2026-07-20",
            "amount": "100.00",
            "nav": "1.250000",
            "shares": "80.000000",
            "platform": "模拟平台",
            "idempotency_key": "api-message-001",
        },
    )
    assert draft_response.status_code == 200
    draft_result = draft_response.json()["data"]
    assert draft_result["draft"]["status"] == "PENDING"
    assert client.get("/v1/holdings").json()["data"]["items"] == []

    commit_response = client.post(
        f"/v1/transaction-drafts/{draft_result['draft']['id']}/commit",
        json={
            "confirmation_token": draft_result["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert commit_response.status_code == 200
    commit_result = commit_response.json()["data"]
    assert commit_result["transaction"]["side"] == "BUY"
    assert commit_result["holding"]["total_shares"] == "80.000000"
    assert commit_result["holding"]["cost_amount"] == "100.00"


def test_investment_context_api_auto_selects_and_persists_single_account(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    client = TestClient(create_app(Settings(environment=Environment.TEST, db_path=database_path)))
    portfolio = client.post("/v1/portfolios", json={"name": "个人投资组合"}).json()["data"]
    account = client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "测试账户",
            "platform": "测试平台",
        },
    ).json()["data"]

    first = client.get("/v1/investment-context")
    second = client.get("/v1/investment-context")

    assert first.status_code == 200
    assert first.json()["data"]["source"] == "AUTO_SELECTED"
    assert first.json()["data"]["portfolio"]["id"] == portfolio["id"]
    assert first.json()["data"]["account"]["id"] == account["id"]
    assert second.json()["data"]["source"] == "SAVED"


def test_opening_position_api_uses_a_dedicated_confirmed_import_path(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
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
        json={
            "code": "022463",
            "name": "富国中证A500ETF发起式联接A",
            "asset_type": "FUND",
        },
    )

    draft_response = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "instrument_code": "022463",
            "as_of_date": "2026-07-20",
            "total_shares": "100.000000",
            "average_cost_nav": "1.2500",
            "platform": "测试平台",
            "idempotency_key": "api-opening-001",
            "note": "测试平台持仓页",
        },
    )
    assert draft_response.status_code == 200
    draft_result = draft_response.json()["data"]
    assert draft_result["draft"]["action"] == "OPENING"
    assert draft_result["draft"]["cost_amount"] == "125.00"
    assert draft_result["draft"]["average_cost_nav"] == "1.250000"
    assert draft_result["cost_basis_input"] == "AVERAGE_COST_NAV"
    assert client.get("/v1/holdings").json()["data"]["items"] == []

    wrong_commit = client.post(
        f"/v1/transaction-drafts/{draft_result['draft']['id']}/commit",
        json={
            "confirmation_token": draft_result["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert wrong_commit.status_code == 409
    assert wrong_commit.json()["error"]["code"] == "DRAFT_TYPE_MISMATCH"

    commit_response = client.post(
        f"/v1/opening-position-drafts/{draft_result['draft']['id']}/commit",
        json={
            "confirmation_token": draft_result["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert commit_response.status_code == 200
    commit_result = commit_response.json()["data"]
    assert commit_result["transaction"]["kind"] == "OPENING"
    assert commit_result["holding"]["total_shares"] == "100.000000"
    assert commit_result["holding"]["cost_amount"] == "125.00"
    assert commit_result["holding"]["average_cost_nav"] == "1.250000"


def test_opening_position_api_requires_exactly_one_cost_basis(tmp_path: Path) -> None:
    client = TestClient(
        create_app(Settings(environment=Environment.TEST, db_path=tmp_path / "not-used.db"))
    )
    payload = {
        "portfolio_id": "portfolio-1",
        "account_id": "account-1",
        "instrument_code": "FUND001",
        "as_of_date": "2026-07-17",
        "total_shares": "100.00",
        "platform": "测试平台",
        "idempotency_key": "invalid-opening",
    }

    missing = client.post("/v1/opening-position-drafts", json=payload)
    both = client.post(
        "/v1/opening-position-drafts",
        json={**payload, "cost_amount": "125.00", "average_cost_nav": "1.2500"},
    )

    assert missing.status_code == 422
    assert both.status_code == 422


def test_confirmation_date_only_create_and_revision_api_contract(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    _, planning, service, portfolio_id, account_id, plan = frozen_plan(database_path)
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    client = TestClient(create_app(planning.settings))

    date_only_response = client.post(
        f"/v1/external-subscriptions/{subscription['id']}/confirmation-drafts",
        json={
            "confirmed_at_precision": "DATE_ONLY",
            "confirmation_business_date": "2026-07-22",
            "nav_date": "2026-07-22",
            "nav": "1",
            "confirmed_shares": "100",
            "confirmed_amount": "100",
            "fee": "0",
            "refunded_amount": "0",
            "idempotency_key": "api-date-only-create",
        },
    )
    assert date_only_response.status_code == 200
    date_only_draft = date_only_response.json()["data"]["draft"]
    assert date_only_draft["payload"]["confirmed_at_precision"] == "DATE_ONLY"
    assert date_only_draft["payload"]["confirmed_at"] == "2026-07-21T16:00:00Z"

    exact = service.create_confirmation_draft(
        subscription_id=str(subscription["id"]),
        confirmed_at="2026-07-22T00:00:00+08:00",
        confirmation_business_date="2026-07-22",
        nav_date="2026-07-22",
        nav="1",
        confirmed_shares="100",
        confirmed_amount="100",
        fee="0",
        refunded_amount="0",
        idempotency_key="api-revise-existing",
    )
    revised_response = client.post(
        "/v1/external-subscription-confirmation-drafts/"
        f"{exact['draft']['id']}/revise",
        json={
            "expected_payload_hash": exact["draft"]["payload_hash"],
            "confirmed_at_precision": "DATE_ONLY",
        },
    )
    assert revised_response.status_code == 200
    revised = revised_response.json()["data"]
    assert revised["draft"]["id"] == exact["draft"]["id"]
    assert revised["draft"]["idempotency_key"] == exact["draft"]["idempotency_key"]
    assert revised["changed_fields"] == ["confirmed_at_precision"]
    assert revised["business_facts_created"] is False
