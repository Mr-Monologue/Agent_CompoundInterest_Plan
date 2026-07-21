"""Deterministic Phase 1 portfolio ledger and confirmation workflow."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from investor_core.config import Settings

JsonDict = dict[str, Any]
INVESTMENT_CONTEXT_KEY = "investment_context"


class LedgerError(Exception):
    """A stable domain error safe to expose through the local API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        details: JsonDict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical_hash(payload: JsonDict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _scaled_decimal(value: str, scale: int, field_name: str) -> int:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerError("INVALID_DECIMAL", f"{field_name} must be a decimal value") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise LedgerError("INVALID_DECIMAL", f"{field_name} must be greater than zero")
    scaled = parsed * scale
    integral = scaled.to_integral_value(rounding=ROUND_HALF_UP)
    if scaled != integral:
        decimal_places = len(str(scale)) - 1
        raise LedgerError(
            "DECIMAL_PRECISION_EXCEEDED",
            f"{field_name} supports at most {decimal_places} decimal places",
        )
    return int(integral)


def _format_scaled(value: int, scale: int, decimal_places: int) -> str:
    return f"{Decimal(value) / Decimal(scale):.{decimal_places}f}"


def _round_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return (numerator + denominator // 2) // denominator


class LedgerService:
    """Own all state transitions for the local investment record."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self._now = now

    def _connect(self) -> sqlite3.Connection:
        if str(self.settings.db_path) == ":memory:":
            database_path = ":memory:"
        else:
            resolved = Path(self.settings.db_path).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            database_path = str(resolved)
        connection = sqlite3.connect(database_path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _business_date(self) -> date:
        try:
            timezone = ZoneInfo(self.settings.timezone)
        except ZoneInfoNotFoundError as exc:
            raise LedgerError(
                "INVALID_TIMEZONE",
                "configured business timezone is not available",
                details={"timezone": self.settings.timezone},
            ) from exc
        return self._now().astimezone(timezone).date()

    @staticmethod
    def _require_row(row: sqlite3.Row | None, code: str, message: str) -> sqlite3.Row:
        if row is None:
            raise LedgerError(code, message, http_status=404)
        return row

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _rollback_and_raise(connection: sqlite3.Connection, error: LedgerError) -> NoReturn:
        connection.rollback()
        raise error

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        actor_type: str,
        actor_ref: str,
        action: str,
        entity_type: str,
        entity_id: str,
        details: JsonDict,
        before_hash: str | None = None,
        after_hash: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_ref, action, entity_type, entity_id,
                before_hash, after_hash, details_json, trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                _iso(self._now()),
                actor_type,
                actor_ref,
                action,
                entity_type,
                entity_id,
                before_hash,
                after_hash,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                str(uuid4()),
            ),
        )

    @staticmethod
    def _context_payload(
        portfolio: sqlite3.Row, account: sqlite3.Row, *, source: str
    ) -> JsonDict:
        return {
            "portfolio": {
                "id": str(portfolio["id"]),
                "name": str(portfolio["name"]),
                "base_currency": str(portfolio["base_currency"]),
            },
            "account": {
                "id": str(account["id"]),
                "name": str(account["name"]),
                "platform": str(account["platform"]),
                "currency": str(account["currency"]),
            },
            "source": source,
            "user_action_required": False,
        }

    def _save_investment_context(
        self,
        connection: sqlite3.Connection,
        *,
        portfolio: sqlite3.Row,
        account: sqlite3.Row,
        actor_ref: str,
        actor_type: str,
    ) -> None:
        payload = {
            "portfolio_id": str(portfolio["id"]),
            "account_id": str(account["id"]),
        }
        next_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM settings WHERE key = ?",
                (INVESTMENT_CONTEXT_KEY,),
            ).fetchone()[0]
        )
        timestamp = _iso(self._now())
        connection.execute(
            "UPDATE settings SET status = 'RETIRED' WHERE key = ? AND status = 'ACTIVE'",
            (INVESTMENT_CONTEXT_KEY,),
        )
        connection.execute(
            """
            INSERT INTO settings (
                key, version, value_json, value_hash, status, approved_by,
                approved_at, created_at
            ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (
                INVESTMENT_CONTEXT_KEY,
                next_version,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                _canonical_hash(payload),
                actor_ref,
                timestamp,
                timestamp,
            ),
        )
        self._audit(
            connection,
            actor_type=actor_type,
            actor_ref=actor_ref,
            action="INVESTMENT_CONTEXT_SET",
            entity_type="setting",
            entity_id=INVESTMENT_CONTEXT_KEY,
            details={
                "portfolio_id": payload["portfolio_id"],
                "account_id": payload["account_id"],
                "version": next_version,
            },
            after_hash=_canonical_hash(payload),
        )

    def get_investment_context(self) -> JsonDict:
        """Return a saved context or persist an unambiguous single active context."""
        connection = self._connect()
        try:
            self._begin(connection)
            setting = connection.execute(
                """
                SELECT value_json
                FROM settings
                WHERE key = ? AND status = 'ACTIVE'
                ORDER BY version DESC
                LIMIT 1
                """,
                (INVESTMENT_CONTEXT_KEY,),
            ).fetchone()
            if setting is not None:
                try:
                    saved = json.loads(str(setting["value_json"]))
                    saved_portfolio_id = str(saved["portfolio_id"])
                    saved_account_id = str(saved["account_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "INVALID_INVESTMENT_CONTEXT",
                            "saved investment context is invalid",
                            http_status=409,
                        ),
                    )
                portfolio = connection.execute(
                    "SELECT * FROM portfolios WHERE id = ? AND status = 'ACTIVE'",
                    (saved_portfolio_id,),
                ).fetchone()
                account = connection.execute(
                    """
                    SELECT * FROM accounts
                    WHERE id = ? AND portfolio_id = ? AND status = 'ACTIVE'
                    """,
                    (saved_account_id, saved_portfolio_id),
                ).fetchone()
                if portfolio is None or account is None:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "INVALID_INVESTMENT_CONTEXT",
                            "saved portfolio or account is no longer active",
                            http_status=409,
                        ),
                    )
                connection.commit()
                return self._context_payload(portfolio, account, source="SAVED")

            portfolios = connection.execute(
                "SELECT * FROM portfolios WHERE status = 'ACTIVE' ORDER BY created_at, name"
            ).fetchall()
            if len(portfolios) != 1:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVESTMENT_CONTEXT_REQUIRED",
                        "a default portfolio must be selected",
                        http_status=409,
                        details={
                            "portfolio_candidates": [
                                {"id": str(row["id"]), "name": str(row["name"])}
                                for row in portfolios
                            ]
                        },
                    ),
                )
            portfolio = portfolios[0]
            accounts = connection.execute(
                """
                SELECT * FROM accounts
                WHERE portfolio_id = ? AND status = 'ACTIVE'
                ORDER BY created_at, name
                """,
                (portfolio["id"],),
            ).fetchall()
            if len(accounts) != 1:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVESTMENT_CONTEXT_REQUIRED",
                        "a default account must be selected",
                        http_status=409,
                        details={
                            "portfolio": {
                                "id": str(portfolio["id"]),
                                "name": str(portfolio["name"]),
                            },
                            "account_candidates": [
                                {
                                    "id": str(row["id"]),
                                    "name": str(row["name"]),
                                    "platform": str(row["platform"]),
                                }
                                for row in accounts
                            ],
                        },
                    ),
                )
            account = accounts[0]
            self._save_investment_context(
                connection,
                portfolio=portfolio,
                account=account,
                actor_ref="system:auto-singleton",
                actor_type="SYSTEM",
            )
            connection.commit()
            return self._context_payload(portfolio, account, source="AUTO_SELECTED")
        finally:
            connection.close()

    def set_investment_context(
        self, *, portfolio_id: str, account_id: str, actor_ref: str = "local-user"
    ) -> JsonDict:
        """Persist an explicit default portfolio and account without changing holdings."""
        connection = self._connect()
        try:
            self._begin(connection)
            portfolio = self._require_row(
                connection.execute(
                    "SELECT * FROM portfolios WHERE id = ? AND status = 'ACTIVE'",
                    (portfolio_id,),
                ).fetchone(),
                "PORTFOLIO_NOT_FOUND",
                "active portfolio was not found",
            )
            account = self._require_row(
                connection.execute(
                    """
                    SELECT * FROM accounts
                    WHERE id = ? AND portfolio_id = ? AND status = 'ACTIVE'
                    """,
                    (account_id, portfolio_id),
                ).fetchone(),
                "ACCOUNT_NOT_FOUND",
                "active account was not found in the selected portfolio",
            )
            self._save_investment_context(
                connection,
                portfolio=portfolio,
                account=account,
                actor_ref=actor_ref,
                actor_type="AGENT" if actor_ref == "hermes" else "USER",
            )
            connection.commit()
            return self._context_payload(portfolio, account, source="SAVED")
        finally:
            connection.close()

    def create_portfolio(
        self,
        *,
        name: str,
        base_currency: str = "CNY",
        actor_ref: str = "local-user",
    ) -> JsonDict:
        normalized_name = name.strip()
        if not normalized_name:
            raise LedgerError("INVALID_NAME", "portfolio name is required")
        currency = base_currency.strip().upper()
        connection = self._connect()
        try:
            self._begin(connection)
            existing = connection.execute(
                "SELECT * FROM portfolios WHERE name = ?", (normalized_name,)
            ).fetchone()
            if existing is not None:
                if existing["base_currency"] != currency:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "SETUP_CONFLICT",
                            "portfolio already exists with a different base currency",
                            http_status=409,
                        ),
                    )
                connection.commit()
                return {**dict(existing), "created": False}

            portfolio_id = str(uuid4())
            created_at = _iso(self._now())
            connection.execute(
                """
                INSERT INTO portfolios (id, name, base_currency, status, created_at)
                VALUES (?, ?, ?, 'ACTIVE', ?)
                """,
                (portfolio_id, normalized_name, currency, created_at),
            )
            self._audit(
                connection,
                actor_type="CLI",
                actor_ref=actor_ref,
                action="PORTFOLIO_CREATED",
                entity_type="portfolio",
                entity_id=portfolio_id,
                details={"name": normalized_name, "base_currency": currency},
            )
            connection.commit()
            return {
                "id": portfolio_id,
                "name": normalized_name,
                "base_currency": currency,
                "status": "ACTIVE",
                "created_at": created_at,
                "created": True,
            }
        finally:
            connection.close()

    def create_account(
        self,
        *,
        portfolio_id: str,
        name: str,
        platform: str,
        currency: str = "CNY",
        actor_ref: str = "local-user",
    ) -> JsonDict:
        normalized_name = name.strip()
        normalized_platform = platform.strip()
        if not normalized_name or not normalized_platform:
            raise LedgerError("INVALID_ACCOUNT", "account name and platform are required")
        normalized_currency = currency.strip().upper()
        connection = self._connect()
        try:
            self._begin(connection)
            self._require_row(
                connection.execute(
                    "SELECT id FROM portfolios WHERE id = ? AND status = 'ACTIVE'",
                    (portfolio_id,),
                ).fetchone(),
                "PORTFOLIO_NOT_FOUND",
                "active portfolio was not found",
            )
            existing = connection.execute(
                "SELECT * FROM accounts WHERE portfolio_id = ? AND name = ?",
                (portfolio_id, normalized_name),
            ).fetchone()
            if existing is not None:
                if (
                    existing["platform"] != normalized_platform
                    or existing["currency"] != normalized_currency
                ):
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "SETUP_CONFLICT",
                            "account already exists with different attributes",
                            http_status=409,
                        ),
                    )
                connection.commit()
                return {**dict(existing), "created": False}

            account_id = str(uuid4())
            created_at = _iso(self._now())
            connection.execute(
                """
                INSERT INTO accounts (
                    id, portfolio_id, name, platform, currency, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    account_id,
                    portfolio_id,
                    normalized_name,
                    normalized_platform,
                    normalized_currency,
                    created_at,
                ),
            )
            self._audit(
                connection,
                actor_type="CLI",
                actor_ref=actor_ref,
                action="ACCOUNT_CREATED",
                entity_type="account",
                entity_id=account_id,
                details={"portfolio_id": portfolio_id, "platform": normalized_platform},
            )
            connection.commit()
            return {
                "id": account_id,
                "portfolio_id": portfolio_id,
                "name": normalized_name,
                "platform": normalized_platform,
                "currency": normalized_currency,
                "status": "ACTIVE",
                "created_at": created_at,
                "created": True,
            }
        finally:
            connection.close()

    def create_instrument(
        self,
        *,
        code: str,
        name: str,
        asset_type: str = "FUND",
        currency: str = "CNY",
        role: str = "UNASSIGNED",
        actor_ref: str = "local-user",
    ) -> JsonDict:
        normalized_code = code.strip().upper()
        normalized_name = name.strip()
        normalized_type = asset_type.strip().upper()
        normalized_currency = currency.strip().upper()
        normalized_role = role.strip().upper()
        if not normalized_code or not normalized_name:
            raise LedgerError("INVALID_INSTRUMENT", "instrument code and name are required")
        if normalized_type not in {"FUND", "ETF", "STOCK", "INDEX", "CASH"}:
            raise LedgerError("INVALID_ASSET_TYPE", "unsupported asset type")
        if normalized_role not in {"CORE", "SATELLITE", "UNASSIGNED"}:
            raise LedgerError("INVALID_ROLE", "unsupported portfolio role")

        connection = self._connect()
        try:
            self._begin(connection)
            existing = connection.execute(
                "SELECT * FROM instruments WHERE code = ?", (normalized_code,)
            ).fetchone()
            if existing is not None:
                expected = (normalized_name, normalized_type, normalized_currency, normalized_role)
                actual = (
                    existing["name"],
                    existing["asset_type"],
                    existing["currency"],
                    existing["role"],
                )
                if actual != expected:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "SETUP_CONFLICT",
                            "instrument already exists with different attributes",
                            http_status=409,
                        ),
                    )
                connection.commit()
                return {**dict(existing), "created": False}

            instrument_id = str(uuid4())
            created_at = _iso(self._now())
            connection.execute(
                """
                INSERT INTO instruments (
                    id, code, name, asset_type, currency, role, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    instrument_id,
                    normalized_code,
                    normalized_name,
                    normalized_type,
                    normalized_currency,
                    normalized_role,
                    created_at,
                ),
            )
            self._audit(
                connection,
                actor_type="CLI",
                actor_ref=actor_ref,
                action="INSTRUMENT_CREATED",
                entity_type="instrument",
                entity_id=instrument_id,
                details={
                    "code": normalized_code,
                    "asset_type": normalized_type,
                    "role": normalized_role,
                },
            )
            connection.commit()
            return {
                "id": instrument_id,
                "code": normalized_code,
                "name": normalized_name,
                "asset_type": normalized_type,
                "currency": normalized_currency,
                "role": normalized_role,
                "status": "ACTIVE",
                "created_at": created_at,
                "created": True,
            }
        finally:
            connection.close()

    def list_portfolios(self) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM portfolios ORDER BY created_at, name"
            ).fetchall()
            return [dict(row) for row in rows]

    def list_accounts(self, portfolio_id: str | None = None) -> list[JsonDict]:
        with self._connect() as connection:
            if portfolio_id:
                rows = connection.execute(
                    "SELECT * FROM accounts WHERE portfolio_id = ? ORDER BY created_at, name",
                    (portfolio_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM accounts ORDER BY created_at, name"
                ).fetchall()
            return [dict(row) for row in rows]

    def list_instruments(self) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM instruments ORDER BY code"
            ).fetchall()
            return [dict(row) for row in rows]

    def _instrument_by_code(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        return self._require_row(
            connection.execute(
                "SELECT * FROM instruments WHERE code = ? AND status = 'ACTIVE'",
                (code.strip().upper(),),
            ).fetchone(),
            "INSTRUMENT_NOT_FOUND",
            "active instrument was not found",
        )

    def _validate_account(
        self, connection: sqlite3.Connection, portfolio_id: str, account_id: str
    ) -> sqlite3.Row:
        self._require_row(
            connection.execute(
                "SELECT id FROM portfolios WHERE id = ? AND status = 'ACTIVE'",
                (portfolio_id,),
            ).fetchone(),
            "PORTFOLIO_NOT_FOUND",
            "active portfolio was not found",
        )
        return self._require_row(
            connection.execute(
                """
                SELECT * FROM accounts
                WHERE id = ? AND portfolio_id = ? AND status = 'ACTIVE'
                """,
                (account_id, portfolio_id),
            ).fetchone(),
            "ACCOUNT_NOT_FOUND",
            "active account was not found in the portfolio",
        )

    def _validate_amount_consistency(
        self, amount_minor: int, nav_micros: int, shares_micros: int
    ) -> None:
        expected_minor = _round_div(nav_micros * shares_micros, 10_000_000_000)
        proportional_tolerance = (
            amount_minor * self.settings.transaction_amount_tolerance_bps + 9_999
        ) // 10_000
        allowed = max(
            self.settings.transaction_amount_tolerance_minor,
            proportional_tolerance,
        )
        difference = abs(amount_minor - expected_minor)
        if difference > allowed:
            raise LedgerError(
                "AMOUNT_SHARE_MISMATCH",
                "amount, NAV and shares differ beyond the configured tolerance",
                details={
                    "amount": _format_scaled(amount_minor, 100, 2),
                    "expected_amount": _format_scaled(expected_minor, 100, 2),
                    "difference": _format_scaled(difference, 100, 2),
                    "allowed_difference": _format_scaled(allowed, 100, 2),
                },
            )

    def _position(
        self,
        connection: sqlite3.Connection,
        *,
        portfolio_id: str,
        account_id: str,
        instrument_id: str,
        exclude_transaction_id: str | None = None,
    ) -> tuple[int, int]:
        query = """
            SELECT id, side, amount_minor, shares_micros
            FROM transactions
            WHERE portfolio_id = ? AND account_id = ? AND instrument_id = ?
              AND kind IN ('TRADE','OPENING') AND reversed_by_transaction_id IS NULL
        """
        parameters: list[Any] = [portfolio_id, account_id, instrument_id]
        if exclude_transaction_id:
            query += " AND id <> ?"
            parameters.append(exclude_transaction_id)
        query += " ORDER BY trade_date, committed_at, id"

        shares = 0
        cost = 0
        for row in connection.execute(query, parameters).fetchall():
            row_shares = int(row["shares_micros"])
            if row["side"] == "BUY":
                shares += row_shares
                cost += int(row["amount_minor"])
                continue
            if row_shares > shares:
                raise LedgerError(
                    "INSUFFICIENT_SHARES",
                    "ledger reconstruction would produce negative shares",
                    http_status=409,
                    details={
                        "transaction_id": row["id"],
                        "available_shares": _format_scaled(shares, 1_000_000, 6),
                        "requested_shares": _format_scaled(row_shares, 1_000_000, 6),
                    },
                )
            cost_reduction = _round_div(cost * row_shares, shares)
            shares -= row_shares
            cost -= cost_reduction
            if shares == 0:
                cost = 0
        return shares, cost

    def _draft_row(self, connection: sqlite3.Connection, draft_id: str) -> sqlite3.Row:
        return self._require_row(
            connection.execute(
                """
                SELECT d.*, i.code AS instrument_code, i.name AS instrument_name
                FROM transaction_drafts d
                JOIN instruments i ON i.id = d.instrument_id
                WHERE d.id = ?
                """,
                (draft_id,),
            ).fetchone(),
            "DRAFT_NOT_FOUND",
            "transaction draft was not found",
        )

    @staticmethod
    def _draft_data(row: sqlite3.Row) -> JsonDict:
        result = {
            "id": row["id"],
            "portfolio_id": row["portfolio_id"],
            "account_id": row["account_id"],
            "instrument_id": row["instrument_id"],
            "instrument_code": row["instrument_code"],
            "instrument_name": row["instrument_name"],
            "action": row["action"],
            "side": row["side"],
            "trade_date": row["trade_date"],
            "amount": _format_scaled(int(row["amount_minor"]), 100, 2),
            "nav": _format_scaled(int(row["nav_micros"]), 1_000_000, 6),
            "shares": _format_scaled(int(row["shares_micros"]), 1_000_000, 6),
            "platform": row["platform"],
            "note": row["note"],
            "reversal_of_transaction_id": row["reversal_of_transaction_id"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "committed_at": row["committed_at"],
            "committed_transaction_id": row["committed_transaction_id"],
        }
        if row["action"] == "OPENING":
            result.update(
                {
                    "as_of_date": row["trade_date"],
                    "cost_amount": _format_scaled(int(row["amount_minor"]), 100, 2),
                    "average_cost_nav": _format_scaled(
                        int(row["nav_micros"]), 1_000_000, 6
                    ),
                    "total_shares": _format_scaled(
                        int(row["shares_micros"]), 1_000_000, 6
                    ),
                }
            )
        return result

    def create_transaction_draft(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        instrument_code: str,
        side: str,
        trade_date_value: str,
        amount: str,
        nav: str,
        shares: str,
        platform: str,
        idempotency_key: str,
        note: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise LedgerError("INVALID_SIDE", "side must be BUY or SELL")
        try:
            normalized_trade_date = date.fromisoformat(trade_date_value).isoformat()
        except ValueError as exc:
            raise LedgerError("INVALID_TRADE_DATE", "trade_date must use YYYY-MM-DD") from exc
        normalized_platform = platform.strip()
        normalized_key = idempotency_key.strip()
        if not normalized_platform or not normalized_key:
            raise LedgerError(
                "MISSING_REQUIRED_FIELD", "platform and idempotency_key are required"
            )

        amount_minor = _scaled_decimal(amount, 100, "amount")
        nav_micros = _scaled_decimal(nav, 1_000_000, "nav")
        shares_micros = _scaled_decimal(shares, 1_000_000, "shares")
        self._validate_amount_consistency(amount_minor, nav_micros, shares_micros)

        connection = self._connect()
        try:
            self._begin(connection)
            self._validate_account(connection, portfolio_id, account_id)
            instrument = self._instrument_by_code(connection, instrument_code)
            if instrument["asset_type"] == "INDEX":
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "NON_TRADABLE_INSTRUMENT",
                        "an index benchmark cannot be used in a transaction record",
                        http_status=409,
                        details={
                            "instrument_code": instrument["code"],
                            "asset_type": instrument["asset_type"],
                        },
                    ),
                )
            opening = connection.execute(
                """
                SELECT id, trade_date
                FROM transactions
                WHERE portfolio_id = ? AND account_id = ? AND instrument_id = ?
                  AND kind = 'OPENING' AND reversed_by_transaction_id IS NULL
                ORDER BY trade_date, committed_at LIMIT 1
                """,
                (portfolio_id, account_id, instrument["id"]),
            ).fetchone()
            if opening is not None and normalized_trade_date < opening["trade_date"]:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "TRADE_PREDATES_OPENING_POSITION",
                        "a transaction cannot predate the active opening position",
                        http_status=409,
                        details={
                            "opening_transaction_id": opening["id"],
                            "opening_as_of_date": opening["trade_date"],
                            "trade_date": normalized_trade_date,
                        },
                    ),
                )
            payload = {
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "instrument_id": instrument["id"],
                "action": "TRADE",
                "side": normalized_side,
                "trade_date": normalized_trade_date,
                "amount_minor": amount_minor,
                "nav_micros": nav_micros,
                "shares_micros": shares_micros,
                "platform": normalized_platform,
                "note": note,
            }
            request_hash = _canonical_hash(payload)
            existing = connection.execute(
                "SELECT id, request_hash FROM transaction_drafts WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used for different content",
                            http_status=409,
                        ),
                    )
                row = self._draft_row(connection, str(existing["id"]))
                connection.commit()
                return {
                    "draft": self._draft_data(row),
                    "confirmation_token": None,
                    "reused": True,
                    "warnings": [
                        "Duplicate request reused the existing draft; use its original token"
                    ],
                }

            if normalized_side == "SELL":
                available_shares, _ = self._position(
                    connection,
                    portfolio_id=portfolio_id,
                    account_id=account_id,
                    instrument_id=str(instrument["id"]),
                )
                if shares_micros > available_shares:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "INSUFFICIENT_SHARES",
                            "sell draft exceeds the currently recorded shares",
                            http_status=409,
                            details={
                                "available_shares": _format_scaled(
                                    available_shares, 1_000_000, 6
                                ),
                                "requested_shares": _format_scaled(
                                    shares_micros, 1_000_000, 6
                                ),
                            },
                        ),
                    )

            now = self._now()
            draft_id = str(uuid4())
            token = secrets.token_urlsafe(24)
            expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
            connection.execute(
                """
                INSERT INTO transaction_drafts (
                    id, portfolio_id, account_id, instrument_id, action, side, trade_date,
                    amount_minor, nav_micros, shares_micros, platform, note,
                    reversal_of_transaction_id, status, idempotency_key, request_hash,
                    confirmation_digest, expires_at, created_at, actor_ref
                ) VALUES (
                    ?, ?, ?, ?, 'TRADE', ?, ?, ?, ?, ?, ?, ?, NULL, 'PENDING',
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    draft_id,
                    portfolio_id,
                    account_id,
                    instrument["id"],
                    normalized_side,
                    normalized_trade_date,
                    amount_minor,
                    nav_micros,
                    shares_micros,
                    normalized_platform,
                    note,
                    normalized_key,
                    request_hash,
                    _token_digest(token),
                    _iso(expires_at),
                    _iso(now),
                    actor_ref,
                ),
            )
            self._audit(
                connection,
                actor_type="AGENT",
                actor_ref=actor_ref,
                action="TRANSACTION_DRAFT_CREATED",
                entity_type="transaction_draft",
                entity_id=draft_id,
                details={
                    "side": normalized_side,
                    "instrument_code": instrument["code"],
                    "idempotency_key": normalized_key,
                    "expires_at": _iso(expires_at),
                },
                after_hash=request_hash,
            )
            row = self._draft_row(connection, draft_id)
            connection.commit()
            return {
                "draft": self._draft_data(row),
                "confirmation_token": token,
                "reused": False,
                "warnings": [],
            }
        finally:
            connection.close()

    def create_opening_position_draft(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        instrument_code: str,
        as_of_date_value: str,
        total_shares: str,
        platform: str,
        idempotency_key: str,
        cost_amount: str | None = None,
        average_cost_nav: str | None = None,
        note: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Create a confirmed-import draft without pretending an old holding is a trade."""
        try:
            as_of_date = date.fromisoformat(as_of_date_value)
        except ValueError as exc:
            raise LedgerError("INVALID_AS_OF_DATE", "as_of_date must use YYYY-MM-DD") from exc
        if as_of_date > self._business_date():
            raise LedgerError("FUTURE_AS_OF_DATE", "as_of_date cannot be in the future")

        normalized_platform = platform.strip()
        normalized_key = idempotency_key.strip()
        if not normalized_platform or not normalized_key:
            raise LedgerError(
                "MISSING_REQUIRED_FIELD", "platform and idempotency_key are required"
            )

        total_shares_micros = _scaled_decimal(total_shares, 1_000_000, "total_shares")
        normalized_cost_amount = (cost_amount or "").strip()
        normalized_average_cost_nav = (average_cost_nav or "").strip()
        if bool(normalized_cost_amount) == bool(normalized_average_cost_nav):
            raise LedgerError(
                "OPENING_COST_BASIS_REQUIRED",
                "provide exactly one of cost_amount or average_cost_nav",
            )
        cost_basis_input: str
        warnings = [
            "Opening-position values are user-supplied and are not independently "
            "verified by Investor Core"
        ]
        if normalized_cost_amount:
            cost_amount_minor = _scaled_decimal(
                normalized_cost_amount, 100, "cost_amount"
            )
            average_cost_nav_micros = _round_div(
                cost_amount_minor * 10_000_000_000, total_shares_micros
            )
            cost_basis_input = "COST_AMOUNT"
        else:
            average_cost_nav_micros = _scaled_decimal(
                normalized_average_cost_nav, 1_000_000, "average_cost_nav"
            )
            cost_amount_minor = _round_div(
                average_cost_nav_micros * total_shares_micros, 10_000_000_000
            )
            cost_basis_input = "AVERAGE_COST_NAV"
            warnings.append(
                "Cost amount was derived from total shares and average cost NAV, then "
                "rounded to CNY 0.01"
            )
        if average_cost_nav_micros <= 0:
            raise LedgerError(
                "AVERAGE_COST_BELOW_PRECISION",
                "cost_amount divided by total_shares is below supported NAV precision",
            )
        if cost_amount_minor <= 0:
            raise LedgerError(
                "COST_AMOUNT_BELOW_PRECISION",
                "derived cost amount is below CNY 0.01",
            )

        connection = self._connect()
        try:
            self._begin(connection)
            self._validate_account(connection, portfolio_id, account_id)
            instrument = self._instrument_by_code(connection, instrument_code)
            if instrument["asset_type"] == "INDEX":
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "NON_TRADABLE_INSTRUMENT",
                        "an index benchmark cannot be imported as an opening position",
                        http_status=409,
                        details={
                            "instrument_code": instrument["code"],
                            "asset_type": instrument["asset_type"],
                        },
                    ),
                )

            payload = {
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "instrument_id": instrument["id"],
                "action": "OPENING",
                "side": "BUY",
                "trade_date": as_of_date.isoformat(),
                "amount_minor": cost_amount_minor,
                "nav_micros": average_cost_nav_micros,
                "shares_micros": total_shares_micros,
                "platform": normalized_platform,
                "note": note,
                "cost_basis_input": cost_basis_input,
            }
            request_hash = _canonical_hash(payload)
            existing = connection.execute(
                "SELECT id, request_hash FROM transaction_drafts WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used for different content",
                            http_status=409,
                        ),
                    )
                row = self._draft_row(connection, str(existing["id"]))
                connection.commit()
                return {
                    "draft": self._draft_data(row),
                    "confirmation_token": None,
                    "reused": True,
                    "cost_basis_input": cost_basis_input,
                    "warnings": [
                        *warnings,
                        "Duplicate request reused the existing opening-position draft; "
                        "use its original token"
                    ],
                }

            active_event = connection.execute(
                """
                SELECT id, kind, trade_date
                FROM transactions
                WHERE portfolio_id = ? AND account_id = ? AND instrument_id = ?
                  AND kind IN ('TRADE','OPENING')
                  AND reversed_by_transaction_id IS NULL
                ORDER BY trade_date, committed_at LIMIT 1
                """,
                (portfolio_id, account_id, instrument["id"]),
            ).fetchone()
            if active_event is not None:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "POSITION_ALREADY_INITIALIZED",
                        "an opening position must be the first active ledger event",
                        http_status=409,
                        details={
                            "existing_transaction_id": active_event["id"],
                            "existing_kind": active_event["kind"],
                            "existing_date": active_event["trade_date"],
                        },
                    ),
                )

            now = self._now()
            expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
            draft_id = str(uuid4())
            token = secrets.token_urlsafe(24)
            connection.execute(
                """
                INSERT INTO transaction_drafts (
                    id, portfolio_id, account_id, instrument_id, action, side, trade_date,
                    amount_minor, nav_micros, shares_micros, platform, note,
                    reversal_of_transaction_id, status, idempotency_key, request_hash,
                    confirmation_digest, expires_at, created_at, actor_ref
                ) VALUES (
                    ?, ?, ?, ?, 'OPENING', 'BUY', ?, ?, ?, ?, ?, ?, NULL, 'PENDING',
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    draft_id,
                    portfolio_id,
                    account_id,
                    instrument["id"],
                    as_of_date.isoformat(),
                    cost_amount_minor,
                    average_cost_nav_micros,
                    total_shares_micros,
                    normalized_platform,
                    note,
                    normalized_key,
                    request_hash,
                    _token_digest(token),
                    _iso(expires_at),
                    _iso(now),
                    actor_ref,
                ),
            )
            self._audit(
                connection,
                actor_type="AGENT",
                actor_ref=actor_ref,
                action="OPENING_POSITION_DRAFT_CREATED",
                entity_type="transaction_draft",
                entity_id=draft_id,
                details={
                    "instrument_code": instrument["code"],
                    "as_of_date": as_of_date.isoformat(),
                    "idempotency_key": normalized_key,
                    "expires_at": _iso(expires_at),
                    "cost_basis_input": cost_basis_input,
                },
                after_hash=request_hash,
            )
            row = self._draft_row(connection, draft_id)
            connection.commit()
            return {
                "draft": self._draft_data(row),
                "confirmation_token": token,
                "reused": False,
                "cost_basis_input": cost_basis_input,
                "warnings": warnings,
            }
        finally:
            connection.close()

    def create_reversal_draft(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise LedgerError("MISSING_REQUIRED_FIELD", "idempotency_key is required")
        connection = self._connect()
        try:
            self._begin(connection)
            original = self._require_row(
                connection.execute(
                    """
                    SELECT t.*, i.code AS instrument_code
                    FROM transactions t
                    JOIN instruments i ON i.id = t.instrument_id
                    WHERE t.id = ? AND t.kind IN ('TRADE','OPENING')
                    """,
                    (transaction_id,),
                ).fetchone(),
                "TRANSACTION_NOT_FOUND",
                "committed trade or opening position was not found",
            )
            if original["reversed_by_transaction_id"] is not None:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "TRANSACTION_ALREADY_REVERSED",
                        "transaction was already reversed",
                        http_status=409,
                    ),
                )
            payload = {
                "action": "REVERSAL",
                "reversal_of_transaction_id": transaction_id,
                "record_hash": original["record_hash"],
            }
            request_hash = _canonical_hash(payload)
            existing = connection.execute(
                "SELECT id, request_hash FROM transaction_drafts WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used for different content",
                            http_status=409,
                        ),
                    )
                row = self._draft_row(connection, str(existing["id"]))
                connection.commit()
                return {
                    "draft": self._draft_data(row),
                    "confirmation_token": None,
                    "reused": True,
                    "warnings": ["Duplicate request reused the existing reversal draft"],
                }

            self._position(
                connection,
                portfolio_id=str(original["portfolio_id"]),
                account_id=str(original["account_id"]),
                instrument_id=str(original["instrument_id"]),
                exclude_transaction_id=transaction_id,
            )
            now = self._now()
            expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
            draft_id = str(uuid4())
            token = secrets.token_urlsafe(24)
            connection.execute(
                """
                INSERT INTO transaction_drafts (
                    id, portfolio_id, account_id, instrument_id, action, side, trade_date,
                    amount_minor, nav_micros, shares_micros, platform, note,
                    reversal_of_transaction_id, status, idempotency_key, request_hash,
                    confirmation_digest, expires_at, created_at, actor_ref
                ) VALUES (
                    ?, ?, ?, ?, 'REVERSAL', ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING',
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    draft_id,
                    original["portfolio_id"],
                    original["account_id"],
                    original["instrument_id"],
                    original["side"],
                    original["trade_date"],
                    original["amount_minor"],
                    original["nav_micros"],
                    original["shares_micros"],
                    original["platform"],
                    f"Reversal of {transaction_id}",
                    transaction_id,
                    normalized_key,
                    request_hash,
                    _token_digest(token),
                    _iso(expires_at),
                    _iso(now),
                    actor_ref,
                ),
            )
            self._audit(
                connection,
                actor_type="AGENT",
                actor_ref=actor_ref,
                action="TRANSACTION_REVERSAL_DRAFT_CREATED",
                entity_type="transaction_draft",
                entity_id=draft_id,
                details={"reversal_of_transaction_id": transaction_id},
                after_hash=request_hash,
            )
            row = self._draft_row(connection, draft_id)
            connection.commit()
            return {
                "draft": self._draft_data(row),
                "confirmation_token": token,
                "reused": False,
                "warnings": [],
            }
        finally:
            connection.close()

    def get_transaction_draft(self, draft_id: str) -> JsonDict:
        with self._connect() as connection:
            return self._draft_data(self._draft_row(connection, draft_id))

    @staticmethod
    def _transaction_data(row: sqlite3.Row) -> JsonDict:
        result = {
            "id": row["id"],
            "draft_id": row["draft_id"],
            "portfolio_id": row["portfolio_id"],
            "account_id": row["account_id"],
            "instrument_id": row["instrument_id"],
            "instrument_code": row["instrument_code"],
            "instrument_name": row["instrument_name"],
            "kind": row["kind"],
            "side": row["side"],
            "trade_date": row["trade_date"],
            "amount": _format_scaled(int(row["amount_minor"]), 100, 2),
            "nav": _format_scaled(int(row["nav_micros"]), 1_000_000, 6),
            "shares": _format_scaled(int(row["shares_micros"]), 1_000_000, 6),
            "platform": row["platform"],
            "note": row["note"],
            "reversal_of_transaction_id": row["reversal_of_transaction_id"],
            "reversed_by_transaction_id": row["reversed_by_transaction_id"],
            "confirmed_by": row["confirmed_by"],
            "committed_at": row["committed_at"],
            "record_hash": row["record_hash"],
        }
        if row["kind"] == "OPENING":
            result.update(
                {
                    "as_of_date": row["trade_date"],
                    "cost_amount": _format_scaled(int(row["amount_minor"]), 100, 2),
                    "average_cost_nav": _format_scaled(
                        int(row["nav_micros"]), 1_000_000, 6
                    ),
                    "total_shares": _format_scaled(
                        int(row["shares_micros"]), 1_000_000, 6
                    ),
                }
            )
        return result

    def _transaction_row(self, connection: sqlite3.Connection, transaction_id: str) -> sqlite3.Row:
        return self._require_row(
            connection.execute(
                """
                SELECT t.*, i.code AS instrument_code, i.name AS instrument_name
                FROM transactions t
                JOIN instruments i ON i.id = t.instrument_id
                WHERE t.id = ?
                """,
                (transaction_id,),
            ).fetchone(),
            "TRANSACTION_NOT_FOUND",
            "committed transaction was not found",
        )

    def _insert_holding_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_id: str,
        portfolio_id: str,
        account_id: str,
        instrument_id: str,
        as_of: str,
        created_at: str,
    ) -> JsonDict:
        shares, cost = self._position(
            connection,
            portfolio_id=portfolio_id,
            account_id=account_id,
            instrument_id=instrument_id,
        )
        source = connection.execute(
            "SELECT kind, nav_micros FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()
        if source is not None and source["kind"] == "OPENING" and shares:
            average_nav = int(source["nav_micros"])
        else:
            average_nav = _round_div(cost * 10_000_000_000, shares) if shares else 0
        snapshot_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO holding_snapshots (
                id, transaction_id, portfolio_id, account_id, instrument_id, as_of,
                total_shares_micros, cost_amount_minor, average_cost_nav_micros, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                transaction_id,
                portfolio_id,
                account_id,
                instrument_id,
                as_of,
                shares,
                cost,
                average_nav,
                created_at,
            ),
        )
        return {
            "id": snapshot_id,
            "transaction_id": transaction_id,
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_id": instrument_id,
            "as_of": as_of,
            "total_shares": _format_scaled(shares, 1_000_000, 6),
            "cost_amount": _format_scaled(cost, 100, 2),
            "average_cost_nav": _format_scaled(average_nav, 1_000_000, 6),
            "created_at": created_at,
        }

    def commit_transaction_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        return self._commit_draft(
            draft_id=draft_id,
            confirmation_token=confirmation_token,
            confirmed_by=confirmed_by,
            allowed_actions={"TRADE", "REVERSAL"},
        )

    def commit_opening_position_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        return self._commit_draft(
            draft_id=draft_id,
            confirmation_token=confirmation_token,
            confirmed_by=confirmed_by,
            allowed_actions={"OPENING"},
        )

    def _commit_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
        allowed_actions: set[str],
    ) -> JsonDict:
        if not confirmation_token or not confirmed_by.strip():
            raise LedgerError(
                "CONFIRMATION_REQUIRED", "confirmation token and confirmed_by are required"
            )
        connection = self._connect()
        try:
            self._begin(connection)
            draft = self._draft_row(connection, draft_id)
            if draft["action"] not in allowed_actions:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "DRAFT_TYPE_MISMATCH",
                        "this draft must be committed with its exact commit operation",
                        http_status=409,
                        details={
                            "draft_action": draft["action"],
                            "allowed_actions": sorted(allowed_actions),
                        },
                    ),
                )
            if not hmac.compare_digest(
                str(draft["confirmation_digest"]), _token_digest(confirmation_token)
            ):
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVALID_CONFIRMATION_TOKEN",
                        "confirmation token does not match this draft",
                        http_status=403,
                    ),
                )
            if draft["status"] == "COMMITTED":
                transaction = self._transaction_row(
                    connection, str(draft["committed_transaction_id"])
                )
                connection.commit()
                return {
                    "transaction": self._transaction_data(transaction),
                    "holding": self._latest_holding(
                        connection,
                        str(draft["portfolio_id"]),
                        str(draft["account_id"]),
                        str(draft["instrument_id"]),
                    ),
                    "idempotent_replay": True,
                }
            if draft["status"] != "PENDING":
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "DRAFT_NOT_PENDING",
                        f"draft cannot be committed from status {draft['status']}",
                        http_status=409,
                    ),
                )
            now = self._now()
            if now > _parse_iso(str(draft["expires_at"])):
                connection.execute(
                    "UPDATE transaction_drafts SET status = 'EXPIRED' WHERE id = ?",
                    (draft_id,),
                )
                self._audit(
                    connection,
                    actor_type="SYSTEM",
                    actor_ref="investor-core",
                    action="TRANSACTION_DRAFT_EXPIRED",
                    entity_type="transaction_draft",
                    entity_id=draft_id,
                    details={"expires_at": draft["expires_at"]},
                )
                connection.commit()
                raise LedgerError(
                    "CONFIRMATION_TOKEN_EXPIRED",
                    "confirmation token expired; create a new draft",
                    http_status=410,
                )

            if draft["action"] == "REVERSAL":
                original_id = str(draft["reversal_of_transaction_id"])
                original = self._transaction_row(connection, original_id)
                if original["kind"] not in {
                    "TRADE",
                    "OPENING",
                } or original["reversed_by_transaction_id"]:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "TRANSACTION_ALREADY_REVERSED",
                            "original transaction is not reversible",
                            http_status=409,
                        ),
                    )
                self._position(
                    connection,
                    portfolio_id=str(draft["portfolio_id"]),
                    account_id=str(draft["account_id"]),
                    instrument_id=str(draft["instrument_id"]),
                    exclude_transaction_id=original_id,
                )
                kind = "REVERSAL"
            elif draft["action"] == "OPENING":
                original_id = None
                active_event = connection.execute(
                    """
                    SELECT id, kind, trade_date
                    FROM transactions
                    WHERE portfolio_id = ? AND account_id = ? AND instrument_id = ?
                      AND kind IN ('TRADE','OPENING')
                      AND reversed_by_transaction_id IS NULL
                    ORDER BY trade_date, committed_at LIMIT 1
                    """,
                    (
                        draft["portfolio_id"],
                        draft["account_id"],
                        draft["instrument_id"],
                    ),
                ).fetchone()
                if active_event is not None:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "POSITION_ALREADY_INITIALIZED",
                            "the position changed after the opening-position draft was created",
                            http_status=409,
                            details={
                                "existing_transaction_id": active_event["id"],
                                "existing_kind": active_event["kind"],
                                "existing_date": active_event["trade_date"],
                            },
                        ),
                    )
                kind = "OPENING"
            else:
                original_id = None
                kind = "TRADE"
                opening = connection.execute(
                    """
                    SELECT id, trade_date
                    FROM transactions
                    WHERE portfolio_id = ? AND account_id = ? AND instrument_id = ?
                      AND kind = 'OPENING' AND reversed_by_transaction_id IS NULL
                    ORDER BY trade_date, committed_at LIMIT 1
                    """,
                    (
                        draft["portfolio_id"],
                        draft["account_id"],
                        draft["instrument_id"],
                    ),
                ).fetchone()
                if opening is not None and draft["trade_date"] < opening["trade_date"]:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "TRADE_PREDATES_OPENING_POSITION",
                            "the opening position changed after this transaction draft was created",
                            http_status=409,
                            details={
                                "opening_transaction_id": opening["id"],
                                "opening_as_of_date": opening["trade_date"],
                                "trade_date": draft["trade_date"],
                            },
                        ),
                    )
                if draft["side"] == "SELL":
                    available_shares, _ = self._position(
                        connection,
                        portfolio_id=str(draft["portfolio_id"]),
                        account_id=str(draft["account_id"]),
                        instrument_id=str(draft["instrument_id"]),
                    )
                    if int(draft["shares_micros"]) > available_shares:
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "INSUFFICIENT_SHARES",
                                "recorded shares changed after draft creation",
                                http_status=409,
                            ),
                        )

            transaction_id = str(uuid4())
            committed_at = _iso(now)
            record_payload = {
                "id": transaction_id,
                "draft_id": draft_id,
                "kind": kind,
                "side": draft["side"],
                "trade_date": draft["trade_date"],
                "amount_minor": draft["amount_minor"],
                "nav_micros": draft["nav_micros"],
                "shares_micros": draft["shares_micros"],
                "reversal_of_transaction_id": original_id,
                "confirmed_by": confirmed_by.strip(),
                "committed_at": committed_at,
            }
            record_hash = _canonical_hash(record_payload)
            connection.execute(
                """
                INSERT INTO transactions (
                    id, draft_id, portfolio_id, account_id, instrument_id, kind, side,
                    trade_date, amount_minor, nav_micros, shares_micros, platform, note,
                    reversal_of_transaction_id, reversed_by_transaction_id, confirmed_by,
                    committed_at, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    transaction_id,
                    draft_id,
                    draft["portfolio_id"],
                    draft["account_id"],
                    draft["instrument_id"],
                    kind,
                    draft["side"],
                    draft["trade_date"],
                    draft["amount_minor"],
                    draft["nav_micros"],
                    draft["shares_micros"],
                    draft["platform"],
                    draft["note"],
                    original_id,
                    confirmed_by.strip(),
                    committed_at,
                    record_hash,
                ),
            )
            if original_id:
                connection.execute(
                    "UPDATE transactions SET reversed_by_transaction_id = ? WHERE id = ?",
                    (transaction_id, original_id),
                )
            connection.execute(
                """
                UPDATE transaction_drafts
                SET status = 'COMMITTED', committed_at = ?, committed_transaction_id = ?
                WHERE id = ?
                """,
                (committed_at, transaction_id, draft_id),
            )
            holding = self._insert_holding_snapshot(
                connection,
                transaction_id=transaction_id,
                portfolio_id=str(draft["portfolio_id"]),
                account_id=str(draft["account_id"]),
                instrument_id=str(draft["instrument_id"]),
                as_of=str(draft["trade_date"]),
                created_at=committed_at,
            )
            self._audit(
                connection,
                actor_type="USER",
                actor_ref=confirmed_by.strip(),
                action=(
                    "TRANSACTION_REVERSED"
                    if original_id
                    else (
                        "OPENING_POSITION_COMMITTED"
                        if kind == "OPENING"
                        else "TRANSACTION_COMMITTED"
                    )
                ),
                entity_type="transaction",
                entity_id=transaction_id,
                details={
                    "draft_id": draft_id,
                    "reversal_of_transaction_id": original_id,
                    "holding_snapshot_id": holding["id"],
                },
                before_hash=str(draft["request_hash"]),
                after_hash=record_hash,
            )
            transaction = self._transaction_row(connection, transaction_id)
            connection.commit()
            return {
                "transaction": self._transaction_data(transaction),
                "holding": holding,
                "idempotent_replay": False,
            }
        finally:
            connection.close()

    def _latest_holding(
        self,
        connection: sqlite3.Connection,
        portfolio_id: str,
        account_id: str,
        instrument_id: str,
    ) -> JsonDict | None:
        row = connection.execute(
            """
            SELECT hs.*, i.code AS instrument_code, i.name AS instrument_name
            FROM holding_snapshots hs
            JOIN instruments i ON i.id = hs.instrument_id
            WHERE hs.portfolio_id = ? AND hs.account_id = ? AND hs.instrument_id = ?
            ORDER BY hs.created_at DESC, hs.rowid DESC LIMIT 1
            """,
            (portfolio_id, account_id, instrument_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "transaction_id": row["transaction_id"],
            "portfolio_id": row["portfolio_id"],
            "account_id": row["account_id"],
            "instrument_id": row["instrument_id"],
            "instrument_code": row["instrument_code"],
            "instrument_name": row["instrument_name"],
            "as_of": row["as_of"],
            "total_shares": _format_scaled(int(row["total_shares_micros"]), 1_000_000, 6),
            "cost_amount": _format_scaled(int(row["cost_amount_minor"]), 100, 2),
            "average_cost_nav": _format_scaled(
                int(row["average_cost_nav_micros"]), 1_000_000, 6
            ),
            "created_at": row["created_at"],
        }

    def list_holdings(
        self, *, portfolio_id: str | None = None, account_id: str | None = None
    ) -> list[JsonDict]:
        query = """
            WITH ranked AS (
                SELECT hs.*, hs.rowid AS event_order,
                       ROW_NUMBER() OVER (
                           PARTITION BY hs.portfolio_id, hs.account_id, hs.instrument_id
                           ORDER BY hs.created_at DESC, hs.rowid DESC
                       ) AS rank_no
                FROM holding_snapshots hs
            )
            SELECT ranked.*, i.code AS instrument_code, i.name AS instrument_name
            FROM ranked
            JOIN instruments i ON i.id = ranked.instrument_id
            WHERE ranked.rank_no = 1
        """
        parameters: list[Any] = []
        if portfolio_id:
            query += " AND ranked.portfolio_id = ?"
            parameters.append(portfolio_id)
        if account_id:
            query += " AND ranked.account_id = ?"
            parameters.append(account_id)
        query += " ORDER BY i.code"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            result: list[JsonDict] = []
            for row in rows:
                result.append(
                    {
                        "portfolio_id": row["portfolio_id"],
                        "account_id": row["account_id"],
                        "instrument_id": row["instrument_id"],
                        "instrument_code": row["instrument_code"],
                        "instrument_name": row["instrument_name"],
                        "as_of": row["as_of"],
                        "total_shares": _format_scaled(
                            int(row["total_shares_micros"]), 1_000_000, 6
                        ),
                        "cost_amount": _format_scaled(int(row["cost_amount_minor"]), 100, 2),
                        "average_cost_nav": _format_scaled(
                            int(row["average_cost_nav_micros"]), 1_000_000, 6
                        ),
                        "created_at": row["created_at"],
                    }
                )
            return result

    def list_transactions(
        self,
        *,
        portfolio_id: str | None = None,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        if limit < 1 or limit > 500:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 500")
        query = """
            SELECT t.*, i.code AS instrument_code, i.name AS instrument_name
            FROM transactions t
            JOIN instruments i ON i.id = t.instrument_id
            WHERE 1 = 1
        """
        parameters: list[Any] = []
        if portfolio_id:
            query += " AND t.portfolio_id = ?"
            parameters.append(portfolio_id)
        if account_id:
            query += " AND t.account_id = ?"
            parameters.append(account_id)
        query += " ORDER BY t.committed_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._transaction_data(row) for row in rows]
