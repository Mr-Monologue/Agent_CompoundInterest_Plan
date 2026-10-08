"""Shadow-only research endpoints; no strategy, portfolio or ledger mutation."""

from typing import Any, Literal

from fastapi import FastAPI

from investor_core.r11_governance import (
    GovernanceConfirmation,
    GovernanceDraft,
    R11Governance,
    ReceiptRequest,
    RegistrationRequest,
    ValidationRequest,
)
from investor_core.r11_service import R11Request, R11Service
from investor_core.r11_validation import ValidationWindow, evidence_summary
from investor_core.research import ResearchService
from investor_core.shadow_models import (
    ModelDefinition,
    ReviewConfirmation,
    ReviewRequest,
    ShadowObservation,
    ShadowService,
)


def register_shadow_routes(app: FastAPI, research: ResearchService) -> None:
    service = ShadowService(research)
    r11 = R11Service(research)
    governance = R11Governance(r11)

    def response(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "data": data,
            "meta": {"schema_version": "1.0", "data_quality": "WARNING"},
            "warnings": ["SHADOW_ONLY", "NUMERICAL_METHOD_NOT_VALIDATED"],
        }

    @app.get("/v1/shadow-models")
    def list_models() -> dict[str, Any]:
        return response(service.list_models())

    @app.post("/v1/shadow-models")
    def register(request: ModelDefinition) -> dict[str, Any]:
        return response(service.register(request))

    @app.get("/v1/shadow-models/{model_id}")
    def read(model_id: str) -> dict[str, Any]:
        return response(service.read(model_id))

    @app.post("/v1/shadow-observations")
    def observe(request: ShadowObservation) -> dict[str, Any]:
        return response(service.observe(request))

    @app.post("/v1/shadow-models/{model_id}/observations/{observation_id}/validate")
    def validate(model_id: str, observation_id: str) -> dict[str, Any]:
        return response(service.validate(model_id, observation_id))

    @app.get("/v1/shadow-models/{model_id}/promotion-check")
    def gate(
        model_id: str, target: Literal["OFF", "SHADOW", "ADVISORY", "ACTIVE"] = "ADVISORY"
    ) -> dict[str, Any]:
        return response(service.gate(model_id, target))

    @app.post("/v1/shadow-reviews")
    def review(request: ReviewRequest) -> dict[str, Any]:
        return response(service.create_review(request))

    @app.post("/v1/shadow-models/{model_id}/reviews/{draft_id}/confirm")
    def confirm(model_id: str, draft_id: str, request: ReviewConfirmation) -> dict[str, Any]:
        return response(service.confirm(model_id, draft_id, request))

    @app.post("/v1/r11/research-runs")
    def r11_observe(request: R11Request) -> dict[str, Any]:
        return response(r11.observe(request))

    @app.get("/v1/r11/research-runs/{method}")
    def r11_runs(method: Literal["C", "MEDICAL", "A500"]) -> dict[str, Any]:
        return response({"items": r11.runs(method), "money_action": False})

    @app.get("/v1/r11/research-runs/{method}/{run_id}/replay")
    def r11_replay(method: Literal["C", "MEDICAL", "A500"], run_id: str) -> dict[str, Any]:
        return response(r11.replay(method, run_id))

    @app.post("/v1/r11/validation-preview")
    def r11_validation(window: ValidationWindow) -> dict[str, Any]:
        records = r11.runs(window.method)
        replays = {r["id"]: r11.replay(window.method, r["id"])["result"] == "PASS" for r in records}
        return response(evidence_summary(window, records, replays))

    @app.post("/v1/r11/registrations")
    def r11_registration(request: RegistrationRequest) -> dict[str, Any]:
        return response(governance.register(request))

    @app.post("/v1/r11/validations")
    def r11_validate(request: ValidationRequest) -> dict[str, Any]:
        return response(governance.validate(request))

    @app.post("/v1/r11/receipts")
    def r11_receipt(request: ReceiptRequest) -> dict[str, Any]:
        return response(governance.attach(request))

    @app.post("/v1/r11/reviews")
    def r11_review(request: GovernanceDraft) -> dict[str, Any]:
        return response(governance.draft(request))

    @app.post("/v1/r11/reviews/{draft_id}/confirm")
    def r11_confirm(draft_id: str, request: GovernanceConfirmation) -> dict[str, Any]:
        return response(governance.confirm(draft_id, request))

    @app.get("/v1/r11/{method}/promotion-check")
    def r11_promotion_check(
        method: Literal["C", "MEDICAL", "A500"],
        target: Literal["OFF", "SHADOW", "ADVISORY", "ACTIVE"] = "ADVISORY",
        validation_id: str | None = None,
    ) -> dict[str, Any]:
        return response(governance.gate(method, target, validation_id))

    @app.post("/v1/r11/{method}/assess")
    def r11_assess(method: Literal["C", "MEDICAL", "A500"], idempotency_key: str) -> dict[str, Any]:
        return response(governance.assess(method, idempotency_key))
