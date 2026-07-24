from __future__ import annotations

from pathlib import Path

import pytest
from conftest import migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerError, LedgerService
from investor_core.strategy import StrategyService


def services(database_path: Path) -> tuple[LedgerService, StrategyService]:
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    return LedgerService(settings), StrategyService(settings)


def test_public_strategy_has_no_user_instrument_defaults(tmp_path: Path) -> None:
    _ledger, strategy = services(tmp_path / "investor.db")

    definitions = strategy.list_definitions()

    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["strategy_key"] == "value-dca"
    assert definition["version"]["version"] == "1.6"
    parameters = definition["version"]["parameters"]
    assert parameters["instrument_selection"] == "INSTANCE_ALLOWLIST_ONLY"
    serialized = str(parameters)
    assert "022463" not in serialized
    assert "000510" not in serialized


def test_strategy_assignment_is_explicit_and_instrument_eligibility_defaults_false(
    tmp_path: Path,
) -> None:
    ledger, strategy = services(tmp_path / "investor.db")
    portfolio = ledger.create_portfolio(name="测试组合")
    ledger.create_instrument(code="FUND001", name="测试基金")

    with pytest.raises(LedgerError) as missing:
        strategy.get_assignment(portfolio_id=str(portfolio["id"]))
    assert missing.value.code == "STRATEGY_NOT_ASSIGNED"

    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="用户选择该策略",
    )
    assignment = strategy.configure_instrument(
        portfolio_id=str(portfolio["id"]),
        instrument_code="FUND001",
        role="CORE",
        contribution_eligible=False,
        target_weight_bps=None,
        priority=100,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="仅迁移角色, 尚未批准新增资金",
    )

    assert assignment["strategy"]["key"] == "value-dca"
    assert assignment["instruments"][0]["role"] == "CORE"
    assert assignment["instruments"][0]["contribution_eligible"] is False


def test_strategy_instrument_configuration_is_portfolio_local(tmp_path: Path) -> None:
    ledger, strategy = services(tmp_path / "investor.db")
    first = ledger.create_portfolio(name="组合一")
    second = ledger.create_portfolio(name="组合二")
    ledger.create_instrument(code="FUND001", name="测试基金")
    for portfolio in (first, second):
        strategy.assign(
            portfolio_id=str(portfolio["id"]),
            strategy_key="value-dca",
            strategy_version="1.6",
            instance_config={},
            approved_by="test-user",
            reason="测试独立实例",
        )

    strategy.configure_instrument(
        portfolio_id=str(first["id"]),
        instrument_code="FUND001",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=10000,
        priority=1,
        minimum_amount_minor=100,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="组合一允许定投",
    )
    strategy.configure_instrument(
        portfolio_id=str(second["id"]),
        instrument_code="FUND001",
        role="SATELLITE",
        contribution_eligible=False,
        target_weight_bps=None,
        priority=100,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="REVIEW_REQUIRED",
        approved_by="test-user",
        reason="组合二仅观察",
    )

    first_config = strategy.get_assignment(portfolio_id=str(first["id"]))["instruments"][0]
    second_config = strategy.get_assignment(portfolio_id=str(second["id"]))["instruments"][0]
    assert first_config["role"] == "CORE"
    assert first_config["contribution_eligible"] is True
    assert second_config["role"] == "SATELLITE"
    assert second_config["contribution_eligible"] is False


def test_index_cannot_become_a_contribution_target(tmp_path: Path) -> None:
    ledger, strategy = services(tmp_path / "investor.db")
    portfolio = ledger.create_portfolio(name="测试组合")
    ledger.create_instrument(
        code="INDEX001",
        name="测试基准",
        asset_type="INDEX",
    )
    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试策略实例",
    )

    with pytest.raises(LedgerError) as error:
        strategy.configure_instrument(
            portfolio_id=str(portfolio["id"]),
            instrument_code="INDEX001",
            role="CORE",
            contribution_eligible=True,
            target_weight_bps=None,
            priority=100,
            minimum_amount_minor=1,
            maximum_amount_minor=None,
            benchmark_code=None,
            thesis_status="ACTIVE",
            approved_by="test-user",
            reason="错误地把指数作为定投标的",
        )

    assert error.value.code == "INDEX_NOT_TRADABLE"


def test_role_update_preserves_explicit_contribution_eligibility(tmp_path: Path) -> None:
    ledger, strategy = services(tmp_path / "investor.db")
    portfolio = ledger.create_portfolio(name="测试组合")
    ledger.create_instrument(code="FUND001", name="测试基金")
    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试策略实例",
    )
    strategy.configure_instrument(
        portfolio_id=str(portfolio["id"]),
        instrument_code="FUND001",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=10000,
        priority=1,
        minimum_amount_minor=100,
        maximum_amount_minor=5000,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="显式批准定投",
    )

    updated = strategy.update_instrument_role(
        portfolio_id=str(portfolio["id"]),
        instrument_code="FUND001",
        role="SATELLITE",
        expected_current_role="CORE",
        reason="用户明确修改角色",
    )

    config = updated["assignment"]["instruments"][0]
    assert config["role"] == "SATELLITE"
    assert config["contribution_eligible"] is True
    assert config["target_weight_bps"] == 10000
    assert config["maximum_amount_minor"] == 5000


def test_strategy_configuration_requires_exact_confirmation(tmp_path: Path) -> None:
    ledger, strategy = services(tmp_path / "investor.db")
    portfolio = ledger.create_portfolio(name="测试组合")
    ledger.create_instrument(code="FUND001", name="测试基金")
    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试策略实例",
    )

    created = strategy.create_config_draft(
        portfolio_id=str(portfolio["id"]),
        instrument_code="FUND001",
        contribution_eligible=True,
        role="CORE",
        reason="用户明确批准该标的长期参与核心舱定投",
    )

    assert created["draft"]["status"] == "PENDING"
    assert created["draft"]["execution_status"] == "NOT_APPLIED"
    assert strategy.get_assignment(portfolio_id=str(portfolio["id"]))["instruments"] == []

    with pytest.raises(LedgerError) as mismatch:
        strategy.commit_config_draft(
            draft_id=str(created["draft"]["id"]),
            confirmation_token="wrong",
            confirmed_by="test-user",
        )
    assert mismatch.value.code == "CONFIRMATION_MISMATCH"

    committed = strategy.commit_config_draft(
        draft_id=str(created["draft"]["id"]),
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    config = committed["assignment"]["instruments"][0]
    assert config["instrument_code"] == "FUND001"
    assert config["contribution_eligible"] is True
    assert config["proxy_suitability"] == "NOT_APPLICABLE"
