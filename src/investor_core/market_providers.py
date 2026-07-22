"""External market-data adapters with explicit, testable contracts."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from investor_core.ledger import LedgerError

AKSHARE_PROVIDER_ID = "AKSHARE_OPEN_FUND"
AKSHARE_CONTRACT_VERSION = "fund_open_fund_info_em.unit-nav.v1"


@dataclass(frozen=True)
class NavObservation:
    instrument_code: str
    nav_date: date
    nav: Decimal
    observed_at: datetime
    source_type: str
    source_name: str
    source_ref: str
    raw_hash: str
    library_version: str
    contract_version: str


@dataclass(frozen=True)
class CanaryResult:
    provider_id: str
    source_name: str
    source_type: str
    library_version: str
    contract_version: str
    status: str
    checked_at: datetime
    details: dict[str, Any]
    error_code: str | None = None


class MarketDataProvider(Protocol):
    provider_id: str
    source_name: str
    source_type: str
    contract_version: str

    def canary(self, instrument_code: str, as_of: date) -> CanaryResult: ...

    def fetch_nav(self, instrument_code: str, as_of: date) -> NavObservation: ...


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AkshareOpenFundProvider:
    """AKShare adapter for open-fund confirmed unit NAV history.

    The adapter deliberately imports AKShare lazily so Core health and the local
    ledger remain usable when the optional external dependency or network fails.
    """

    provider_id = AKSHARE_PROVIDER_ID
    source_name = "AKShare / Eastmoney open-fund NAV"
    source_type = "AGGREGATOR"
    contract_version = AKSHARE_CONTRACT_VERSION
    required_columns = frozenset({"净值日期", "单位净值"})

    def _module(self) -> Any:
        try:
            return importlib.import_module("akshare")
        except ImportError as exc:
            raise LedgerError(
                "PROVIDER_UNAVAILABLE",
                "AKShare is not installed in the locked runtime",
                http_status=503,
            ) from exc

    @staticmethod
    def _library_version() -> str:
        try:
            return importlib.metadata.version("akshare")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    def _fetch_frame(self, instrument_code: str) -> Any:
        module = self._module()
        fetch = getattr(module, "fund_open_fund_info_em", None)
        if not callable(fetch):
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "AKShare fund_open_fund_info_em is unavailable",
                http_status=503,
            )
        try:
            return fetch(symbol=instrument_code, indicator="单位净值走势")
        except Exception as exc:
            raise LedgerError(
                "PROVIDER_FETCH_FAILED",
                "AKShare open-fund NAV request failed",
                details={"instrument_code": instrument_code, "error_type": type(exc).__name__},
                http_status=503,
            ) from exc

    @staticmethod
    def _parse_date(value: Any) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError as exc:
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "AKShare returned an invalid NAV date",
                http_status=503,
            ) from exc

    @staticmethod
    def _parse_nav(value: Any) -> Decimal:
        try:
            nav = Decimal(str(value))
        except InvalidOperation as exc:
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "AKShare returned an invalid unit NAV",
                http_status=503,
            ) from exc
        if not nav.is_finite() or nav <= 0:
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "AKShare returned a non-positive unit NAV",
                http_status=503,
            )
        return nav

    def fetch_nav(self, instrument_code: str, as_of: date) -> NavObservation:
        normalized_code = instrument_code.strip().upper()
        frame = self._fetch_frame(normalized_code)
        columns = {str(column) for column in getattr(frame, "columns", [])}
        if not self.required_columns.issubset(columns):
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "AKShare open-fund NAV columns changed",
                details={"expected": sorted(self.required_columns), "actual": sorted(columns)},
                http_status=503,
            )

        eligible: list[tuple[date, Decimal]] = []
        for _, row in frame.iterrows():
            nav_date = self._parse_date(row["净值日期"])
            if nav_date <= as_of:
                eligible.append((nav_date, self._parse_nav(row["单位净值"])))
        if not eligible:
            raise LedgerError(
                "MARKET_DATA_NOT_FOUND",
                "no eligible confirmed unit NAV was returned",
                details={"instrument_code": normalized_code, "as_of_date": as_of.isoformat()},
                http_status=404,
            )
        nav_date, nav = max(eligible, key=lambda item: item[0])
        observed_at = datetime.now(UTC)
        raw_payload = {
            "instrument_code": normalized_code,
            "nav_date": nav_date.isoformat(),
            "nav": str(nav),
            "provider_id": self.provider_id,
            "contract_version": self.contract_version,
        }
        return NavObservation(
            instrument_code=normalized_code,
            nav_date=nav_date,
            nav=nav,
            observed_at=observed_at,
            source_type=self.source_type,
            source_name=self.source_name,
            source_ref=(f"akshare://fund_open_fund_info_em/{normalized_code}?indicator=unit-nav"),
            raw_hash=_canonical_hash(raw_payload),
            library_version=self._library_version(),
            contract_version=self.contract_version,
        )

    def canary(self, instrument_code: str, as_of: date) -> CanaryResult:
        checked_at = datetime.now(UTC)
        try:
            observation = self.fetch_nav(instrument_code, as_of)
        except LedgerError as exc:
            return CanaryResult(
                provider_id=self.provider_id,
                source_name=self.source_name,
                source_type=self.source_type,
                library_version=self._library_version(),
                contract_version=self.contract_version,
                status="FAIL",
                checked_at=checked_at,
                details={"instrument_code": instrument_code, "message": exc.message},
                error_code=exc.code,
            )
        return CanaryResult(
            provider_id=self.provider_id,
            source_name=self.source_name,
            source_type=self.source_type,
            library_version=observation.library_version,
            contract_version=self.contract_version,
            status="PASS",
            checked_at=checked_at,
            details={
                "instrument_code": observation.instrument_code,
                "latest_nav_date": observation.nav_date.isoformat(),
                "raw_hash": observation.raw_hash,
            },
        )


def build_provider(provider_id: str) -> MarketDataProvider:
    normalized = provider_id.strip().upper()
    if normalized == AKSHARE_PROVIDER_ID:
        return AkshareOpenFundProvider()
    raise LedgerError(
        "PROVIDER_NOT_FOUND",
        "market data provider is not registered",
        details={"provider_id": normalized},
        http_status=404,
    )
