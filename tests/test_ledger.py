from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerError, LedgerService


def build_service(
    tmp_path: Path,
    *,
    now: list[datetime] | None = None,
) -> tuple[LedgerService, dict[str, object]]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    clock = now or [datetime(2026, 7, 20, 12, 0, tzinfo=UTC)]
    service = LedgerService(settings, now=lambda: clock[0])
    portfolio = service.create_portfolio(name="测试组合")
    account = service.create_account(
        portfolio_id=str(portfolio["id"]),
        name="测试账户",
        platform="模拟平台",
    )
    instrument = service.create_instrument(
        code="DEMO001",
        name="模拟基金",
        role="CORE",
    )
    return service, {
        "portfolio": portfolio,
        "account": account,
        "instrument": instrument,
        "clock": clock,
    }


def buy_draft(
    service: LedgerService,
    context: dict[str, object],
    *,
    idempotency_key: str = "message-001",
) -> dict[str, object]:
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    return service.create_transaction_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="DEMO001",
        side="BUY",
        trade_date_value="2026-07-20",
        amount="100.00",
        nav="1.250000",
        shares="80.000000",
        platform="模拟平台",
        idempotency_key=idempotency_key,
    )


def opening_draft(
    service: LedgerService,
    context: dict[str, object],
    *,
    idempotency_key: str = "opening-001",
) -> dict[str, object]:
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    return service.create_opening_position_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="DEMO001",
        as_of_date_value="2026-07-19",
        total_shares="100.000000",
        cost_amount="123.45",
        platform="模拟平台",
        idempotency_key=idempotency_key,
        note="平台持仓页",
    )


def test_opening_position_requires_exact_commit_and_is_not_a_trade(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = opening_draft(service, context)
    draft = created["draft"]
    token = created["confirmation_token"]
    assert isinstance(draft, dict)
    assert isinstance(token, str)
    assert draft["action"] == "OPENING"
    assert draft["as_of_date"] == "2026-07-19"
    assert draft["total_shares"] == "100.000000"
    assert draft["cost_amount"] == "123.45"
    assert draft["average_cost_nav"] == "1.234500"
    assert service.list_holdings() == []

    with pytest.raises(LedgerError, match="exact commit operation") as captured:
        service.commit_transaction_draft(
            draft_id=str(draft["id"]),
            confirmation_token=token,
            confirmed_by="test-user",
        )
    assert captured.value.code == "DRAFT_TYPE_MISMATCH"
    assert service.get_transaction_draft(str(draft["id"]))["status"] == "PENDING"

    committed = service.commit_opening_position_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )
    transaction = committed["transaction"]
    holding = committed["holding"]
    assert isinstance(transaction, dict)
    assert isinstance(holding, dict)
    assert transaction["kind"] == "OPENING"
    assert transaction["as_of_date"] == "2026-07-19"
    assert holding["total_shares"] == "100.000000"
    assert holding["cost_amount"] == "123.45"

    replay = service.commit_opening_position_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )
    assert replay["idempotent_replay"] is True
    assert len(service.list_transactions()) == 1


def test_opening_position_derives_total_cost_from_platform_average_cost(
    tmp_path: Path,
) -> None:
    service, context = build_service(tmp_path)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    created = service.create_opening_position_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="DEMO001",
        as_of_date_value="2026-07-17",
        total_shares="123.91",
        average_cost_nav="1.9904",
        platform="支付宝",
        idempotency_key="average-cost-opening",
        note="支付宝持仓页",
    )
    draft = created["draft"]
    token = created["confirmation_token"]
    assert isinstance(draft, dict)
    assert isinstance(token, str)
    assert created["cost_basis_input"] == "AVERAGE_COST_NAV"
    assert draft["total_shares"] == "123.910000"
    assert draft["average_cost_nav"] == "1.990400"
    assert draft["cost_amount"] == "246.63"
    assert any("rounded to CNY 0.01" in warning for warning in created["warnings"])

    committed = service.commit_opening_position_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )
    holding = committed["holding"]
    assert isinstance(holding, dict)
    assert holding["total_shares"] == "123.910000"
    assert holding["cost_amount"] == "246.63"
    assert holding["average_cost_nav"] == "1.990400"


def test_opening_position_requires_exactly_one_cost_basis(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    common = {
        "portfolio_id": str(portfolio["id"]),
        "account_id": str(account["id"]),
        "instrument_code": "DEMO001",
        "as_of_date_value": "2026-07-17",
        "total_shares": "123.91",
        "platform": "支付宝",
        "idempotency_key": "invalid-cost-basis",
    }

    with pytest.raises(LedgerError) as missing:
        service.create_opening_position_draft(**common)
    assert missing.value.code == "OPENING_COST_BASIS_REQUIRED"

    with pytest.raises(LedgerError) as duplicate:
        service.create_opening_position_draft(
            **common,
            cost_amount="246.63",
            average_cost_nav="1.9904",
        )
    assert duplicate.value.code == "OPENING_COST_BASIS_REQUIRED"


def test_opening_position_rejects_future_index_and_initialized_positions(
    tmp_path: Path,
) -> None:
    service, context = build_service(tmp_path)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)

    with pytest.raises(LedgerError) as future_error:
        service.create_opening_position_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code="DEMO001",
            as_of_date_value="2026-07-21",
            total_shares="10",
            cost_amount="10",
            platform="模拟平台",
            idempotency_key="future-opening",
        )
    assert future_error.value.code == "FUTURE_AS_OF_DATE"

    service.create_instrument(code="000510", name="中证A500", asset_type="INDEX")
    with pytest.raises(LedgerError) as index_error:
        service.create_opening_position_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code="000510",
            as_of_date_value="2026-07-19",
            total_shares="10",
            cost_amount="10",
            platform="模拟平台",
            idempotency_key="index-opening",
        )
    assert index_error.value.code == "NON_TRADABLE_INSTRUMENT"

    created = buy_draft(service, context)
    draft = created["draft"]
    token = created["confirmation_token"]
    assert isinstance(draft, dict)
    assert isinstance(token, str)
    service.commit_transaction_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )
    with pytest.raises(LedgerError) as initialized_error:
        opening_draft(service, context, idempotency_key="late-opening")
    assert initialized_error.value.code == "POSITION_ALREADY_INITIALIZED"


def test_transaction_cannot_predate_opening_position(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = opening_draft(service, context)
    draft = created["draft"]
    token = created["confirmation_token"]
    assert isinstance(draft, dict)
    assert isinstance(token, str)
    service.commit_opening_position_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )

    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    with pytest.raises(LedgerError) as captured:
        service.create_transaction_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code="DEMO001",
            side="BUY",
            trade_date_value="2026-07-18",
            amount="10.00",
            nav="1.000000",
            shares="10.000000",
            platform="模拟平台",
            idempotency_key="predates-opening",
        )
    assert captured.value.code == "TRADE_PREDATES_OPENING_POSITION"


def test_opening_position_uses_configured_business_date(tmp_path: Path) -> None:
    clock = [datetime(2026, 7, 19, 16, 30, tzinfo=UTC)]
    service, context = build_service(tmp_path, now=clock)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)

    created = service.create_opening_position_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="DEMO001",
        as_of_date_value="2026-07-20",
        total_shares="10",
        cost_amount="10",
        platform="模拟平台",
        idempotency_key="local-business-date",
    )
    assert created["draft"]["as_of_date"] == "2026-07-20"


def test_transaction_commit_rechecks_opening_position_order(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    trade = service.create_transaction_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="DEMO001",
        side="BUY",
        trade_date_value="2026-07-18",
        amount="10.00",
        nav="1.000000",
        shares="10.000000",
        platform="模拟平台",
        idempotency_key="early-pending-trade",
    )
    trade_draft = trade["draft"]
    trade_token = trade["confirmation_token"]
    assert isinstance(trade_draft, dict)
    assert isinstance(trade_token, str)

    opening = opening_draft(service, context)
    opening_data = opening["draft"]
    opening_token = opening["confirmation_token"]
    assert isinstance(opening_data, dict)
    assert isinstance(opening_token, str)
    service.commit_opening_position_draft(
        draft_id=str(opening_data["id"]),
        confirmation_token=opening_token,
        confirmed_by="test-user",
    )

    with pytest.raises(LedgerError) as captured:
        service.commit_transaction_draft(
            draft_id=str(trade_draft["id"]),
            confirmation_token=trade_token,
            confirmed_by="test-user",
        )
    assert captured.value.code == "TRADE_PREDATES_OPENING_POSITION"


def test_opening_commit_rechecks_that_position_is_still_empty(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    opening = opening_draft(service, context)
    opening_data = opening["draft"]
    opening_token = opening["confirmation_token"]
    assert isinstance(opening_data, dict)
    assert isinstance(opening_token, str)

    trade = buy_draft(service, context, idempotency_key="concurrent-trade")
    trade_data = trade["draft"]
    trade_token = trade["confirmation_token"]
    assert isinstance(trade_data, dict)
    assert isinstance(trade_token, str)
    service.commit_transaction_draft(
        draft_id=str(trade_data["id"]),
        confirmation_token=trade_token,
        confirmed_by="test-user",
    )

    with pytest.raises(LedgerError) as captured:
        service.commit_opening_position_draft(
            draft_id=str(opening_data["id"]),
            confirmation_token=opening_token,
            confirmed_by="test-user",
        )
    assert captured.value.code == "POSITION_ALREADY_INITIALIZED"


def test_opening_position_can_be_reversed_without_deleting_history(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = opening_draft(service, context)
    draft = created["draft"]
    token = created["confirmation_token"]
    assert isinstance(draft, dict)
    assert isinstance(token, str)
    committed = service.commit_opening_position_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )
    transaction = committed["transaction"]
    assert isinstance(transaction, dict)

    reversal = service.create_reversal_draft(
        transaction_id=str(transaction["id"]),
        idempotency_key="reverse-opening",
    )
    reversal_draft = reversal["draft"]
    reversal_token = reversal["confirmation_token"]
    assert isinstance(reversal_draft, dict)
    assert isinstance(reversal_token, str)
    result = service.commit_transaction_draft(
        draft_id=str(reversal_draft["id"]),
        confirmation_token=reversal_token,
        confirmed_by="test-user",
    )

    holding = result["holding"]
    assert isinstance(holding, dict)
    assert holding["total_shares"] == "0.000000"
    assert {item["kind"] for item in service.list_transactions()} == {
        "REVERSAL",
        "OPENING",
    }


def test_commit_is_explicit_and_idempotent(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = buy_draft(service, context)
    draft = created["draft"]
    assert isinstance(draft, dict)
    token = created["confirmation_token"]
    assert isinstance(token, str)
    assert service.list_holdings() == []

    committed = service.commit_transaction_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )
    assert committed["idempotent_replay"] is False
    holding = committed["holding"]
    assert isinstance(holding, dict)
    assert holding["total_shares"] == "80.000000"
    assert holding["cost_amount"] == "100.00"
    assert holding["average_cost_nav"] == "1.250000"

    replay = service.commit_transaction_draft(
        draft_id=str(draft["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )
    assert replay["idempotent_replay"] is True
    assert len(service.list_transactions()) == 1


def test_duplicate_message_reuses_draft_but_never_reissues_token(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = buy_draft(service, context)
    duplicate = buy_draft(service, context)

    assert duplicate["reused"] is True
    assert duplicate["confirmation_token"] is None
    assert duplicate["draft"] == created["draft"]


def test_idempotency_key_cannot_be_reused_for_changed_content(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    buy_draft(service, context)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)

    with pytest.raises(LedgerError, match="different content") as captured:
        service.create_transaction_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code="DEMO001",
            side="BUY",
            trade_date_value="2026-07-20",
            amount="50.00",
            nav="1.250000",
            shares="40.000000",
            platform="模拟平台",
            idempotency_key="message-001",
        )
    assert captured.value.code == "IDEMPOTENCY_CONFLICT"


def test_index_benchmark_cannot_create_transaction_draft(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    service.create_instrument(
        code="000510",
        name="中证A500",
        asset_type="INDEX",
        role="CORE",
    )

    with pytest.raises(LedgerError, match="index benchmark") as captured:
        service.create_transaction_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code="000510",
            side="BUY",
            trade_date_value="2026-07-20",
            amount="100.00",
            nav="1.250000",
            shares="80.000000",
            platform="支付宝",
            idempotency_key="index-must-not-trade",
        )

    assert captured.value.code == "NON_TRADABLE_INSTRUMENT"
    assert service.list_holdings() == []


def test_expired_token_changes_no_holding(tmp_path: Path) -> None:
    clock = [datetime(2026, 7, 20, 12, 0, tzinfo=UTC)]
    service, context = build_service(tmp_path, now=clock)
    created = buy_draft(service, context)
    draft = created["draft"]
    assert isinstance(draft, dict)
    token = created["confirmation_token"]
    assert isinstance(token, str)
    clock[0] += timedelta(minutes=16)

    with pytest.raises(LedgerError, match="expired") as captured:
        service.commit_transaction_draft(
            draft_id=str(draft["id"]),
            confirmation_token=token,
            confirmed_by="test-user",
        )
    assert captured.value.code == "CONFIRMATION_TOKEN_EXPIRED"
    assert service.get_transaction_draft(str(draft["id"]))["status"] == "EXPIRED"
    assert service.list_holdings() == []


def test_sell_cannot_exceed_recorded_shares(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)

    with pytest.raises(LedgerError, match="exceeds") as captured:
        service.create_transaction_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code="DEMO001",
            side="SELL",
            trade_date_value="2026-07-20",
            amount="12.50",
            nav="1.250000",
            shares="10.000000",
            platform="模拟平台",
            idempotency_key="sell-without-holding",
        )
    assert captured.value.code == "INSUFFICIENT_SHARES"


def test_sell_reduces_shares_and_cost_at_average_cost(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = buy_draft(service, context)
    buy = created["draft"]
    assert isinstance(buy, dict)
    token = created["confirmation_token"]
    assert isinstance(token, str)
    service.commit_transaction_draft(
        draft_id=str(buy["id"]), confirmation_token=token, confirmed_by="test-user"
    )

    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    sell = service.create_transaction_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="DEMO001",
        side="SELL",
        trade_date_value="2026-07-21",
        amount="39.00",
        nav="1.300000",
        shares="30.000000",
        platform="模拟平台",
        idempotency_key="sell-001",
    )
    sell_draft = sell["draft"]
    assert isinstance(sell_draft, dict)
    sell_token = sell["confirmation_token"]
    assert isinstance(sell_token, str)
    committed = service.commit_transaction_draft(
        draft_id=str(sell_draft["id"]),
        confirmation_token=sell_token,
        confirmed_by="test-user",
    )
    holding = committed["holding"]
    assert isinstance(holding, dict)
    assert holding["total_shares"] == "50.000000"
    assert holding["cost_amount"] == "62.50"
    assert holding["average_cost_nav"] == "1.250000"


def test_reversal_is_confirmed_and_preserves_audit_history(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = buy_draft(service, context)
    draft = created["draft"]
    assert isinstance(draft, dict)
    token = created["confirmation_token"]
    assert isinstance(token, str)
    committed = service.commit_transaction_draft(
        draft_id=str(draft["id"]), confirmation_token=token, confirmed_by="test-user"
    )
    transaction = committed["transaction"]
    assert isinstance(transaction, dict)

    reversal = service.create_reversal_draft(
        transaction_id=str(transaction["id"]),
        idempotency_key="reverse-001",
    )
    reversal_draft = reversal["draft"]
    assert isinstance(reversal_draft, dict)
    reversal_token = reversal["confirmation_token"]
    assert isinstance(reversal_token, str)
    reversed_result = service.commit_transaction_draft(
        draft_id=str(reversal_draft["id"]),
        confirmation_token=reversal_token,
        confirmed_by="test-user",
    )

    holding = reversed_result["holding"]
    assert isinstance(holding, dict)
    assert holding["total_shares"] == "0.000000"
    assert holding["cost_amount"] == "0.00"
    transactions = service.list_transactions()
    assert len(transactions) == 2
    original = next(item for item in transactions if item["kind"] == "TRADE")
    reversal_record = next(item for item in transactions if item["kind"] == "REVERSAL")
    assert original["reversed_by_transaction_id"] == reversal_record["id"]
    assert reversal_record["reversal_of_transaction_id"] == original["id"]


def test_reversing_buy_is_rejected_if_later_sell_would_be_uncovered(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    created = buy_draft(service, context)
    buy_draft_data = created["draft"]
    assert isinstance(buy_draft_data, dict)
    buy_token = created["confirmation_token"]
    assert isinstance(buy_token, str)
    buy_result = service.commit_transaction_draft(
        draft_id=str(buy_draft_data["id"]),
        confirmation_token=buy_token,
        confirmed_by="test-user",
    )

    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)
    sell = service.create_transaction_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="DEMO001",
        side="SELL",
        trade_date_value="2026-07-21",
        amount="13.00",
        nav="1.300000",
        shares="10.000000",
        platform="模拟平台",
        idempotency_key="sell-before-reversal",
    )
    sell_draft_data = sell["draft"]
    assert isinstance(sell_draft_data, dict)
    sell_token = sell["confirmation_token"]
    assert isinstance(sell_token, str)
    service.commit_transaction_draft(
        draft_id=str(sell_draft_data["id"]),
        confirmation_token=sell_token,
        confirmed_by="test-user",
    )
    transaction = buy_result["transaction"]
    assert isinstance(transaction, dict)

    with pytest.raises(LedgerError) as captured:
        service.create_reversal_draft(
            transaction_id=str(transaction["id"]),
            idempotency_key="invalid-reversal",
        )
    assert captured.value.code == "INSUFFICIENT_SHARES"


def test_amount_nav_and_shares_are_validated_deterministically(tmp_path: Path) -> None:
    service, context = build_service(tmp_path)
    portfolio = context["portfolio"]
    account = context["account"]
    assert isinstance(portfolio, dict)
    assert isinstance(account, dict)

    with pytest.raises(LedgerError, match="configured tolerance") as captured:
        service.create_transaction_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code="DEMO001",
            side="BUY",
            trade_date_value="2026-07-20",
            amount="100.00",
            nav="1.250000",
            shares="50.000000",
            platform="模拟平台",
            idempotency_key="bad-math",
        )
    assert captured.value.code == "AMOUNT_SHARE_MISMATCH"
