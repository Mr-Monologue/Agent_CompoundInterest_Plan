from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService
from investor_core.market_data import MarketDataService
from investor_core.market_providers import CanaryResult, NavObservation
from investor_core.market_sync import MarketSyncService


class FakeProvider:
    provider_id = "AKSHARE_OPEN_FUND"
    source_name = "AKShare fixture"
    source_type = "AGGREGATOR"
    contract_version = "fixture.v1"

    def __init__(self, *, fail_code: str | None = None, canary_status: str = "PASS") -> None:
        self.fail_code = fail_code
        self.canary_status = canary_status

    def canary(self, instrument_code: str, as_of: date) -> CanaryResult:
        del as_of
        return CanaryResult(
            provider_id=self.provider_id,
            source_name=self.source_name,
            source_type=self.source_type,
            library_version="1.18.64",
            contract_version=self.contract_version,
            status=self.canary_status,
            checked_at=datetime(2026, 7, 22, 8, tzinfo=UTC),
            details={"instrument_code": instrument_code},
            error_code=(None if self.canary_status == "PASS" else "FIXTURE_FAILURE"),
        )

    def fetch_nav(self, instrument_code: str, as_of: date) -> NavObservation:
        if instrument_code == self.fail_code:
            from investor_core.ledger import LedgerError

            raise LedgerError("PROVIDER_FETCH_FAILED", "fixture failure", http_status=503)
        return NavObservation(
            instrument_code=instrument_code,
            nav_date=as_of,
            nav=Decimal("1.234567"),
            observed_at=datetime(2026, 7, 22, 8, tzinfo=UTC),
            source_type=self.source_type,
            source_name=self.source_name,
            source_ref=f"fixture://{instrument_code}",
            raw_hash=f"raw-{instrument_code}",
            library_version="1.18.64",
            contract_version=self.contract_version,
        )


def _service(tmp_path: Path, provider: FakeProvider) -> tuple[MarketSyncService, LedgerService]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(
        environment=Environment.TEST,
        db_path=database_path,
        market_provider_canary_code="FUND001",
    )
    ledger = LedgerService(settings)
    service = MarketSyncService(
        settings,
        now=lambda: datetime(2026, 7, 22, 8, tzinfo=UTC),
        provider_factory=lambda _provider_id: provider,
    )
    return service, ledger


def test_canary_records_provider_contract_without_nav(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, FakeProvider())
    ledger.create_instrument(code="FUND001", name="测试基金A")

    result = service.run_canary(as_of_date_value="2026-07-21")
    status = service.status()

    assert result["status"] == "PASS"
    assert result["library_version"] == "1.18.64"
    assert status["sources"][0]["contract_version"] == "fixture.v1"
    assert status["runs"] == []


def test_sync_records_single_source_navs_as_warning_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service, ledger = _service(tmp_path, FakeProvider())
    ledger.create_instrument(code="FUND001", name="测试基金A")
    ledger.create_instrument(code="FUND002", name="测试基金B")

    first = service.sync_navs(
        instrument_codes=["FUND001", "FUND002"],
        as_of_date_value="2026-07-21",
    )
    second = service.sync_navs(
        instrument_codes=["FUND001", "FUND002"],
        as_of_date_value="2026-07-21",
    )

    assert first["status"] == "PASS"
    assert first["data_quality"] == "WARNING"
    assert first["succeeded_count"] == 2
    assert all(item["created"] is True for item in first["items"])
    assert all(item["created"] is False for item in second["items"])
    assert len(service.status()["runs"]) == 2


def test_automated_source_replay_is_idempotent_across_fetch_times(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, FakeProvider())
    ledger.create_instrument(code="FUND001", name="测试基金A")
    market_data = MarketDataService(service.settings)
    common = {
        "instrument_code": "FUND001",
        "nav_date_value": "2026-07-21",
        "nav": "1.234567",
        "source_type": "AGGREGATOR",
        "source_name": "测试聚合源",
        "source_ref": "fixture://FUND001",
    }

    first = market_data.record_nav_snapshot(
        **common,
        observed_at_value="2026-07-21T18:00:00+08:00",
    )
    replay = market_data.record_nav_snapshot(
        **common,
        observed_at_value="2026-07-21T18:05:00+08:00",
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert first["snapshot"]["record_hash"] == replay["snapshot"]["record_hash"]


def test_partial_provider_failure_is_a_source_error_without_fake_values(
    tmp_path: Path,
) -> None:
    service, ledger = _service(tmp_path, FakeProvider(fail_code="FUND002"))
    ledger.create_instrument(code="FUND001", name="测试基金A")
    ledger.create_instrument(code="FUND002", name="测试基金B")

    result = service.sync_navs(
        instrument_codes=["FUND001", "FUND002"],
        as_of_date_value="2026-07-21",
    )

    assert result["status"] == "PARTIAL"
    assert result["data_quality"] == "SOURCE_ERROR"
    assert result["succeeded_count"] == 1
    failed = next(item for item in result["items"] if item["status"] == "FAIL")
    assert failed == {
        "instrument_code": "FUND002",
        "status": "FAIL",
        "error": {"code": "PROVIDER_FETCH_FAILED", "message": "fixture failure"},
    }


def test_failed_canary_blocks_sync_before_any_nav_is_recorded(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, FakeProvider(canary_status="FAIL"))
    ledger.create_instrument(code="FUND001", name="测试基金A")

    with pytest.raises(Exception) as error:
        service.sync_navs(
            instrument_codes=["FUND001"],
            as_of_date_value="2026-07-21",
        )

    assert error.value.code == "PROVIDER_CANARY_FAILED"
    assert service.status()["runs"] == []


def test_open_fund_provider_rejects_non_fund_instruments(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, FakeProvider())
    ledger.create_instrument(code="INDEX001", name="测试指数", asset_type="INDEX", role="CORE")

    with pytest.raises(Exception) as error:
        service.sync_navs(
            instrument_codes=["INDEX001"],
            as_of_date_value="2026-07-21",
        )

    assert error.value.code == "PROVIDER_ASSET_UNSUPPORTED"


def test_sync_status_shape_is_json_serializable(tmp_path: Path) -> None:
    service, ledger = _service(tmp_path, FakeProvider())
    ledger.create_instrument(code="FUND001", name="测试基金A")
    service.sync_navs(instrument_codes=["FUND001"], as_of_date_value="2026-07-21")

    status: dict[str, Any] = service.status()
    assert status["runs"][0]["details"]["single_source"] is True
