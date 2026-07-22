"""Canary-gated market data synchronization orchestration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from investor_core.config import Settings
from investor_core.ledger import LedgerError, utc_now
from investor_core.market_data import MarketDataService
from investor_core.market_providers import (
    AKSHARE_PROVIDER_ID,
    CanaryResult,
    MarketDataProvider,
    build_provider,
)

JsonDict = dict[str, Any]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class MarketSyncService:
    """Run provider canaries before bounded, auditable NAV synchronization."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utc_now,
        provider_factory: Callable[[str], MarketDataProvider] | None = None,
    ) -> None:
        self.settings = settings
        self._now = now
        self._provider_factory = provider_factory or (
            lambda provider_id: build_provider(
                provider_id,
                timeout_seconds=settings.market_provider_timeout_seconds,
            )
        )
        self._market_data = MarketDataService(settings, now=now)

    def _connect(self) -> sqlite3.Connection:
        path = (
            ":memory:"
            if str(self.settings.db_path) == ":memory:"
            else str(Path(self.settings.db_path).resolve())
        )
        connection = sqlite3.connect(path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _business_date(self) -> date:
        return self._now().astimezone(ZoneInfo(self.settings.timezone)).date()

    def _provider(self, provider_id: str) -> MarketDataProvider:
        provider = self._provider_factory(provider_id)
        if provider.provider_id == AKSHARE_PROVIDER_ID and not self.settings.market_akshare_enabled:
            raise LedgerError(
                "PROVIDER_DISABLED",
                "AKShare market data provider is disabled",
                http_status=503,
            )
        return provider

    def _run_canary_bounded(
        self, provider: MarketDataProvider, instrument_code: str, as_of: date
    ) -> CanaryResult:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(provider.canary, instrument_code, as_of)
        try:
            return future.result(timeout=self.settings.market_provider_timeout_seconds + 5)
        except FutureTimeoutError:
            future.cancel()
            return CanaryResult(
                provider_id=provider.provider_id,
                source_name=provider.source_name,
                source_type=provider.source_type,
                library_version="unknown",
                contract_version=provider.contract_version,
                status="FAIL",
                checked_at=self._now(),
                details={"instrument_code": instrument_code},
                error_code="PROVIDER_TIMEOUT",
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _store_canary(self, result: CanaryResult) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_data_source_health (
                    provider_id, source_name, source_type, library_version,
                    contract_version, canary_status, checked_at,
                    last_error_code, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    source_name=excluded.source_name,
                    source_type=excluded.source_type,
                    library_version=excluded.library_version,
                    contract_version=excluded.contract_version,
                    canary_status=excluded.canary_status,
                    checked_at=excluded.checked_at,
                    last_error_code=excluded.last_error_code,
                    details_json=excluded.details_json
                """,
                (
                    result.provider_id,
                    result.source_name,
                    result.source_type,
                    result.library_version,
                    result.contract_version,
                    result.status,
                    _iso(result.checked_at),
                    result.error_code,
                    json.dumps(result.details, ensure_ascii=False, sort_keys=True),
                ),
            )

    @staticmethod
    def _canary_data(result: CanaryResult) -> JsonDict:
        return {
            "provider_id": result.provider_id,
            "source_name": result.source_name,
            "source_type": result.source_type,
            "library_version": result.library_version,
            "contract_version": result.contract_version,
            "status": result.status,
            "checked_at": _iso(result.checked_at),
            "error_code": result.error_code,
            "details": result.details,
        }

    def run_canary(
        self,
        *,
        provider_id: str = AKSHARE_PROVIDER_ID,
        instrument_code: str | None = None,
        as_of_date_value: str | None = None,
    ) -> JsonDict:
        as_of = self._parse_as_of(as_of_date_value)
        code = (instrument_code or self.settings.market_provider_canary_code).strip().upper()
        provider = self._provider(provider_id)
        result = self._run_canary_bounded(provider, code, as_of)
        self._store_canary(result)
        return self._canary_data(result)

    def _parse_as_of(self, value: str | None) -> date:
        if value is None:
            return self._business_date()
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise LedgerError("INVALID_DATE", "as_of_date must be an ISO date") from exc
        if parsed > self._business_date():
            raise LedgerError("FUTURE_DATE", "as_of_date cannot be in the future")
        return parsed

    def _validate_instruments(self, codes: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(code.strip().upper() for code in codes if code.strip()))
        if not normalized:
            raise LedgerError("INSTRUMENTS_REQUIRED", "at least one instrument code is required")
        if len(normalized) > 100:
            raise LedgerError("TOO_MANY_INSTRUMENTS", "at most 100 instruments may be synced")
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT code, asset_type FROM instruments WHERE code IN ({placeholders}) "
                "AND status='ACTIVE'",
                normalized,
            ).fetchall()
        found = {str(row["code"]): str(row["asset_type"]) for row in rows}
        missing = [code for code in normalized if code not in found]
        unsupported = [code for code, asset_type in found.items() if asset_type != "FUND"]
        if missing:
            raise LedgerError(
                "INSTRUMENT_NOT_FOUND",
                "one or more active instruments were not found",
                details={"instrument_codes": missing},
                http_status=404,
            )
        if unsupported:
            raise LedgerError(
                "PROVIDER_ASSET_UNSUPPORTED",
                "AKShare open-fund provider only supports FUND instruments",
                details={"instrument_codes": unsupported},
            )
        return normalized

    def sync_navs(
        self,
        *,
        instrument_codes: list[str],
        provider_id: str = AKSHARE_PROVIDER_ID,
        as_of_date_value: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        as_of = self._parse_as_of(as_of_date_value)
        codes = self._validate_instruments(instrument_codes)
        provider = self._provider(provider_id)
        started_at = self._now()
        canary = self._run_canary_bounded(
            provider, self.settings.market_provider_canary_code, as_of
        )
        self._store_canary(canary)
        if canary.status != "PASS":
            raise LedgerError(
                "PROVIDER_CANARY_FAILED",
                "market data provider canary did not pass",
                details=self._canary_data(canary),
                http_status=503,
            )

        items: list[JsonDict] = []
        warnings: list[str] = []
        succeeded = 0
        worker_count = min(3, len(codes))
        executor = ThreadPoolExecutor(max_workers=worker_count)
        futures = {code: executor.submit(provider.fetch_nav, code, as_of) for code in codes}
        for code in codes:
            try:
                observation = futures[code].result(
                    timeout=self.settings.market_provider_timeout_seconds + 5
                )
                stored = self._market_data.record_nav_snapshot(
                    instrument_code=observation.instrument_code,
                    nav_date_value=observation.nav_date.isoformat(),
                    nav=str(observation.nav),
                    source_type=observation.source_type,
                    source_name=observation.source_name,
                    source_ref=observation.source_ref,
                    verification_status="UNVERIFIED",
                    observed_at_value=observation.observed_at.isoformat(),
                    actor_ref=actor_ref,
                )
                succeeded += 1
                items.append(
                    {
                        "instrument_code": code,
                        "status": "PASS",
                        "nav_date": observation.nav_date.isoformat(),
                        "nav": str(observation.nav),
                        "raw_hash": observation.raw_hash,
                        "timings_ms": observation.timings_ms,
                        "snapshot": stored["snapshot"],
                        "created": stored["created"],
                    }
                )
            except FutureTimeoutError:
                futures[code].cancel()
                warnings.append(f"{code}: PROVIDER_TIMEOUT")
                items.append(
                    {
                        "instrument_code": code,
                        "status": "FAIL",
                        "error": {
                            "code": "PROVIDER_TIMEOUT",
                            "message": "market data provider request timed out",
                        },
                    }
                )
            except LedgerError as exc:
                warnings.append(f"{code}: {exc.code}")
                items.append(
                    {
                        "instrument_code": code,
                        "status": "FAIL",
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
            except Exception as exc:
                warnings.append(f"{code}: PROVIDER_FETCH_FAILED")
                items.append(
                    {
                        "instrument_code": code,
                        "status": "FAIL",
                        "error": {
                            "code": "PROVIDER_FETCH_FAILED",
                            "message": "market data provider request failed",
                            "error_type": type(exc).__name__,
                        },
                    }
                )
        executor.shutdown(wait=False, cancel_futures=True)

        failed = len(codes) - succeeded
        status = "PASS" if failed == 0 else ("PARTIAL" if succeeded else "FAIL")
        completed_at = self._now()
        run_id = str(uuid4())
        run_details = {
            "items": items,
            "warnings": warnings,
            "single_source": True,
            "requires_secondary_validation": True,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_sync_runs (
                    id, provider_id, requested_as_of, started_at, completed_at,
                    status, requested_count, succeeded_count, failed_count, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    provider.provider_id,
                    as_of.isoformat(),
                    _iso(started_at),
                    _iso(completed_at),
                    status,
                    len(codes),
                    succeeded,
                    failed,
                    json.dumps(run_details, ensure_ascii=False, sort_keys=True),
                ),
            )
        return {
            "run_id": run_id,
            "provider_id": provider.provider_id,
            "as_of_date": as_of.isoformat(),
            "status": status,
            "requested_count": len(codes),
            "succeeded_count": succeeded,
            "failed_count": failed,
            "items": items,
            "data_quality": "WARNING" if status == "PASS" else "SOURCE_ERROR",
            "warnings": list(
                dict.fromkeys(
                    [
                        "Market NAV sync currently uses one aggregator source; "
                        "values remain unverified",
                        *warnings,
                    ]
                )
            ),
        }

    def status(self, *, limit: int = 20) -> JsonDict:
        if limit < 1 or limit > 100:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 100")
        with self._connect() as connection:
            sources = connection.execute(
                "SELECT * FROM market_data_source_health ORDER BY checked_at DESC"
            ).fetchall()
            runs = connection.execute(
                "SELECT * FROM market_sync_runs ORDER BY completed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return {
            "sources": [self._status_row(row) for row in sources],
            "runs": [self._status_row(row) for row in runs],
        }

    @staticmethod
    def _status_row(row: sqlite3.Row) -> JsonDict:
        data = dict(row)
        details_json = data.pop("details_json")
        data["details"] = json.loads(str(details_json))
        return data
