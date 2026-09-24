"""One continuous HTTP journey per branch; all facts are isolated fixtures."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_planning import configured_services
from test_subscriptions import _business_fact_counts

from investor_core.api.app import create_app


def facts(path):
    with closing(sqlite3.connect(path)) as connection:
        return list(connection.iterdump())


def call(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def approval(draft):
    return {"confirmation_token": draft["confirmation_token"], "confirmed_by": "isolated-user"}


@pytest.mark.parametrize("outcome", ["executed", "partial", "skip"])
def test_complete_http_business_journey_and_lost_commit_response(tmp_path, outcome):
    database = tmp_path / (outcome + ".db")
    _, planning, pid, aid = configured_services(database)
    with TestClient(create_app(planning.settings)) as client:
        context = {"portfolio_id": pid, "account_id": aid}
        before = facts(database)
        preview = call(
            client,
            "GET",
            "/v1/weekly-plan-preview",
            params={
                **context,
                "contribution_amount": "100",
                "plan_date": "2026-07-21",
                "as_of_date": "2026-07-21",
            },
        )
        assert preview["plan"]["instrument_items"][0]["instrument_code"] == "CORE01"
        assert facts(database) == before
        created = call(
            client,
            "POST",
            "/v1/weekly-plans",
            json={
                **context,
                "contribution_amount": "100",
                "plan_date": "2026-07-21",
                "as_of_date": "2026-07-21",
                "idempotency_key": "journey-plan",
            },
        )
        plan_id = created["plan"]["id"]
        plan_path = "/v1/weekly-plans/" + plan_id
        initial = _business_fact_counts(database)
        if outcome == "skip":
            call(
                client,
                "POST",
                plan_path + "/skip",
                json={**approval(created), "reason": "isolated explicit skip"},
            )
            assert _business_fact_counts(database) == initial
        else:
            call(client, "POST", plan_path + "/freeze", json=approval(created))
            amounts = [("40", "39", "2026-07-22")]
            if outcome == "executed":
                amounts.append(("60", "59", "2026-07-24"))
            for index, (gross, net, day) in enumerate(amounts):
                payload = {
                    **context,
                    "weekly_plan_id": plan_id,
                    "instrument_code": "CORE01",
                    "requested_amount": gross,
                    "submitted_at": day + "T10:00:00+08:00",
                    "submitted_business_date": day,
                    "external_platform": "isolated platform",
                    "expected_confirmation_date": day,
                    "idempotency_key": f"order-{index}",
                }
                submitted = call(client, "POST", "/v1/external-subscription-drafts", json=payload)
                sub = call(
                    client,
                    "POST",
                    "/v1/external-subscription-drafts/" + submitted["draft"]["id"] + "/commit",
                    json=approval(submitted),
                )["subscription"]
                baseline = _business_fact_counts(database)
                assert baseline["transactions"] == initial["transactions"] + index
                progress = call(client, "GET", plan_path)["execution_progress"]
                assert progress["in_flight_amount"] == gross + ".00"
                pending = call(
                    client,
                    "POST",
                    "/v1/external-subscriptions/" + sub["id"] + "/status-drafts",
                    json={
                        "target_status": "PENDING_CONFIRMATION",
                        "reason": "isolated processing",
                        "idempotency_key": f"pending-{index}",
                    },
                )
                call(
                    client,
                    "POST",
                    "/v1/external-subscription-drafts/" + pending["draft"]["id"] + "/commit",
                    json=approval(pending),
                )
                confirmation = call(
                    client,
                    "POST",
                    "/v1/external-subscriptions/" + sub["id"] + "/confirmation-drafts",
                    json={
                        "confirmed_at": day + "T18:00:00+08:00",
                        "confirmation_business_date": day,
                        "nav_date": day,
                        "nav": "1",
                        "confirmed_shares": net,
                        "confirmed_amount": net,
                        "fee": "1",
                        "idempotency_key": f"confirm-{index}",
                    },
                )
                confirmed = call(
                    client,
                    "POST",
                    "/v1/external-subscription-drafts/" + confirmation["draft"]["id"] + "/commit",
                    json=approval(confirmation),
                )["subscription"]
                assert _business_fact_counts(database)["transactions"] == baseline["transactions"]
                cid = confirmed["confirmations"][0]["id"]
                transaction_path = (
                    "/v1/external-subscription-confirmations/" + cid + "/transaction-drafts"
                )
                transaction = call(
                    client, "POST", transaction_path, json={"idempotency_key": f"ledger-{index}"}
                )
                commit_path = transaction_path + "/" + transaction["draft"]["id"] + "/commit"
                # Server commits, client discards the response body; recover by reading facts.
                response = client.post(commit_path, json=approval(transaction))
                assert response.status_code == 200
                del response
                readback = call(client, "GET", "/v1/external-subscriptions/" + sub["id"])
                assert readback["confirmations"][0]["transaction_id"]
                after = _business_fact_counts(database)
                assert after["transactions"] == baseline["transactions"] + 1
                assert after["plan_execution_links"] == baseline["plan_execution_links"] + 1
                # Deliberate negative retry in isolation, not the recommended operator workflow.
                retry = client.post(commit_path, json=approval(transaction))
                assert retry.status_code in (200, 400, 409)
                assert _business_fact_counts(database) == after
            if outcome == "partial":
                closed = call(
                    client,
                    "POST",
                    plan_path + "/partial-close-drafts",
                    json={
                        "closure_business_date": "2026-07-27",
                        "closure_reason_code": "USER_DECLINED_REMAINDER",
                        "closure_note": "isolated remainder not carried",
                        "carry_forward": False,
                        "idempotency_key": "close",
                    },
                )
                call(
                    client,
                    "POST",
                    "/v1/weekly-plan-partial-close-drafts/" + closed["draft"]["id"] + "/commit",
                    json=approval(closed),
                )
        status = call(client, "GET", plan_path)["status"]
        assert (
            status
            == {"executed": "EXECUTED", "partial": "PARTIALLY_EXECUTED_CLOSED", "skip": "SKIPPED"}[
                outcome
            ]
        )
        holdings = call(client, "GET", "/v1/holdings", params=context)["items"]
        core = next(h for h in holdings if h["instrument_code"] == "CORE01")
        assert (
            Decimal(core["total_shares"]) == {"executed": 198, "partial": 139, "skip": 100}[outcome]
        )
        assert (
            Decimal(core["cost_amount"]) == {"executed": 200, "partial": 140, "skip": 100}[outcome]
        )
        assert (
            _business_fact_counts(database)["cash_ledger_events"] == initial["cash_ledger_events"]
        )
        transactions = call(client, "GET", "/v1/transactions", params=context)["items"]
        buys = [t for t in transactions if t["side"] == "BUY" and t["kind"] != "OPENING"]
        assert (
            sum(Decimal(t["amount"]) for t in buys)
            == {"executed": 100, "partial": 40, "skip": 0}[outcome]
        )
        report = call(
            client, "POST", plan_path + "/report-drafts", json={"idempotency_key": "report"}
        )
        before_report = _business_fact_counts(database)
        report_path = "/v1/weekly-report-drafts/" + report["draft"]["id"] + "/commit"
        result = call(client, "POST", report_path, json=approval(report))
        assert result["financial_facts_created"] is False and result["notification_sent"] is False
        content = result["report"]["content"]
        assert (
            content["executed_amount"]
            == {"executed": "100.00", "partial": "40.00", "skip": "0.00"}[outcome]
        )
        assert (
            content["abandoned_amount"]
            == {"executed": "0.00", "partial": "60.00", "skip": "100.00"}[outcome]
        )
        assert content["valuation"]["status"] == "LIMITED"
        assert content["valuation"]["substitution_used"] is False
        repeat = client.post(report_path, json=approval(report))
        assert repeat.status_code in (200, 400, 409)
        assert _business_fact_counts(database) == before_report
        reports = call(client, "GET", "/v1/weekly-reports", params={"portfolio_id": pid})
        assert len(reports["items"]) == 1
