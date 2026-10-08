"""Complete E success path in SYNTHETIC domain, without real F or model promotion."""

import hashlib
import json
from datetime import UTC, datetime, time, timedelta

from fastapi.testclient import TestClient
from test_r11_governance import diagnostic_record, window
from test_r11_service import isolated  # noqa: F401

from investor_core.api.app import create_app
from investor_core.benchmarks import BenchmarkService, MappingApproval, MappingDraft
from investor_core.execution import ExecutionService, SourceArchive
from investor_core.ledger import LedgerService
from investor_core.notebook import NotebookService
from investor_core.r11_governance import R11Governance, RegistrationRequest
from investor_core.r11_inputs import TZ
from investor_core.r11_provenance import leaves
from investor_core.scheduler import digest


def store(
    service, body, key, *, lineage="SYNTHETIC_REVIEW", source_ref=None, facts=None, code="CORE01"
):
    text = json.dumps(body, separators=(",", ":"))
    assert len(text) <= 100000
    return ExecutionService(service.research).archive(
        SourceArchive(
            instrument_code=code,
            source_name="SYNTHETIC TEST ORIGINAL ONLY",
            source_ref=source_ref or "https://example.test/synthetic/" + key,
            source_lineage=lineage,
            retrieved_at=service.research._now(),
            published_date=service.research._now().astimezone(TZ).date(),
            data_date=service.research._now().date(),
            excerpt=text,
            original_sha256=hashlib.sha256(text.encode()).hexdigest(),
            quality="OFFICIAL",
            facts=dict(dataset_kind="SYNTHETIC", **(facts or {})),
        )
    )["id"]


def core_mappings(service, codes=("022463", "022424")):
    """Create and specifically confirm native mappings only in the temporary test DB."""
    ledger = LedgerService(service.research.settings)
    with service.research._connect() as c:
        portfolio = c.execute("SELECT id FROM portfolios LIMIT 1").fetchone()[0]
    result = {}
    for code in codes:
        ledger.create_instrument(code=code, name="SYNTHETIC " + code, role="SATELLITE")
        eid = store(service, dict(synthetic_mapping=code), "mapping/" + code, code=code)
        period = dict(
            effective_from="2020-01-01",
            observed_on="2026-10-08",
            components=[
                dict(
                    name="SYNTHETIC research path",
                    provider="SYNTHETIC",
                    code="000510CNY010"
                    if code in {"022463", "022424"}
                    else "SYNTHETIC_PUBLISHED_PATH",
                    currency="CNY",
                    return_basis="TOTAL_RETURN",
                    weight_bps=10000,
                    evidence_ids=[eid],
                    availability="test only",
                )
            ],
            evidence_ids=[eid],
            method="DAILY_REBALANCED",
            limitation="SYNTHETIC ONLY",
        )
        native = BenchmarkService(NotebookService(service.research))
        draft = native.create(
            MappingDraft(
                portfolio_id=portfolio,
                instrument_code=code,
                expected_previous_version=0,
                idempotency_key="mapping/" + code,
                official_disclosures=[period],
                diagnostic_mapping=[period],
                rationale="SYNTHETIC ONLY",
                limitations=["SYNTHETIC ONLY"],
            )
        )
        approved = native.approve(
            draft["id"],
            MappingApproval(
                confirmation_token=draft["confirmation_token"], confirmed_by="SYNTHETIC TEST"
            ),
        )
        result[code] = approved["id"]
    return dict(portfolio_id=portfolio, mapping_ids=result)


def bundle(service, data, key):
    src = data["context"]["sources"]["official"]
    publication = service.research._now().isoformat()
    raw = dict(
        input={k: v for k, v in data.items() if k != "context"},
        publication=publication,
        publisher="SYNTHETIC_PRIMARY",
    )
    original = json.dumps(raw, separators=(",", ":"))
    src.update(
        published_at=publication,
        first_retrieved_at=publication,
        publication_precision="INSTANT",
        publication_timezone="Asia/Shanghai",
        url="https://example.test/synthetic/" + key,
        lineage="SYNTHETIC_PRIMARY",
        document_hash=hashlib.sha256(original.encode()).hexdigest(),
    )
    src["archive_id"] = store(
        service,
        raw,
        key,
        lineage=src["lineage"],
        facts=dict(
            publication_timezone="Asia/Shanghai",
            publication_precision="INSTANT",
            r11_original_format="JSON_UTF8",
            **(
                {
                    "r11_constituent_collection": {
                        "pointer": "/input/members",
                        "code_pointer": "/code",
                    }
                }
                if "members" in data
                else {}
            ),
            r11_publication_pointer="/publication",
            r11_source_binding=dict(published_at=publication, first_retrieved_at=publication),
        ),
    )
    bindings = {path: dict(source="official", pointer="/input" + path) for path in leaves(data)}
    if "members" in data:
        bindings["/members"] = dict(
            source="official", pointer="/input/members", code_pointer="/code"
        )
    return store(
        service,
        {"synthetic_bundle": key},
        key + "/bundle",
        facts=dict(r11_input=data, r11_bindings=bindings),
    )


def engineering_artifacts(service, report):
    result = {}
    for name in [
        "full_regression",
        "ubuntu_ci",
        "windows_ci",
        "migration",
        "idempotency",
        "six_step_isolation",
    ]:
        original = dict(
            artifact_type=name,
            dataset_kind="SYNTHETIC",
            engine_hash=report["engine_hash"],
            result="PASS",
            exit_code=0,
            passed=1,
            failed=0,
            unexpected_skips=0,
            command="SYNTHETIC CONTRACT FIXTURE - NOT ACTUAL CI",
            repository="Mr-Monologue/Agent_CompoundInterest_Plan",
            platform=name.removesuffix("_ci"),
            head_sha="a" * 40,
        )
        result[name] = store(
            service,
            original,
            name,
            source_ref="https://github.com/Mr-Monologue/Agent_CompoundInterest_Plan/actions/runs/SYNTHETIC-ONLY"
            if name.endswith("_ci")
            else None,
        )
    return result


def independent_artifacts(service, report):
    checked = report["forward_ids"] + report["history_ids"][:10]
    runs = {r["id"]: r for r in service.runs("A500")}
    observations = [
        dict(
            run_id=key,
            input_hash=digest(runs[key]["input"]),
            output_hash=runs[key]["output_hash"],
            result="PASS",
        )
        for key in checked
    ]
    pairs = []
    for key in checked:
        primary = runs[key]["evidence"]["sources"]["official"]
        raw = json.loads(json.loads(primary["facts_json"])["excerpt"])
        raw["publisher"] = "SYNTHETIC_SECONDARY"
        secondary = store(service, raw, key + "/secondary", lineage="SYNTHETIC_SECONDARY")
        proof = store(
            service,
            dict(
                primary_lineage="SYNTHETIC_PRIMARY",
                secondary_lineage="SYNTHETIC_SECONDARY",
                independent_upstream=True,
                reviewer="synthetic-reviewer",
            ),
            key + "/origin",
        )
        pairs.append(
            dict(
                primary_id=primary["id"],
                secondary_id=secondary,
                origin_review_id=proof,
                fields="IDENTICAL_POINTERS",
            )
        )
    return {
        name: store(
            service,
            dict(
                artifact_type=name,
                dataset_kind="SYNTHETIC",
                result="PASS",
                engine_hash=report["engine_hash"],
                observations=observations,
                pairs=pairs,
            ),
            name,
        )
        for name in [
            "source_independence",
            "source_value_reconciliation",
            "recomputed_observations",
        ]
    }


def test_complete_simulated_evidence_advisory_confirmation_downgrade_and_fresh_restore(
    isolated,  # noqa: F811
    monkeypatch,
):
    _, settings, service, clock = isolated
    monkeypatch.setattr("investor_core.api.shadow.R11Service", lambda research: service)
    periods = window()
    assert periods.dataset_kind == "SYNTHETIC"
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    scope = core_mappings(service)
    registration = R11Governance(service).register(
        RegistrationRequest(window=periods, idempotency_key="sim-register")
    )
    with TestClient(create_app(settings)) as client:
        for kind, start, count in [
            ("H", periods.history_start, 52),
            ("F", periods.forward_start, 13),
        ]:
            for i in range(count):
                day = start + timedelta(weeks=i)
                data = diagnostic_record(day, kind, i)["input"]
                assert data["context"]["dataset_kind"] == "SYNTHETIC"
                if kind == "F":
                    clock[0] = datetime.combine(day, time(11, 59), TZ)
                evidence_id = bundle(service, data, f"{kind}/{i}")
                if kind == "F":
                    clock[0] = datetime.combine(day, time(12), TZ)
                response = client.post(
                    "/v1/r11/research-runs",
                    json=dict(
                        **scope,
                        method="A500",
                        bundle_evidence_id=evidence_id,
                        idempotency_key=f"sim-{kind}-{i}",
                    ),
                )
                assert response.status_code == 200, response.text
                run = response.json()["data"]
                assert run["output"]["status"] == "CALCULATED_SHADOW", run["output"]["gaps"]
                assert run["output"]["extraction"]["status"] == "MATCHED"
        response = client.post(
            "/v1/r11/validations",
            json=dict(registration_id=registration["id"], idempotency_key="sim-validation"),
        )
        assert response.status_code == 200, response.text
        validation = response.json()["data"]
        report = validation["report"]
        assert report["simulation_only"]
        assert all(report["summary"]["computed_checks"].values()), report["summary"][
            "computed_checks"
        ]
        assert all(report["additional_checks"].values()), report["additional_checks"]
        assert report["summary"]["forward"]["valid_weeks"] == 13
        for kind, artifacts in [
            ("ENGINEERING", engineering_artifacts(service, report)),
            ("INDEPENDENT_REVIEW", independent_artifacts(service, report)),
        ]:
            original = dict(
                receipt_type=kind,
                dataset_kind="SYNTHETIC",
                report_hash=validation["report_hash"],
                engine_hash=report["engine_hash"],
                definition_hash=report["definition_hash"],
                reviewer="synthetic-reviewer",
                implementer="synthetic-author",
                unresolved_high=0,
                unresolved_medium=0,
                reviewed_forward_ids=report["forward_ids"],
                reviewed_history_ids=report["history_ids"][:10],
                artifacts=artifacts,
            )
            eid = store(service, original, kind + "/receipt")
            attached = client.post(
                "/v1/r11/receipts",
                json=dict(validation_id=validation["id"], evidence_id=eid, idempotency_key=kind),
            )
            assert attached.status_code == 200, attached.text
        params = dict(dataset_kind="SYNTHETIC", validation_id=validation["id"])
        gate = client.get("/v1/r11/A500/promotion-check", params=params).json()["data"]
        assert gate["eligible"] and gate["simulation_only"], gate
        real = client.get(
            "/v1/r11/A500/promotion-check", params={"validation_id": validation["id"]}
        ).json()["data"]
        assert not real["eligible"] and real["current"] == "UNREGISTERED"

        def confirm(target, key):
            response = client.post(
                "/v1/r11/reviews",
                json=dict(
                    method="A500",
                    dataset_kind="SYNTHETIC",
                    target=target,
                    validation_id=validation["id"],
                    reason="SYNTHETIC LIFECYCLE TEST ONLY",
                    idempotency_key=key,
                ),
            )
            assert response.status_code == 200, response.text
            draft = response.json()["data"]
            response = client.post(
                f"/v1/r11/reviews/{draft['id']}/confirm",
                json=dict(
                    confirmation_token=draft["confirmation_token"], confirmed_by="synthetic-test"
                ),
            )
            assert response.status_code == 200, response.text
            return response.json()["data"]

        event = confirm("ADVISORY", "sim-promote")
        assert event["simulation_only"] and event["target"] == "ADVISORY"
        # Advance the test clock only: source staleness downgrades simulated state.
        clock[0] += timedelta(days=7)
        assessed = client.post(
            "/v1/r11/A500/assess", params=dict(dataset_kind="SYNTHETIC", idempotency_key="stale")
        )
        assert assessed.status_code == 200, assessed.text
        assert "OBSERVATION_STALE" in assessed.json()["data"]["critical"]
        assert (
            client.get("/v1/r11/A500/promotion-check", params=params).json()["data"]["current"]
            == "SHADOW"
        )
        assert confirm("OFF", "sim-pause")["target"] == "OFF"
        blocked = client.post(
            "/v1/r11/reviews",
            json=dict(
                method="A500",
                dataset_kind="SYNTHETIC",
                target="ADVISORY",
                validation_id=validation["id"],
                reason="blocked while paused",
                idempotency_key="blocked",
            ),
        )
        assert blocked.status_code == 400
        assert confirm("SHADOW", "sim-resume")["target"] == "SHADOW"
        assert (
            client.get("/v1/r11/A500/promotion-check", params=params).json()["data"]["current"]
            == "SHADOW"
        )
        assert (
            client.get("/v1/r11/A500/promotion-check").json()["data"]["current"] == "UNREGISTERED"
        )

        # An old eligible report cannot bypass present staleness even in SHADOW.
        stale_gate = client.get("/v1/r11/A500/promotion-check", params=params).json()["data"]
        assert not stale_gate["eligible"] and "OBSERVATION_STALE" in stale_gate["blockers"]

        def fresh_week(index, swap=False):
            day = periods.forward_start + timedelta(weeks=index)
            clock[0] = datetime.combine(day, time(12), TZ)
            data = diagnostic_record(day, "F", index)["input"]
            if swap:
                a, b = data["products"]
                for pa, pb in zip(a["nav"]["points"], b["nav"]["points"], strict=True):
                    pa["value"], pb["value"] = pb["value"], pa["value"]
            eid = bundle(service, data, f"F/{index}")
            response = client.post(
                "/v1/r11/research-runs",
                json=dict(
                    **scope, method="A500", bundle_evidence_id=eid, idempotency_key=f"sim-F-{index}"
                ),
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["output"]["mapping_qualified"]
            return response.json()["data"]

        fresh_week(13)
        assert not client.get("/v1/r11/A500/promotion-check", params=params).json()["data"][
            "eligible"
        ]
        response = client.post(
            "/v1/r11/validations",
            json=dict(registration_id=registration["id"], idempotency_key="fresh-validation"),
        )
        assert response.status_code == 200, response.text
        validation = response.json()["data"]
        report = validation["report"]
        assert report["summary"]["forward"]["valid_weeks"] == 14
        assert all(report["additional_checks"].values()), report["additional_checks"]
        for kind, artifacts in [
            ("ENGINEERING", engineering_artifacts(service, report)),
            ("INDEPENDENT_REVIEW", independent_artifacts(service, report)),
        ]:
            original = dict(
                receipt_type=kind,
                dataset_kind="SYNTHETIC",
                report_hash=validation["report_hash"],
                engine_hash=report["engine_hash"],
                definition_hash=report["definition_hash"],
                reviewer="synthetic-reviewer",
                implementer="synthetic-author",
                unresolved_high=0,
                unresolved_medium=0,
                reviewed_forward_ids=report["forward_ids"],
                reviewed_history_ids=report["history_ids"][:10],
                artifacts=artifacts,
            )
            eid = store(service, original, "fresh/" + kind + "/receipt")
            response = client.post(
                "/v1/r11/receipts",
                json=dict(
                    validation_id=validation["id"], evidence_id=eid, idempotency_key="fresh/" + kind
                ),
            )
            assert response.status_code == 200, response.text
        params["validation_id"] = validation["id"]
        assert confirm("ADVISORY", "sim-repromote")["target"] == "ADVISORY"
        # Actual recalculated winners flip four times, then fail for a second week.
        for index in range(14, 19):
            fresh_week(index, swap=index in {14, 16})
            response = client.post(
                "/v1/r11/A500/assess",
                params=dict(dataset_kind="SYNTHETIC", idempotency_key=f"stability-{index}"),
            )
            assert response.status_code == 200, response.text
            health = response.json()["data"]
            assert not health["critical"], health
            current = client.get("/v1/r11/A500/promotion-check", params=params).json()["data"]
            assert current["current"] == ("SHADOW" if index == 18 else "ADVISORY")
        assert health["stability_failure_weeks"] == 2
        assert confirm("SHADOW", "stability-fresh-shadow")["target"] == "SHADOW"
        assert (
            client.get("/v1/r11/A500/promotion-check").json()["data"]["current"] == "UNREGISTERED"
        )
