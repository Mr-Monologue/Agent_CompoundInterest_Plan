"""Native mapping approvals and account scope, all temporary SYNTHETIC fixtures."""

import json
from datetime import UTC, datetime

from test_r11_calculators import candidates
from test_r11_complete_simulation import core_mappings
from test_r11_review_fixes import enriched
from test_r11_service import archive_bundle, business_rows, isolated  # noqa: F401

from investor_core.r11_candidates import CandidateInput
from investor_core.r11_core_bindings import capture
from investor_core.r11_service import R11Request


def test_native_mapping_scope_dates_exact_basis_and_immutable_replay(isolated):  # noqa: F811
    db, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service)
    data = candidates("A500")
    for product in data["products"]:
        product["benchmark_research_approved"] = True
    eid = archive_bundle(service, data)
    clock[0] = datetime(2026, 10, 10, 4, tzinfo=UTC)
    before = business_rows(db)
    run = service.observe(
        R11Request(method="A500", bundle_evidence_id=eid, idempotency_key="native-pass", **scope)
    )
    assert run["output"]["mapping_qualified"]
    assert business_rows(db) == before
    assert service.replay("A500", run["id"])["result"] == "PASS"
    parsed = CandidateInput.model_validate(data)
    with service.research._connect() as c:

        def get(portfolio=scope["portfolio_id"], ids=None):
            return capture(c, parsed, portfolio, ids or scope["mapping_ids"], "2020-01-01")

        assert all(not v["blockers"] for v in get()["mappings"].values())
        assert all(
            "MAPPING_SCOPE_MISMATCH" in v["blockers"]
            for v in get("wrong-portfolio")["mappings"].values()
        )
        mid = scope["mapping_ids"]["022463"]
        original = c.execute(
            "SELECT payload_json FROM research_benchmark_mappings WHERE id=?", (mid,)
        ).fetchone()[0]
        for mode, expected in [
            ("draft", "MAPPING_NOT_APPROVED"),
            ("future", "MAPPING_APPROVAL_AFTER_CUTOFF"),
            ("wrong_basis", "MAPPING_IDENTITY_CURRENCY_OR_BASIS_MISMATCH"),
            ("gap", "MAPPING_WINDOW_UNCOVERED"),
            ("composite", "COMPOSITE_MAPPING_NOT_EXACT_SINGLE_PATH"),
        ]:
            changed = json.loads(original)
            part = changed["diagnostic_mapping"][0]
            if mode == "draft":
                changed["status"] = "DRAFT"
            if mode == "future":
                changed["confirmed_at"] = "2026-10-11T00:00:00Z"
            if mode == "wrong_basis":
                part["components"][0]["return_basis"] = "PRICE"
            if mode == "gap":
                part["effective_from"] = "2026-10-09"
            if mode == "composite":
                part["components"][0]["weight_bps"] = 9500
            c.execute(
                "UPDATE research_benchmark_mappings SET payload_json=? WHERE id=?",
                (json.dumps(changed), mid),
            )
            assert expected in get()["mappings"]["022463"]["blockers"]
        # Existing run replays from its snapshot; present approval changes cannot rewrite history.
    assert service.replay("A500", run["id"])["result"] == "PASS"


def test_account_entity_must_belong_to_exact_active_cny_portfolio(isolated):  # noqa: F811
    _, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service, ("003096", "009163"))
    data = enriched(datetime(2026, 10, 10).date())
    with service.research._connect() as c:
        account = c.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        data["replacement_evidence"]["account_ref"] = account
        parsed = CandidateInput.model_validate(data)

        def get(portfolio=scope["portfolio_id"]):
            return capture(c, parsed, portfolio, scope["mapping_ids"], "2020-01-01")

        assert not get()["account_blockers"]
        assert get("wrong")["account_blockers"] == ["CORE_ACCOUNT_SCOPE_MISMATCH"]
        c.execute("UPDATE accounts SET status='ARCHIVED' WHERE id=?", (account,))
        assert get()["account_blockers"] == ["CORE_ACCOUNT_NOT_ACTIVE_CNY"]


def test_four_native_bound_weeks_reach_only_hypothetical_replacement(isolated):  # noqa: F811
    from datetime import time, timedelta

    from test_r11_complete_simulation import bundle, store

    from investor_core.r11_inputs import TZ
    from investor_core.scheduler import digest

    db, _, service, clock = isolated
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service, ("003096", "009163"))
    with service.research._connect() as c:
        account = c.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
    for i in range(4):
        day = datetime(2026, 10, 10).date() + timedelta(weeks=i)
        clock[0] = datetime.combine(day, time(12), TZ)
        body = enriched(day)
        body["replacement_evidence"]["account_ref"] = account
        for platform in body["replacement_evidence"]["platforms"]:
            platform["account_ref"] = account
        for p in body["products"]:
            p["nav"]["points"] = p["nav"]["points"][-260:]
            p["calendar"]["dates"] = p["calendar"]["dates"][-260:]
            p["calendar"]["covered_from"] = p["calendar"]["dates"][0]
            # Core approval, not this false caller flag, governs leading weeks.
            p["benchmark_research_approved"] = False
            fingerprint = digest({k: v for k, v in p.items() if k != "corroboration"})
            p["corroboration"]["primary_product_hash"] = fingerprint
            p["corroboration"]["corroborated_product_hash"] = fingerprint
        for key in ["secondary", "review"]:
            import hashlib

            raw = dict(
                input={k: v for k, v in body.items() if k != "context"},
                publication=clock[0].isoformat(),
                publisher="SYNTHETIC_" + key,
            )
            source = body["context"]["sources"][key]
            source.update(
                url=f"https://example.test/native-{i}-{key}",
                lineage="SYNTHETIC_" + key.upper(),
                published_at=clock[0].isoformat(),
                first_retrieved_at=clock[0].isoformat(),
                publication_precision="INSTANT",
                publication_timezone="Asia/Shanghai",
                document_hash=hashlib.sha256(
                    json.dumps(raw, separators=(",", ":")).encode()
                ).hexdigest(),
            )
            source["archive_id"] = store(
                service,
                raw,
                f"native-{i}-{key}",
                lineage=source["lineage"],
                source_ref=source["url"],
                facts=dict(
                    publication_timezone="Asia/Shanghai",
                    publication_precision="INSTANT",
                    r11_original_format="JSON_UTF8",
                    r11_publication_pointer="/publication",
                    r11_source_binding=dict(
                        published_at=clock[0].isoformat(), first_retrieved_at=clock[0].isoformat()
                    ),
                ),
            )
        eid = bundle(service, body, f"native-replacement-{i}")
        before = business_rows(db)
        run = service.observe(
            R11Request(
                method="MEDICAL",
                bundle_evidence_id=eid,
                idempotency_key=f"native-replacement-{i}",
                **scope,
            )
        )
        assert run["output"]["leading_weeks"] == i + 1, run["output"]
        assert service.replay("MEDICAL", run["id"])["result"] == "PASS"
        assert business_rows(db) == before
    assert run["output"]["replacement"] == "SHADOW_REPLACEMENT_REVIEW"
    assert not run["output"]["money_action"]
    assert business_rows(db) == before
