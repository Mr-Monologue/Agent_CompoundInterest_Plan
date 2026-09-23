from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_daily_client import dump
from test_weekly_reports import terminal_plan

from investor_core.api.app import create_app
from investor_core.execution import (
    ConstraintBundle,
    ConstraintDraft,
    ExecutionService,
    SourceArchive,
    calculate,
)
from investor_core.ledger import LedgerError
from investor_core.research import ResearchService


def dt(value):
    return datetime.fromisoformat(value)


START = dt("2026-09-23T00:00:00+08:00")
END = dt("2026-09-26T00:00:00+08:00")


def bundle(channel="CHANNEL_A", cap=1000):
    rule = dict(
        effective_from=START.isoformat(),
        effective_to=END.isoformat(),
        source_ids=["source"],
        minimum_order_minor=100,
        per_order_minor=600,
        per_day_minor=cap,
        cumulative_minor=None,
        cumulative_from=START.isoformat(),
        cumulative_to=END.isoformat(),
        quota_scope="CHANNEL_ACCOUNT",
        cancelled_orders_consume_quota=True,
    )
    calendar = [
        dict(
            day=f"2026-09-{d}",
            exchange_open=d != 25,
            fund_subscription_open=True,
            channel_accepting=True,
            qdii_subscription_open=True,
            opens_at=f"2026-09-{d}T09:00:00+08:00",
            cutoff_at=f"2026-09-{d}T15:00:00+08:00",
            exchange_source_ids=["source"],
            fund_source_ids=["source"],
            channel_source_ids=["source"],
            qdii_source_ids=["source"],
            confirmation_rule="Explicit source rule; confirmation date not predicted",
        )
        for d in (23, 24, 25)
    ]
    return dict(
        instrument_code="CORE01",
        share_class="A",
        channel=channel,
        account_id="account",
        qdii=True,
        applicability_source_ids=["source"],
        limits=[rule],
        calendar=calendar,
        quota_observations=[
            dict(
                business_date="2026-09-23",
                observed_at=START.isoformat(),
                valid_until="2026-09-23T15:00:00+08:00",
                remaining_minor=cap,
                quota_scope="CHANNEL_ACCOUNT",
                source_ids=["source"],
            )
        ],
        rationale="Account and channel applicability explicitly reviewed",
    )


def run(b, amount=4000, now=START, submitted=None):
    b = ConstraintBundle.model_validate(b).model_dump(mode="json")
    result = calculate(amount, b, START, END, now, submitted or [])
    assert (
        sum(result[k] for k in ("executable_minor", "unverified_minor", "infeasible_minor"))
        == amount
    )
    assert sum(x["amount_minor"] for x in result["schedule"]) == result["executable_minor"]
    return result


def test_channel_quota_and_holiday_not_weekend():
    assert run(bundle())["executable_minor"] == 2000
    assert run(bundle("CHANNEL_B", 2000))["executable_minor"] == 4000
    assert all(x["business_date"] != "2026-09-25" for x in run(bundle())["schedule"])


def test_mid_period_limit_and_intraday_change():
    b = bundle()
    old = b["limits"][0]
    new = dict(old, effective_from="2026-09-24T12:00:00+08:00", per_day_minor=500)
    old["effective_to"] = new["effective_from"]
    b["limits"].append(new)
    b["quota_observations"] = [
        dict(
            business_date="2026-09-24",
            observed_at="2026-09-24T12:00:00+08:00",
            valid_until="2026-09-24T15:00:00+08:00",
            remaining_minor=500,
            quota_scope="CHANNEL_ACCOUNT",
            source_ids=["source"],
        )
    ]
    result = run(b, now=dt("2026-09-24T13:00:00+08:00"))
    assert result["executable_minor"] == 500
    assert all(dt(s["at"]) >= dt(new["effective_from"]) for s in result["schedule"])


def test_existing_submission_daily_and_cumulative_scope():
    b = bundle()
    submitted = [
        dict(
            code="CORE01",
            external_platform="CHANNEL_A",
            requested_amount_minor=700,
            cancelled_amount_minor=0,
            refunded_amount_minor=0,
            submitted_business_date="2026-09-23",
            submitted_at="2026-09-23T09:00:00+08:00",
        )
    ]
    b["limits"][0]["cumulative_minor"] = 1500
    result = run(b, submitted=submitted)
    assert result["executable_minor"] == 800
    assert (
        sum(x["amount_minor"] for x in result["schedule"] if x["business_date"] == "2026-09-23")
        == 300
    )
    submitted[0]["external_platform"] = "OTHER"
    assert run(b, submitted=submitted)["executable_minor"] == 1500
    b["limits"][0]["quota_scope"] = "FUND_ACCOUNT_ALL_CHANNELS"
    b["quota_observations"][0]["quota_scope"] = "FUND_ACCOUNT_ALL_CHANNELS"
    assert run(b, submitted=submitted)["executable_minor"] == 800


@pytest.mark.parametrize(
    "field",
    ["exchange_open", "fund_subscription_open", "channel_accepting", "qdii_subscription_open"],
)
def test_calendar_layers_and_cutoff(field):
    b = bundle()
    b["calendar"][1][field] = False
    result = run(b, now=dt("2026-09-23T15:00:00+08:00"))
    assert result["executable_minor"] == 0 and result["infeasible_minor"] == 4000


def test_conflict_unknown_and_conservation_no_reallocation():
    b = bundle()
    b["limits"].append(deepcopy(b["limits"][0]))
    assert run(b)["unverified_minor"] == 4000
    b = bundle()
    b["calendar"][1]["fund_subscription_open"] = None
    result = run(b)
    assert result["executable_minor"] == 1000 and result["unverified_minor"] == 3000
    result = calculate(4000, None, START, END, START, [])
    assert result["unverified_minor"] == 4000 and result["infeasible_minor"] == 0


def test_minimum_split_and_exact_end_boundary():
    b = bundle(cap=1000)
    b["limits"][0].update(minimum_order_minor=400, per_order_minor=700)
    result = run(b, amount=1000)
    assert sorted(x["amount_minor"] for x in result["schedule"]) == [400, 600]
    result = calculate(4000, b, START, dt("2026-09-23T09:00:00+08:00"), START, [])
    assert result["executable_minor"] == 0


def test_archive_governance_replay_drift_and_readonly(tmp_path):
    settings, pid, aid, _ = terminal_plan(tmp_path / "db", outcome="SKIPPED")
    clock = [START.astimezone(UTC)]
    service = ExecutionService(ResearchService(settings, now=lambda: clock[0]))
    request = SourceArchive(
        instrument_code="CORE01",
        source_name="Issuer",
        source_ref="https://example.com/source",
        source_lineage="ISSUER",
        retrieved_at=START,
        published_date=START.date(),
        data_date=START.date(),
        excerpt="Sample isolated source",
        quality="OFFICIAL",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(
            pool.map(
                service.archive,
                [
                    request,
                    request.model_copy(update={"retrieved_at": START + timedelta(seconds=1)}),
                ],
            )
        )
    assert records[0]["id"] == records[1]["id"]
    b = bundle()
    b["account_id"] = aid
    b = json_replace(b, "source", records[0]["id"])
    draft = service.create_draft(ConstraintDraft(bundle=ConstraintBundle.model_validate(b)))
    assert service.list_constraints(aid) == []
    assert "confirmation_digest" not in service.list_drafts(aid)[0]
    assert service.list_drafts(aid)[0]["id"] == draft["draft"]["id"]
    second = service.create_draft(ConstraintDraft(bundle=ConstraintBundle.model_validate(b)))
    with pytest.raises(LedgerError, match="Invalid confirmation"):
        service.commit(
            draft_id=draft["draft"]["id"], confirmation_token="wrong", confirmed_by="test"
        )
    args = dict(
        draft_id=draft["draft"]["id"],
        confirmation_token=draft["confirmation_token"],
        confirmed_by="test",
    )
    assert not service.commit(**args)["idempotent_replay"]
    assert service.commit(**args)["idempotent_replay"]
    candidate = {
        "plan": {
            "instrument_items": [
                {
                    "instrument_code": "CORE01",
                    "instrument_name": "Isolated",
                    "candidate_amount": "40.00",
                }
            ]
        },
        "display_text": "WARNING",
    }
    assessed = service.assess(candidate, account_id=aid, start=START, end=END, channel="CHANNEL_A")
    assert assessed["execution_assessment"]["items"][0]["executable_minor"] == 2000
    assert assessed["plan"] == candidate["plan"]
    assert assessed["execution_assessment"]["items"][0]["archived_sources"]

    with pytest.raises(LedgerError, match="Preview again"):
        service.commit(
            draft_id=second["draft"]["id"],
            confirmation_token=second["confirmation_token"],
            confirmed_by="test",
        )
    expired = service.create_draft(ConstraintDraft(bundle=ConstraintBundle.model_validate(b)))
    clock[0] += timedelta(hours=1)
    with pytest.raises(LedgerError, match="Preview again"):
        service.commit(
            draft_id=expired["draft"]["id"],
            confirmation_token=expired["confirmation_token"],
            confirmed_by="test",
        )
    client = TestClient(create_app(settings))
    before = dump(settings.db_path)
    response = client.get(
        "/v1/weekly-plan-preview",
        params=dict(
            portfolio_id=pid,
            account_id=aid,
            contribution_amount="200",
            as_of_date="2026-07-21",
            period_start=START.isoformat(),
            period_end=END.isoformat(),
            channel="CHANNEL_A",
        ),
    )
    assert response.status_code == 200
    assert dump(settings.db_path) == before
    assert response.json()["data"]["execution_assessment"]["strategy_unchanged"]
    assert client.get("/v1/execution-constraints", params={"account_id": aid}).json()["data"][
        "items"
    ]


def json_replace(value, before, after):
    import json

    return json.loads(json.dumps(value).replace('"' + before + '"', '"' + after + '"'))


def test_unverified_evidence_cannot_approve_a_constraint(tmp_path):
    settings, _, aid, _ = terminal_plan(tmp_path / "db", outcome="SKIPPED")
    service = ExecutionService(ResearchService(settings, now=lambda: START))
    source = service.archive(
        SourceArchive(
            instrument_code="CORE01",
            source_name="Repost",
            source_ref="https://example.com/repost",
            source_lineage="REPOST",
            retrieved_at=START,
            published_date=None,
            data_date=START.date(),
            excerpt="Unverified source",
            quality="REPOST",
        )
    )
    b = json_replace(bundle(), "source", source["id"])
    b["account_id"] = aid
    with pytest.raises(LedgerError, match="Resolve source"):
        service.create_draft(ConstraintDraft(bundle=ConstraintBundle.model_validate(b)))


def test_account_remaining_quota_counts_only_submissions_after_observation():
    b = bundle()
    b["quota_observations"][0].update(observed_at="2026-09-23T10:00:00+08:00", remaining_minor=200)
    orders = [
        dict(
            code="CORE01",
            external_platform="CHANNEL_A",
            requested_amount_minor=700,
            cancelled_amount_minor=0,
            refunded_amount_minor=0,
            submitted_business_date="2026-09-23",
            submitted_at="2026-09-23T09:00:00+08:00",
        ),
        dict(
            code="CORE01",
            external_platform="CHANNEL_A",
            requested_amount_minor=50,
            cancelled_amount_minor=0,
            refunded_amount_minor=0,
            submitted_business_date="2026-09-23",
            submitted_at="2026-09-23T11:00:00+08:00",
        ),
    ]
    result = run(b, now=dt("2026-09-23T12:00:00+08:00"), submitted=orders)
    assert (
        sum(o["amount_minor"] for o in result["schedule"] if o["business_date"] == "2026-09-23")
        == 150
    )
    assert result["executable_minor"] == 1150
    assert result["future_quota_recheck_required"]


@pytest.mark.parametrize("case", ["missing", "stale", "conflict"])
def test_account_quota_unknown_never_becomes_zero_candidate(case):
    b = bundle()
    if case == "missing":
        b["quota_observations"] = []
    elif case == "stale":
        b["quota_observations"][0]["valid_until"] = "2026-09-23T11:00:00+08:00"
    else:
        b["quota_observations"].append(dict(b["quota_observations"][0], remaining_minor=500))
    result = run(b, now=dt("2026-09-23T12:00:00+08:00"))
    assert result["candidate_minor"] == 4000
    assert result["executable_minor"] == 1000
    assert result["unverified_minor"] == 3000


def test_account_capture_keeps_fingerprint_and_cannot_cross_accounts(tmp_path):
    settings, _, aid, _ = terminal_plan(tmp_path / "db", outcome="SKIPPED")
    service = ExecutionService(ResearchService(settings, now=lambda: START))
    fingerprint = "a" * 64
    request = SourceArchive(
        instrument_code="CORE01",
        source_name="Redacted account capture",
        source_ref="attachment:sha256:" + fingerprint,
        source_lineage="USER_ACCOUNT_CAPTURE",
        retrieved_at=START,
        published_date=None,
        data_date=START.date(),
        excerpt="Remaining quota",
        quality="ACCOUNT_OBSERVATION",
        original_sha256=fingerprint,
        facts={"account_id": "another-account"},
    )
    saved = service.archive(request)
    b = json_replace(bundle(), "source", saved["id"])
    b["account_id"] = aid
    with pytest.raises(LedgerError, match="Account capture scope mismatch"):
        service.create_draft(ConstraintDraft(bundle=ConstraintBundle.model_validate(b)))
