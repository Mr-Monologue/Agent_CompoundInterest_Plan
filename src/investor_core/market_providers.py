"""External market-data adapters with explicit, testable contracts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Protocol
from zoneinfo import ZoneInfo

import httpx

from investor_core.ledger import LedgerError

AKSHARE_PROVIDER_ID = "AKSHARE_OPEN_FUND"
AKSHARE_CONTRACT_VERSION = "eastmoney.pingzhongdata.unit-nav.v2"
MAX_PROVIDER_RESPONSE_BYTES = 5 * 1024 * 1024
_NAV_TREND_PATTERN = re.compile(
    r"(?:var\s+)?Data_netWorthTrend\s*=\s*(\[.*?\])\s*;",
    re.DOTALL,
)
_OBJECT_PATTERN = re.compile(r"\{([^{}]*)\}")
_TIMESTAMP_PATTERN = re.compile(r"""(?:^|,)\s*["']?x["']?\s*:\s*(\d+)""")
_NAV_PATTERN = re.compile(r"""(?:^|,)\s*["']?y["']?\s*:\s*(-?\d+(?:\.\d+)?)""")


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
    timings_ms: dict[str, int] | None = None


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
    """AKShare-compatible adapter for Eastmoney confirmed open-fund NAV history.

    AKShare's public function evaluates the provider's entire JavaScript payload.
    That path can exceed the Windows service budget and its underlying request has
    no timeout. This adapter preserves the provider identity and pinned contract,
    but downloads the same public payload with a real timeout and parses only the
    flat NAV series without executing remote JavaScript.
    """

    provider_id = AKSHARE_PROVIDER_ID
    source_name = "Eastmoney open-fund NAV (AKShare-compatible)"
    source_type = "AGGREGATOR"
    contract_version = AKSHARE_CONTRACT_VERSION
    _headers: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Referer": "https://fund.eastmoney.com/",
    }

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    @staticmethod
    def _library_version() -> str:
        try:
            return importlib.metadata.version("akshare")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    def _download_payload(self, instrument_code: str) -> tuple[str, int]:
        url = f"https://fund.eastmoney.com/pingzhongdata/{instrument_code}.js"
        started = time.perf_counter()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                headers=self._headers,
                transport=self._transport,
                trust_env=self._transport is None,
            ) as client:
                response = client.get(url)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LedgerError(
                "PROVIDER_TIMEOUT",
                "Eastmoney open-fund NAV request timed out",
                details={"instrument_code": instrument_code},
                http_status=503,
            ) from exc
        except httpx.HTTPError as exc:
            raise LedgerError(
                "PROVIDER_FETCH_FAILED",
                "Eastmoney open-fund NAV request failed",
                details={"instrument_code": instrument_code, "error_type": type(exc).__name__},
                http_status=503,
            ) from exc

        payload = response.content
        if len(payload) > MAX_PROVIDER_RESPONSE_BYTES:
            raise LedgerError(
                "PROVIDER_RESPONSE_TOO_LARGE",
                "Eastmoney open-fund NAV response exceeded the safety limit",
                details={"instrument_code": instrument_code, "response_bytes": len(payload)},
                http_status=503,
            )
        return response.text, round((time.perf_counter() - started) * 1000)

    @staticmethod
    def _parse_nav(value: Any) -> Decimal:
        try:
            nav = Decimal(str(value))
        except InvalidOperation as exc:
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "Eastmoney returned an invalid unit NAV",
                http_status=503,
            ) from exc
        if not nav.is_finite() or nav <= 0:
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "Eastmoney returned a non-positive unit NAV",
                http_status=503,
            )
        return nav

    def fetch_nav(self, instrument_code: str, as_of: date) -> NavObservation:
        normalized_code = instrument_code.strip().upper()
        payload, download_ms = self._download_payload(normalized_code)
        parse_started = time.perf_counter()
        eligible = [
            item for item in self._parse_payload(payload, normalized_code) if item[0] <= as_of
        ]
        parse_ms = round((time.perf_counter() - parse_started) * 1000)
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
            source_ref=(f"https://fund.eastmoney.com/pingzhongdata/{normalized_code}.js"),
            raw_hash=_canonical_hash(raw_payload),
            library_version=self._library_version(),
            contract_version=self.contract_version,
            timings_ms={"download": download_ms, "parse": parse_ms},
        )

    @staticmethod
    def _parse_payload(payload: str, instrument_code: str) -> list[tuple[date, Decimal]]:
        trend = _NAV_TREND_PATTERN.search(payload)
        if trend is None:
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "Eastmoney open-fund NAV series is unavailable",
                details={"instrument_code": instrument_code},
                http_status=503,
            )

        observations: list[tuple[date, Decimal]] = []
        timezone = ZoneInfo("Asia/Shanghai")
        for item in _OBJECT_PATTERN.finditer(trend.group(1)):
            fields = item.group(1)
            timestamp_match = _TIMESTAMP_PATTERN.search(fields)
            nav_match = _NAV_PATTERN.search(fields)
            if timestamp_match is None or nav_match is None:
                continue
            timestamp_ms = int(timestamp_match.group(1))
            nav_date = (
                datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).astimezone(timezone).date()
            )
            nav = AkshareOpenFundProvider._parse_nav(nav_match.group(1))
            observations.append((nav_date, nav))
        if not observations:
            raise LedgerError(
                "PROVIDER_CONTRACT_MISMATCH",
                "Eastmoney open-fund NAV series contained no parseable observations",
                details={"instrument_code": instrument_code},
                http_status=503,
            )
        return observations

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
                "timings_ms": observation.timings_ms,
            },
        )


def build_provider(provider_id: str, *, timeout_seconds: float = 60.0) -> MarketDataProvider:
    normalized = provider_id.strip().upper()
    if normalized == AKSHARE_PROVIDER_ID:
        return AkshareOpenFundProvider(timeout_seconds=timeout_seconds)
    raise LedgerError(
        "PROVIDER_NOT_FOUND",
        "market data provider is not registered",
        details={"provider_id": normalized},
        http_status=404,
    )
