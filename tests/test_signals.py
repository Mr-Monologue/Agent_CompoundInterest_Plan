from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerError, LedgerService
from investor_core.market_data import _instrument_plan_items
from investor_core.risk import RiskService
from investor_core.signals import SignalService
from investor_core.strategy import StrategyService


def configured_signal_services(
    database_path: Path,
    *,
    mapped: bool,
    contribution_eligible: bool,
    proxy_suitability: str = "STRONG",
) -> tuple[Settings, str]:
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    ledger = LedgerService(settings)
    portfolio = ledger.create_portfolio(name="信号测试组合")
    ledger.create_instrument(code="SAT001", name="卫星测试基金")
    if mapped:
        ledger.create_instrument(code="INDEX001", name="卫星测试指数", asset_type="INDEX")
    strategy = StrategyService(settings)
    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试卫星估值信号",
    )
    strategy.configure_instrument(
        portfolio_id=str(portfolio["id"]),
        instrument_code="SAT001",
        role="SATELLITE",
        contribution_eligible=contribution_eligible,
        target_weight_bps=None,
        priority=1,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code="INDEX001" if mapped else None,
        proxy_suitability=proxy_suitability if mapped else "NOT_APPLICABLE",
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="显式配置测试卫星标的",
    )
    return settings, str(portfolio["id"])


def record_valuation_history(
    settings: Settings,
    *,
    end: date,
    sample_count: int,
    current_low: bool = False,
    warning: bool = False,
) -> None:
    risk = RiskService(settings)
    start = end - timedelta(days=sample_count - 1)
    for offset in range(sample_count):
        observed = start + timedelta(days=offset)
        value = 10 if current_low and offset == sample_count - 1 else 20 + offset
        risk.record_valuation_observation(
            instrument_code="INDEX001",
            metric="PE",
            observation_date=observed.isoformat(),
            value=str(value),
            source_type="OFFICIAL",
            source_name="test-index-source",
            source_ref=f"test://index/{observed.isoformat()}",
            verification_status=(
                "UNVERIFIED" if warning and offset == sample_count - 1 else "VERIFIED"
            ),
            observed_at=f"{observed.isoformat()}T16:00:00+08:00",
        )


def commit_policy(service: SignalService, portfolio_id: str) -> dict[str, object]:
    draft = service.create_policy_draft(
        portfolio_id=portfolio_id,
        metric="PE",
        entry_max_percentile_bps=3000,
        lookback_days=1826,
        minimum_sample_count=30,
        maximum_observation_age_days=10,
        allow_warning_data=False,
        reason="用户批准低于等于 30% 分位时进入候选",
        actor_ref="test-user",
    )
    return service.commit_policy_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )


def test_signal_policy_requires_confirmation_and_is_versioned(tmp_path: Path) -> None:
    settings, portfolio_id = configured_signal_services(
        tmp_path / "investor.db",
        mapped=False,
        contribution_eligible=False,
    )
    service = SignalService(settings)
    draft = service.create_policy_draft(
        portfolio_id=portfolio_id,
        metric="PE",
        entry_max_percentile_bps=3000,
        lookback_days=1826,
        minimum_sample_count=30,
        maximum_observation_age_days=10,
        allow_warning_data=False,
        reason="测试显式政策确认",
    )

    with pytest.raises(LedgerError) as mismatch:
        service.commit_policy_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token="wrong",
            confirmed_by="test-user",
        )
    assert mismatch.value.code == "CONFIRMATION_MISMATCH"

    committed = service.commit_policy_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )
    replay = service.commit_policy_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )

    assert committed["policy"]["version"] == 1
    assert committed["policy"]["entry_max_percentile"] == "30.00"
    assert committed["strategy_changed"] is False
    assert committed["transactions_created"] is False
    assert replay["idempotent_replay"] is True


def test_signal_snapshot_blocks_missing_benchmark_without_guessing(tmp_path: Path) -> None:
    settings, portfolio_id = configured_signal_services(
        tmp_path / "investor.db",
        mapped=False,
        contribution_eligible=False,
    )
    service = SignalService(settings)
    commit_policy(service, portfolio_id)

    result = service.build_snapshot(
        portfolio_id=portfolio_id,
        as_of_date="2026-08-04",
    )

    assert result["state"] == "BLOCKED"
    assert result["items"][0]["state"] == "BLOCKED"
    assert result["items"][0]["reason_code"] == "BENCHMARK_REQUIRED"
    assert result["items"][0]["benchmark_code"] is None
    assert result["automatic_trade"] is False


def test_verified_low_percentile_opens_authorized_signal_idempotently(
    tmp_path: Path,
) -> None:
    settings, portfolio_id = configured_signal_services(
        tmp_path / "investor.db",
        mapped=True,
        contribution_eligible=True,
    )
    risk = RiskService(settings)
    start = date(2026, 7, 1)
    for offset in range(30):
        observed = start + timedelta(days=offset)
        risk.record_valuation_observation(
            instrument_code="INDEX001",
            metric="PE",
            observation_date=observed.isoformat(),
            value=str(20 + offset),
            source_type="OFFICIAL",
            source_name="测试官方指数源",
            source_ref=f"test://index/{observed.isoformat()}",
            verification_status="VERIFIED",
            observed_at=f"{observed.isoformat()}T16:00:00+08:00",
        )
    risk.record_valuation_observation(
        instrument_code="INDEX001",
        metric="PE",
        observation_date="2026-08-04",
        value="10",
        source_type="OFFICIAL",
        source_name="测试官方指数源",
        source_ref="test://index/2026-08-04",
        verification_status="VERIFIED",
        observed_at="2026-08-04T16:00:00+08:00",
    )
    service = SignalService(settings)
    commit_policy(service, portfolio_id)

    first = service.build_snapshot(portfolio_id=portfolio_id, as_of_date="2026-08-04")
    second = service.build_snapshot(portfolio_id=portfolio_id, as_of_date="2026-08-04")

    item = first["items"][0]
    assert first["state"] == "READY"
    assert item["state"] == "OPEN"
    assert item["reason_code"] == "VALUATION_SIGNAL_OPEN"
    assert item["percentile_bps"] <= 3000
    assert item["data_quality"] == "PASS"
    assert second["items"][0]["id"] == item["id"]
    assert len(service.list_snapshots(portfolio_id=portfolio_id)) == 1


def test_instrument_plan_requires_open_signal_when_policy_is_active() -> None:
    assignment = {
        "instruments": [
            {
                "id": "config-1",
                "instrument_id": "instrument-1",
                "instrument_code": "SAT001",
                "instrument_name": "卫星测试基金",
                "role": "SATELLITE",
                "status": "ACTIVE",
                "contribution_eligible": True,
                "thesis_status": "ACTIVE",
                "priority": 1,
                "target_weight_bps": None,
                "minimum_amount_minor": 1,
                "maximum_amount_minor": None,
                "benchmark_code": "INDEX001",
                "updated_at": "2026-08-04T00:00:00Z",
            }
        ]
    }
    policy = {
        "id": "policy-1",
        "version": 1,
        "metric": "PE",
        "entry_max_percentile_bps": 3000,
    }
    closed = _instrument_plan_items(
        assignment=assignment,
        role_allocations={"CORE": "0.00", "SATELLITE": "100.00"},
        data_quality="PASS",
        signal_policy=policy,
        signal_states={
            "SAT001": {
                "id": "snapshot-closed",
                "state": "CLOSED",
                "reason_code": "VALUATION_SIGNAL_CLOSED",
                "facts": {
                    "strategy_config_id": "config-1",
                    "strategy_config_updated_at": "2026-08-04T00:00:00Z",
                },
            }
        },
    )
    opened = _instrument_plan_items(
        assignment=assignment,
        role_allocations={"CORE": "0.00", "SATELLITE": "100.00"},
        data_quality="PASS",
        signal_policy=policy,
        signal_states={
            "SAT001": {
                "id": "snapshot-open",
                "state": "OPEN",
                "reason_code": "VALUATION_SIGNAL_OPEN",
                "facts": {
                    "strategy_config_id": "config-1",
                    "strategy_config_updated_at": "2026-08-04T00:00:00Z",
                },
            }
        },
    )
    stale = _instrument_plan_items(
        assignment=assignment,
        role_allocations={"CORE": "0.00", "SATELLITE": "100.00"},
        data_quality="PASS",
        signal_policy=policy,
        signal_states={
            "SAT001": {
                "id": "snapshot-stale",
                "state": "OPEN",
                "reason_code": "VALUATION_SIGNAL_OPEN",
                "facts": {
                    "strategy_config_id": "config-1",
                    "strategy_config_updated_at": "2026-08-03T00:00:00Z",
                },
            }
        },
    )

    assert closed[0]["instrument_code"] is None
    assert closed[0]["reason_code"] == "NO_OPEN_SATELLITE_SIGNAL"
    assert closed[0]["reserved_amount"] == "100.00"
    assert opened[0]["instrument_code"] == "SAT001"
    assert opened[0]["candidate_amount"] == "100.00"
    assert stale[0]["instrument_code"] is None
    assert stale[0]["reason_code"] == "SATELLITE_SIGNAL_SNAPSHOT_STALE"


@pytest.mark.parametrize(
    ("case", "contribution_eligible", "expected_state", "expected_reason"),
    [
        ("weak_proxy", True, "BLOCKED", "STRONG_PROXY_REQUIRED"),
        ("missing_history", True, "BLOCKED", "VALUATION_HISTORY_MISSING"),
        ("insufficient_samples", True, "BLOCKED", "VALUATION_SAMPLE_COUNT_INSUFFICIENT"),
        ("stale", True, "BLOCKED", "VALUATION_OBSERVATION_STALE"),
        ("warning", True, "BLOCKED", "VALUATION_DATA_QUALITY_BLOCKED"),
        ("not_authorized", False, "NOT_AUTHORIZED", "CONTRIBUTION_NOT_AUTHORIZED"),
        ("closed", True, "CLOSED", "VALUATION_SIGNAL_CLOSED"),
    ],
)
def test_signal_snapshot_safety_states(
    tmp_path: Path,
    case: str,
    contribution_eligible: bool,
    expected_state: str,
    expected_reason: str,
) -> None:
    settings, portfolio_id = configured_signal_services(
        tmp_path / "investor.db",
        mapped=True,
        contribution_eligible=contribution_eligible,
        proxy_suitability="WEAK" if case == "weak_proxy" else "STRONG",
    )
    if case == "insufficient_samples":
        record_valuation_history(settings, end=date(2026, 8, 4), sample_count=29)
    elif case == "stale":
        record_valuation_history(settings, end=date(2026, 7, 20), sample_count=30)
    elif case in {"warning", "not_authorized"}:
        record_valuation_history(
            settings,
            end=date(2026, 8, 4),
            sample_count=30,
            current_low=True,
            warning=case == "warning",
        )
    elif case == "closed":
        record_valuation_history(settings, end=date(2026, 8, 4), sample_count=30)

    service = SignalService(settings)
    commit_policy(service, portfolio_id)
    result = service.build_snapshot(portfolio_id=portfolio_id, as_of_date="2026-08-04")

    assert result["items"][0]["state"] == expected_state
    assert result["items"][0]["reason_code"] == expected_reason
    assert result["automatic_trade"] is False
