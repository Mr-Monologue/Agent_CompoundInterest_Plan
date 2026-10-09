"""Evidence-only shadow research. Never consumed by money, signal or risk engines.

Unknown numerical methods remain unknown. No user-supplied PASS can promote a
model; this first evaluator does not implement probabilistic/ranking validation.
"""

from __future__ import annotations

import hmac
import json
import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.research import ResearchService
from investor_core.scheduler import digest, instant, stamp

Json = dict[str, Any]
C_INPUTS = {"valuation", "fundamentals", "liquidity", "structure", "price"}
D_INPUTS = {"identity", "product", "benchmark", "cost", "delay", "limit"}


class FilterRule(StrictModel):
    slot: str = Field(min_length=1, max_length=80)
    field: str = Field(min_length=1, max_length=80)
    operator: Literal["EQ", "MIN", "MAX"]
    value: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def finite(self) -> FilterRule:
        if self.operator != "EQ":
            try:
                if not Decimal(self.value).is_finite():
                    raise ValueError("finite thresholds required")
            except InvalidOperation as exc:
                raise ValueError("numeric threshold required") from exc
        return self


class InputField(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$", max_length=80)
    value_type: Literal["NUMBER", "INTEGER", "TEXT", "BOOLEAN", "DATE"]


def valid_value(value: Any, value_type: str) -> bool:
    if value is None:
        return False
    if value_type == "BOOLEAN":
        return isinstance(value, bool)
    if value_type == "TEXT":
        return (
            isinstance(value, str)
            and bool(value.strip())
            and value.strip().casefold() not in {"null", "none", "unknown", "n/a", "nan"}
        )
    if value_type == "DATE":
        if not isinstance(value, str):
            return False
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return False
    try:
        number = Decimal(str(value))
        return number.is_finite() and (
            value_type != "INTEGER" or number == number.to_integral_value()
        )
    except InvalidOperation:
        return False


def rule_contract(rule: Json, contract: Json) -> tuple[str, Any]:
    fields = contract.get(rule["slot"], [])
    field = next((f for f in fields if f["name"] == rule["field"]), None)
    if field is None:
        raise ValueError("rule field must be declared in its input dimension")
    kind = field["value_type"]
    if rule["operator"] != "EQ" and kind not in {"NUMBER", "INTEGER"}:
        raise ValueError("MIN/MAX require a numeric input contract")
    value = rule["value"]
    if kind == "BOOLEAN":
        if value.casefold() not in {"true", "false"}:
            raise ValueError("boolean EQ threshold must be true or false")
        value = value.casefold() == "true"
    if not valid_value(value, kind):
        raise ValueError("rule threshold must satisfy its input contract")
    return kind, value


class ModelDefinition(StrictModel):
    model_key: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    model_type: Literal["MACRO_REGIME", "SATELLITE_RANKING"]
    scope: str = Field(pattern=r"^(MARKET|REGION|SECTOR|STYLE):[A-Za-z0-9_.-]+$")
    rationale: str = Field(min_length=1, max_length=2000)
    # A definition is a research proposal, never an approved strategy instance.
    rules: list[FilterRule] = Field(default_factory=list, max_length=100)
    # No default scientific content contract is inferred from a slot name.
    input_contract: dict[str, list[InputField]] = Field(default_factory=dict, max_length=6)

    @model_validator(mode="after")
    def rules_valid(self) -> ModelDefinition:
        if self.model_type == "MACRO_REGIME" and self.rules:
            raise ValueError("season formula not defined; do not infer it from filters")
        if any(r.slot not in D_INPUTS for r in self.rules):
            raise ValueError("unknown candidate input slot")
        slots = C_INPUTS if self.model_type == "MACRO_REGIME" else D_INPUTS
        if set(self.input_contract) - slots:
            raise ValueError("unknown input contract dimension")
        for fields in self.input_contract.values():
            if not fields or len(fields) > 100 or len({f.name for f in fields}) != len(fields):
                raise ValueError("distinct nonempty required fields expected")
        # Empty contracts remain parseable for exact replay of pre-contract models.
        # New registrations still reject rules without contracts in register().
        if self.input_contract:
            contract = {s: [f.model_dump() for f in fs] for s, fs in self.input_contract.items()}
            for rule in self.rules:
                rule_contract(rule.model_dump(), contract)
        identities = [(r.slot, r.field, r.operator) for r in self.rules]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate rule")
        return self


class ShadowInput(StrictModel):
    slot: str = Field(min_length=1, max_length=80)
    evidence_id: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=100)


class ShadowObservation(StrictModel):
    model_id: str
    scope: str
    as_of: datetime
    subjects: list[str] = Field(min_length=1, max_length=100)
    inputs: list[ShadowInput] = Field(default_factory=list, max_length=600)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique(self) -> ShadowObservation:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of requires timezone")
        if len(set(self.subjects)) != len(self.subjects) or any(
            not s.strip() for s in self.subjects
        ):
            raise ValueError("distinct nonempty subjects required")
        keys = [(x.subject, x.slot) for x in self.inputs]
        if len(set(keys)) != len(keys) or any(x.subject not in self.subjects for x in self.inputs):
            raise ValueError("duplicate or out-of-scope input subject")
        return self


class ReviewRequest(StrictModel):
    model_id: str
    target: Literal["OFF", "SHADOW", "ADVISORY", "ACTIVE"]
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class ReviewConfirmation(StrictModel):
    confirmation_token: str
    confirmed_by: str = Field(min_length=1, max_length=100)


def evaluate(spec: Json, request: Json, snapshots: list[Json]) -> Json:
    """Pure evaluation over stored snapshots; never query current/live evidence."""
    required = C_INPUTS if spec["model_type"] == "MACRO_REGIME" else D_INPUTS
    rows = []
    as_of = instant(request["as_of"])
    for subject in sorted(request["subjects"]):
        reasons = []
        values = {}
        for slot in sorted(required):
            refs = [x for x in snapshots if x["subject"] == subject and x["slot"] == slot]
            if not refs:
                reasons.append(slot + ":MISSING")
                continue
            ref = refs[0]
            if ref.get("error"):
                reasons.append(slot + ":" + ref["error"])
                continue
            facts = ref["facts"]
            payload = facts.get("facts", {})
            if not isinstance(payload, dict):
                payload = {}
            facts_scope = payload.get("shadow_scope")
            facts_subject = payload.get("shadow_subject")
            if facts_scope != spec["scope"] or facts_subject != subject:
                reasons.append(slot + ":EVIDENCE_SCOPE_MISMATCH")
            if (
                spec["model_type"] == "SATELLITE_RANKING"
                and ref.get("source_instrument_code") != subject
            ):
                reasons.append(slot + ":SOURCE_IDENTITY_MISMATCH")
            if facts.get("quality") != "OFFICIAL":
                reasons.append(slot + ":QUALITY_UNVERIFIED")
            try:
                # Date-only disclosure is not known at start of its publication day.
                published = datetime.combine(
                    date.fromisoformat(facts["published_date"]),
                    time.max,
                    ZoneInfo(ref["timezone"]),
                )
                retrieved = datetime.fromisoformat(facts["retrieved_at"])
                if retrieved.tzinfo is None:
                    raise ValueError("missing timezone")
                data_date = date.fromisoformat(facts["data_date"])
                if (
                    max(published, retrieved) > as_of
                    or data_date > as_of.astimezone(ZoneInfo(ref["timezone"])).date()
                ):
                    reasons.append(slot + ":NOT_KNOWN_AS_OF")
            except (KeyError, TypeError, ValueError):
                reasons.append(slot + ":TIME_PROVENANCE_MISSING")
            slot_values = payload.get("values", {})
            values[slot] = slot_values if isinstance(slot_values, dict) else {}
            contract = spec.get("input_contract", {}).get(slot)
            if not contract:
                reasons.append(slot + ":INPUT_CONTRACT_UNDEFINED")
            else:
                for field in contract:
                    if not valid_value(values[slot].get(field["name"]), field["value_type"]):
                        reasons.append(slot + "." + field["name"] + ":VALUE_MISSING_OR_INVALID")
        excluded = []
        for rule in spec["rules"]:
            raw = values.get(rule["slot"], {}).get(rule["field"])
            label = rule["slot"] + "." + rule["field"]
            try:
                kind, threshold = rule_contract(rule, spec.get("input_contract", {}))
            except ValueError:
                reasons.append(label + ":RULE_CONTRACT_INVALID")
                continue
            if not valid_value(raw, kind):
                reasons.append(label + ":VALUE_MISSING_OR_INVALID")
                continue
            if kind in {"NUMBER", "INTEGER"}:
                a, b = Decimal(str(raw)), Decimal(str(threshold))
                passed = (
                    a == b
                    if rule["operator"] == "EQ"
                    else a >= b
                    if rule["operator"] == "MIN"
                    else a <= b
                )
            elif kind == "DATE":
                passed = date.fromisoformat(str(raw)) == date.fromisoformat(threshold)
            else:
                passed = raw == threshold
            if not passed:
                excluded.append(label + ":RULE_NOT_MET")
        rows.append(
            dict(
                subject=subject,
                missing=sorted(set(reasons)),
                exclusions=sorted(set(excluded)),
                filter_result="UNKNOWN"
                if reasons
                else "EXCLUDED"
                if excluded
                else "NOT_CONFIGURED"
                if not spec["rules"]
                else "PASS_RESEARCH_ONLY",
            )
        )
    return dict(
        evaluator_version="shadow-evidence-v3",
        input_contract_hash=digest(spec.get("input_contract", {})),
        rows=rows,
        status="INSUFFICIENT_DATA" if any(r["missing"] for r in rows) else "EVIDENCE_COMPLETE",
        dominant_season="UNKNOWN",
        probabilities=None,
        ranking=None,
        money_action=False,
        limitations=[
            "SCOPE_NOT_APPROVED",
            "INPUT_CONTRACT_NOT_REVIEWED",
            "NUMERICAL_METHOD_NOT_VALIDATED",
        ],
        display_text="影子研究: 未批准范围, 不改变策略、资格或金额; 季节概率与排名未实现。",
    )


class ShadowService:
    def __init__(self, research: ResearchService) -> None:
        self.research = research

    @staticmethod
    def _model(c: Any, model_id: str) -> Json:
        row = c.execute("SELECT payload_json FROM shadow_models WHERE id=?", (model_id,)).fetchone()
        if not row:
            raise LedgerError("SHADOW_MODEL_NOT_FOUND", "Unknown shadow model", http_status=404)
        return json.loads(row[0])  # type: ignore[no-any-return]

    @staticmethod
    def _rows(c: Any, model_id: str) -> list[Json]:
        return [
            json.loads(r[0])
            for r in c.execute(
                "SELECT payload_json FROM shadow_records WHERE model_id=? ORDER BY rowid",
                (model_id,),
            )
        ]

    @staticmethod
    def _public(value: Json) -> Json:
        return {k: v for k, v in value.items() if k != "confirmation_digest"}

    def register(self, request: ModelDefinition) -> Json:
        spec = request.model_dump(mode="json")
        spec["rules"] = sorted(spec["rules"], key=lambda r: (r["slot"], r["field"], r["operator"]))
        fingerprint = digest(spec)
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT payload_json FROM shadow_models WHERE model_key=? AND version=?",
                (request.model_key, request.version),
            ).fetchone()
            if row:
                saved = json.loads(row[0])
                legacy_spec = {k: v for k, v in spec.items() if k != "input_contract"}
                legacy_replay = (
                    "input_contract" not in saved["definition"]
                    and not spec["input_contract"]
                    and saved["definition"] == legacy_spec
                    and saved["parameters_hash"] == digest(legacy_spec)
                )
                if saved["parameters_hash"] != fingerprint and not legacy_replay:
                    raise LedgerError("SHADOW_VERSION_CONFLICT", "Create a new model version")
                return saved  # type: ignore[no-any-return]
            for rule in spec["rules"]:
                try:
                    rule_contract(rule, spec["input_contract"])
                except ValueError as exc:
                    raise LedgerError("SHADOW_RULE_CONTRACT_INVALID", str(exc)) from exc
            saved = dict(
                id=str(uuid4()),
                definition=spec,
                parameters_hash=fingerprint,
                mode="SHADOW",
                status="DRAFT",
                created_at=stamp(self.research._now()),
            )
            c.execute(
                "INSERT INTO shadow_models VALUES (?,?,?,?)",
                (
                    saved["id"],
                    request.model_key,
                    request.version,
                    json.dumps(saved),
                ),
            )
        return saved

    def list_models(self) -> Json:
        with self.research._connect() as c:
            ids = [str(r[0]) for r in c.execute("SELECT id FROM shadow_models ORDER BY rowid")]
        return {"items": [self.read(model_id) for model_id in ids], "money_action": False}

    def read(self, model_id: str) -> Json:
        with self.research._connect() as c:
            model = self._model(c, model_id)
            rows = self._rows(c, model_id)
        events = [r for r in rows if r["kind"] == "REVIEW_CONFIRMATION"]
        model["mode"] = events[-1]["target"] if events else "SHADOW"
        model["history"] = [self._public(r) for r in rows]
        model["money_action"] = False
        return model

    @staticmethod
    def _append(c: Any, model_id: str, kind: str, key: str, payload: Json) -> Json:
        request_key = kind + ":" + key
        previous = c.execute(
            "SELECT payload_json FROM shadow_records WHERE request_key=?", (request_key,)
        ).fetchone()
        if previous:
            saved = json.loads(previous[0])
            if saved["model_id"] != model_id or saved["request_hash"] != payload["request_hash"]:
                raise LedgerError("SHADOW_KEY_CONFLICT", "Same key has different immutable input")
            return saved  # type: ignore[no-any-return]
        saved = dict(payload, id=str(uuid4()), kind=kind, model_id=model_id)
        c.execute(
            "INSERT INTO shadow_records VALUES (?,?,?,?,?)",
            (
                saved["id"],
                model_id,
                kind,
                request_key,
                json.dumps(saved),
            ),
        )
        return saved

    def observe(self, request: ShadowObservation) -> Json:
        body = request.model_dump(mode="json", exclude={"idempotency_key"})
        body["subjects"] = sorted(body["subjects"])
        body["inputs"] = sorted(body["inputs"], key=lambda x: (x["subject"], x["slot"]))
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            model = self._model(c, request.model_id)
            previous = c.execute(
                "SELECT payload_json FROM shadow_records WHERE request_key=?",
                ("OBSERVATION:" + request.idempotency_key,),
            ).fetchone()
            if previous:
                saved = json.loads(previous[0])
                if saved["model_id"] != request.model_id or saved["request_hash"] != digest(body):
                    raise LedgerError(
                        "SHADOW_KEY_CONFLICT", "Same key has different immutable input"
                    )
                return saved  # type: ignore[no-any-return]
            spec = model["definition"]
            events = [
                r for r in self._rows(c, request.model_id) if r["kind"] == "REVIEW_CONFIRMATION"
            ]
            if events and events[-1]["target"] == "OFF":
                raise LedgerError("SHADOW_PAUSED", "Shadow observation is paused")
            if request.scope != spec["scope"]:
                raise LedgerError("SHADOW_SCOPE_MISMATCH", "Observation scope differs from model")
            if request.as_of > self.research._now():
                raise LedgerError("SHADOW_FUTURE_AS_OF", "Future observations are forbidden")
            required = C_INPUTS if spec["model_type"] == "MACRO_REGIME" else D_INPUTS
            if any(x.slot not in required for x in request.inputs):
                raise LedgerError("SHADOW_INPUT_SLOT", "Unknown model dimension")
            snapshots = []
            for ref in body["inputs"]:
                row = c.execute(
                    "SELECT * FROM market_research_evidence WHERE id=?", (ref["evidence_id"],)
                ).fetchone()
                if not row:
                    snapshots.append(dict(ref, error="EVIDENCE_NOT_FOUND"))
                else:
                    facts = json.loads(row["facts_json"])
                    metadata = facts.get("facts", {})
                    source_zone = (
                        metadata.get("publication_timezone") if isinstance(metadata, dict) else None
                    )
                    snapshots.append(
                        dict(
                            ref,
                            facts=facts,
                            source_instrument_code=c.execute(
                                "SELECT code FROM instruments WHERE id=?", (row["instrument_id"],)
                            ).fetchone()[0],
                            facts_hash=row["facts_hash"],
                            source_ref=row["source_ref"],
                            created_at=row["created_at"],
                            timezone=source_zone,
                        )
                    )
            output = evaluate(spec, body, snapshots)
            input_hash = digest(
                dict(parameters=model["parameters_hash"], request=body, snapshots=snapshots)
            )
            return self._append(
                c,
                request.model_id,
                "OBSERVATION",
                request.idempotency_key,
                dict(
                    request_hash=digest(body),
                    input_hash=input_hash,
                    input=body,
                    snapshots=snapshots,
                    output=output,
                    created_at=stamp(self.research._now()),
                ),
            )

    def validate(self, model_id: str, observation_id: str) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            model = self._model(c, model_id)
            rows = self._rows(c, model_id)
            observation = next(
                (r for r in rows if r["id"] == observation_id and r["kind"] == "OBSERVATION"), None
            )
            if observation is None:
                raise LedgerError(
                    "SHADOW_OBSERVATION_MISSING", "Observation belongs to another model"
                )
            replay = evaluate(model["definition"], observation["input"], observation["snapshots"])
            checks = dict(
                deterministic_replay=replay == observation["output"],
                input_fingerprint=observation["input_hash"]
                == digest(
                    dict(
                        parameters=model["parameters_hash"],
                        request=observation["input"],
                        snapshots=observation["snapshots"],
                    )
                ),
                parameters_fingerprint=model["parameters_hash"] == digest(model["definition"]),
                input_coverage=replay["status"] == "EVIDENCE_COMPLETE",
                scope_approved=False,
                numerical_method_validated=False,
                stability_validated=False,
                independent_review=False,
                out_of_sample=False,
                baseline_cost_delay_stress=False,
            )
            return self._append(
                c,
                model_id,
                "VALIDATION",
                observation_id + ":" + replay["evaluator_version"],
                dict(
                    request_hash=digest(
                        dict(observation=observation, evaluator_version=replay["evaluator_version"])
                    ),
                    evaluator_version=replay["evaluator_version"],
                    observation_id=observation_id,
                    checks=checks,
                    result="INSUFFICIENT_DATA"
                    if all(
                        checks[k]
                        for k in (
                            "deterministic_replay",
                            "input_fingerprint",
                            "parameters_fingerprint",
                        )
                    )
                    else "FAIL",
                    created_at=stamp(self.research._now()),
                ),
            )

    def _gate(self, c: Any, model_id: str, target: str) -> Json:
        model = self._model(c, model_id)
        rows = self._rows(c, model_id)
        events = [r for r in rows if r["kind"] == "REVIEW_CONFIRMATION"]
        current = events[-1]["target"] if events else "SHADOW"
        validations = [r for r in rows if r["kind"] == "VALIDATION"]
        blockers = []
        if target in {"ADVISORY", "ACTIVE"}:
            blockers = [
                "SCOPE_NOT_APPROVED",
                "NUMERICAL_METHOD_NOT_VALIDATED",
                "STABILITY_NOT_VALIDATED",
                "INDEPENDENT_REVIEW_MISSING",
            ]
            if not validations:
                blockers.append("VALIDATION_MISSING")
            elif not all(validations[-1]["checks"].values()):
                blockers.extend(k.upper() for k, v in validations[-1]["checks"].items() if not v)
        if target == "ACTIVE":
            blockers += ["ACTIVE_INTEGRATION_NOT_IMPLEMENTED", "STRATEGY_APPROVAL_REQUIRED"]
            if current != "ADVISORY":
                blockers.append("NO_DIRECT_SHADOW_TO_ACTIVE")
        return dict(
            current=current,
            target=target,
            blockers=sorted(set(blockers)),
            eligible=not blockers,
            state_hash=digest(
                dict(model=model, records=[r for r in rows if r["kind"] != "REVIEW_DRAFT"])
            ),
            financial_mutation=False,
        )

    def gate(self, model_id: str, target: str) -> Json:
        with self.research._connect() as c:
            return self._gate(c, model_id, target)

    def create_review(self, request: ReviewRequest) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            gate = self._gate(c, request.model_id, request.target)
            if not gate["eligible"]:
                raise LedgerError(
                    "SHADOW_PROMOTION_BLOCKED", "Promotion evidence incomplete", details=gate
                )
            token = secrets.token_urlsafe(32)
            body = request.model_dump(mode="json")
            record = self._append(
                c,
                request.model_id,
                "REVIEW_DRAFT",
                request.idempotency_key,
                dict(
                    request_hash=digest(body),
                    target=request.target,
                    reason=request.reason,
                    state_hash=gate["state_hash"],
                    confirmation_digest=digest(token),
                    expires_at=stamp(
                        self.research._now()
                        + timedelta(minutes=self.research.settings.confirmation_ttl_minutes)
                    ),
                ),
            )
            # Exact-key replay must not disclose a new token that cannot commit.
            result = self._public(record)
            if record["confirmation_digest"] == digest(token):
                result["confirmation_token"] = token
            return result

    def confirm(self, model_id: str, draft_id: str, request: ReviewConfirmation) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._model(c, model_id)
            rows = self._rows(c, model_id)
            draft = next(
                (r for r in rows if r["id"] == draft_id and r["kind"] == "REVIEW_DRAFT"), None
            )
            if draft is None or not hmac.compare_digest(
                draft["confirmation_digest"], digest(request.confirmation_token)
            ):
                raise LedgerError("SHADOW_CONFIRMATION_MISMATCH", "Exact confirmation required")
            existing = next(
                (
                    r
                    for r in rows
                    if r["kind"] == "REVIEW_CONFIRMATION" and r["draft_id"] == draft_id
                ),
                None,
            )
            if existing:
                return existing
            if instant(draft["expires_at"]) <= self.research._now():
                raise LedgerError("SHADOW_CONFIRMATION_EXPIRED", "Preview a new review")
            # Drafts do not themselves change the reviewed state.
            without_drafts = [r for r in rows if r["kind"] != "REVIEW_DRAFT"]
            model = self._model(c, model_id)
            state = digest(dict(model=model, records=without_drafts))
            if state != draft["state_hash"]:
                raise LedgerError("SHADOW_REVIEW_DRIFT", "Model evidence changed; review again")
            gate = self._gate(c, model_id, draft["target"])
            if not gate["eligible"]:
                raise LedgerError("SHADOW_PROMOTION_BLOCKED", "Promotion evidence incomplete")
            return self._append(
                c,
                model_id,
                "REVIEW_CONFIRMATION",
                draft_id,
                dict(
                    request_hash=digest(dict(draft_id=draft_id, target=draft["target"])),
                    draft_id=draft_id,
                    target=draft["target"],
                    confirmed_by=request.confirmed_by,
                    created_at=stamp(self.research._now()),
                    financial_mutation=False,
                ),
            )
