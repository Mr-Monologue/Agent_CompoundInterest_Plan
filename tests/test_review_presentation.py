from copy import deepcopy

from fastapi.testclient import TestClient
from test_daily_client import dump
from test_holding_review import capture, setup

from investor_core.api.app import create_app
from investor_core.review_presentation import disclosure_sections, group_reasons, window_label


def test_summary_details_and_baseline_readonly_preserve_inputs(tmp_path):
    settings, pid, aid, _, svc, _ = setup(tmp_path)
    before = dump(settings.db_path)
    original = svc.build(pid, aid)
    web = TestClient(create_app(settings))
    q = dict(portfolio_id=pid, account_id=aid)
    summary = web.get("/v1/holding-review", params=q).json()["data"]
    details = web.get("/v1/holding-review", params=dict(q, view="DETAIL")).json()["data"]
    assert summary["items"] == details["items"]
    assert summary["input_hash"] == original["input_hash"]
    assert details["display_text"] == summary["detail_text"]
    assert len(summary["display_text"].splitlines()) == len(original["items"]) + 2
    assert "http" not in summary["display_text"]
    for item, raw in zip(summary["items"], original["items"], strict=True):
        assert item["windows"] == raw["windows"]
        assert item["thesis"] == raw["thesis"]
        assert item["comparison"] == raw["comparison"]
        for actual, expected in zip(item["reasons"], raw["reasons"], strict=True):
            assert {k: v for k, v in actual.items() if k != "task"} == expected
        for w in item["windows"]:
            assert window_label(w) in details["display_text"]
            assert f"{w['fund_return_pct']:.2f}%" in details["display_text"]
            if w["fund_max_drawdown_pct"] is not None:
                assert f"{w['fund_max_drawdown_pct']:.2f}%" in details["display_text"]
    url = "/v1/holding-review-baseline-preview"
    proposal = web.get(url, params=q).json()["data"]
    assert proposal == web.get(url, params=q).json()["data"]
    assert {k: v for k, v in proposal["proposed_snapshot"].items() if k != "observed_on"} == {
        k: v for k, v in original.items() if k != "observed_on"
    }
    assert proposal["observed_on"] == summary["observed_on"]
    assert svc.baseline_preview(pid, aid)["proposed_snapshot"] == original
    assert proposal["proposed_task_count"] == sum(len(i["reasons"]) for i in original["items"])
    assert sum(proposal["category_counts"].values()) == proposal["proposed_task_count"]
    assert not proposal["writes_performed"] and not proposal["money_action"]
    assert dump(settings.db_path) == before
    saved = svc.capture(capture(svc, pid, aid))
    after = dump(settings.db_path)
    reused = svc.baseline_preview(pid, aid)
    assert reused["action"] == "REUSE_EXISTING" and reused["proposed_snapshot"] is None
    assert reused["existing_snapshot"]["id"] == saved["snapshot_id"]
    assert reused["proposed_task_count"] == 0 and not any(c["changed"] for c in reused["changes"])
    assert dump(settings.db_path) == after


def test_grouping_retains_windows_and_limit_cannot_prove_product_type(tmp_path):
    _, pid, aid, _, svc, _ = setup(tmp_path)
    item = next(
        i
        for i in svc.build(pid, aid)["items"]
        if any(r["kind"] == "RELATIVE_LAG" for r in i["reasons"])
    )
    reason = deepcopy(next(r for r in item["reasons"] if r["kind"] == "RELATIVE_LAG"))
    reason["reason_key"] = "different-window"
    reason["basis"]["window"]["start"] = "2020-01-01"
    item["reasons"].append(reason)
    original = deepcopy(item)
    groups = group_reasons(item)
    lag = next(g for g in groups if g["kind"] == "RELATIVE_LAG")
    assert len(lag["reason_keys"]) == 2 and len(lag["windows"]) == 2
    assert item == original
    item["comparison"]["disclosed_mandate"] = [
        dict(text="QDII 每日累计申购不超过5元", evidence_ids=["limit"])
    ]
    item["comparison"]["product_labels"] = [dict(label="QDII", source=dict(id="limit"))]
    separated = disclosure_sections(item)
    assert not separated["product_types"] and not separated["investment_scope"]
    assert separated["purchase_disclosures"] == item["comparison"]["disclosed_mandate"]
    assert not separated["purchase_schedule_effect"]
