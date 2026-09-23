"""Evidence-backed execution assessment. Does not allocate money or place orders."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investor_core.ledger import LedgerError
from investor_core.research import ResearchService
from investor_core.scheduler import digest, instant, stamp

Json = dict[str, Any]
TZ = ZoneInfo("Asia/Shanghai")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceArchive(StrictModel):
    instrument_code: str = Field(min_length=1, max_length=40)
    source_name: str = Field(min_length=1, max_length=200)
    source_ref: str = Field(pattern=r"^(https://|attachment:sha256:[a-f0-9]{64}$)", max_length=1000)
    source_lineage: str = Field(min_length=1, max_length=120)
    retrieved_at: datetime
    published_date: date | None
    data_date: date
    excerpt: str = Field(min_length=1, max_length=100000)
    original_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    quality: Literal["OFFICIAL", "ACCOUNT_OBSERVATION", "REPOST", "UNVERIFIED"]
    facts: Json = Field(default_factory=dict)
    actor_ref: str = "codex"

    @model_validator(mode="after")
    def validate_time(self) -> SourceArchive:
        if self.source_ref.startswith("attachment:"):
            if self.quality != "ACCOUNT_OBSERVATION" or not self.facts.get("account_id"):
                raise ValueError("account capture requires its explicit account scope")
            if self.original_sha256 != self.source_ref.removeprefix("attachment:sha256:"):
                raise ValueError("account capture must retain its original content fingerprint")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at requires timezone")
        return self


class LimitRule(StrictModel):
    effective_from: datetime
    effective_to: datetime
    source_ids: list[str] = Field(min_length=1)
    # Explicit None means evidenced absence of a cap, never missing information.
    minimum_order_minor: int = Field(ge=1)
    per_order_minor: int | None = Field(ge=0)
    per_day_minor: int | None = Field(ge=0)
    cumulative_minor: int | None = Field(ge=0)
    cumulative_from: datetime
    cumulative_to: datetime
    quota_scope: Literal["CHANNEL_ACCOUNT", "FUND_ACCOUNT_ALL_CHANNELS"]
    cancelled_orders_consume_quota: bool

    @model_validator(mode="after")
    def intervals(self) -> LimitRule:
        for value in (
            self.effective_from,
            self.effective_to,
            self.cumulative_from,
            self.cumulative_to,
        ):
            if value.tzinfo is None:
                raise ValueError("all boundaries require timezone")
        if self.effective_from >= self.effective_to:
            raise ValueError("empty effective interval")
        if not (
            self.cumulative_from <= self.effective_from < self.effective_to <= self.cumulative_to
        ):
            raise ValueError("quota interval must contain the effective interval")
        return self


class CalendarDay(StrictModel):
    day: date
    exchange_open: bool | None
    fund_subscription_open: bool | None
    channel_accepting: bool | None
    qdii_subscription_open: bool | None
    opens_at: datetime
    cutoff_at: datetime
    exchange_source_ids: list[str] = Field(min_length=1)
    fund_source_ids: list[str] = Field(min_length=1)
    channel_source_ids: list[str] = Field(min_length=1)
    qdii_source_ids: list[str] = Field(default_factory=list)
    confirmation_rule: str = Field(min_length=1)

    @model_validator(mode="after")
    def times(self) -> CalendarDay:
        if self.opens_at.tzinfo is None or self.cutoff_at.tzinfo is None:
            raise ValueError("calendar times require timezone")
        if self.opens_at >= self.cutoff_at:
            raise ValueError("calendar window must be nonempty")
        if any(t.astimezone(TZ).date() != self.day for t in (self.opens_at, self.cutoff_at)):
            raise ValueError("calendar times must belong to the specified Shanghai day")
        return self


class RemainingQuota(StrictModel):
    business_date: date
    observed_at: datetime
    valid_until: datetime
    remaining_minor: int = Field(ge=0)
    quota_scope: Literal["CHANNEL_ACCOUNT", "FUND_ACCOUNT_ALL_CHANNELS"]
    source_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validity(self) -> RemainingQuota:
        if self.observed_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("quota observation requires timezone")
        if self.observed_at >= self.valid_until:
            raise ValueError("quota observation must have a finite validity interval")
        if self.observed_at.astimezone(TZ).date() != self.business_date:
            raise ValueError("quota observation must belong to its business date")
        if self.valid_until > datetime.combine(
            self.business_date + timedelta(days=1), datetime.min.time(), TZ
        ):
            raise ValueError("daily quota observation cannot carry into the next day")
        return self


class ConstraintBundle(StrictModel):
    instrument_code: str = Field(min_length=1)
    share_class: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    qdii: bool
    applicability_source_ids: list[str] = Field(min_length=1)
    limits: list[LimitRule] = Field(min_length=1, max_length=100)
    calendar: list[CalendarDay] = Field(min_length=1, max_length=366)
    quota_observations: list[RemainingQuota] = Field(default_factory=list, max_length=366)
    rationale: str = Field(min_length=1)


class ConstraintDraft(StrictModel):
    bundle: ConstraintBundle
    actor_ref: str = "codex"


def scope(bundle: Json) -> str:
    return digest([bundle[k] for k in ("instrument_code", "account_id", "channel")])


def source_ids(bundle: Json) -> set[str]:
    ids = set(bundle["applicability_source_ids"])
    for observation in bundle.get("quota_observations", []):
        ids.update(observation["source_ids"])
    for rule in bundle["limits"]:
        ids.update(rule["source_ids"])
    for day in bundle["calendar"]:
        for k in (
            "exchange_source_ids",
            "fund_source_ids",
            "channel_source_ids",
            "qdii_source_ids",
        ):
            ids.update(day[k])
    return ids


class QuotaCapture(StrictModel):
    account_id: str
    instrument_code: str
    share_class: str
    channel: str
    observation: RemainingQuota
    actor_ref: str = "codex"


class ExecutionService:
    def __init__(self, research: ResearchService) -> None:
        self.research = research

    def archive(self, request: SourceArchive) -> Json:
        facts = request.model_dump(mode="json", exclude={"actor_ref"})
        facts["kind"] = "EXECUTION_SOURCE_V1"
        facts["excerpt_sha256"] = hashlib.sha256(request.excerpt.encode()).hexdigest()
        # Repeated retrieval is an observation of the same immutable source, not a new fact.
        with self.research._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM market_research_evidence WHERE source_ref=?",
                (request.source_ref,),
            ).fetchall()
            for row in rows:
                old = json.loads(row["facts_json"])
                ignored = {"retrieved_at"}
                if {k: v for k, v in old.items() if k not in ignored} == {
                    k: v for k, v in facts.items() if k not in ignored
                }:
                    return self.research._evidence_data(connection, row, idempotent_replay=True)
        return self.research.record_evidence(
            instrument_code=request.instrument_code,
            evidence_date=request.data_date,
            evidence_type="OTHER",
            source_name=request.source_name,
            source_ref=request.source_ref,
            source_lineage=request.source_lineage,
            facts=facts,
            actor_ref=request.actor_ref,
        )

    def record_quota(self, request: QuotaCapture) -> Json:
        payload = request.model_dump(mode="json")
        observation = payload["observation"]
        if request.observation.observed_at > self.research._now():
            raise LedgerError("EXECUTION_QUOTA_FUTURE", "Cannot record future quota")
        with self.research._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM accounts WHERE id=? AND status='ACTIVE'", (request.account_id,)
            ).fetchone():
                raise LedgerError("EXECUTION_ACCOUNT_INVALID", "Active account required")
            for eid in observation["source_ids"]:
                row = connection.execute(
                    "SELECT e.facts_json, i.code FROM market_research_evidence e "
                    "JOIN instruments i ON i.id=e.instrument_id WHERE e.id=?",
                    (eid,),
                ).fetchone()
                facts = json.loads(row[0]) if row else {}
                scope_facts = facts.get("facts", {})
                if (
                    not row
                    or row["code"] != request.instrument_code
                    or facts.get("quality") != "ACCOUNT_OBSERVATION"
                    or any(
                        scope_facts.get(k) != payload[k]
                        for k in ("account_id", "channel", "share_class")
                    )
                    or scope_facts.get("observed_at") != observation["observed_at"]
                ):
                    raise LedgerError(
                        "EXECUTION_QUOTA_SCOPE",
                        "Capture must match fund/account/channel/class/time",
                    )
            key = digest(payload)
            connection.execute(
                "INSERT OR IGNORE INTO execution_quota_observations VALUES (?,?,?)",
                (key, request.account_id, json.dumps(payload, ensure_ascii=False)),
            )
        return dict(id=key, observation=payload, rule_approval=False)

    def list_quotas(self, account_id: str) -> list[Json]:
        with self.research._connect() as c:
            return [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT payload_json FROM execution_quota_observations WHERE account_id=?",
                    (account_id,),
                )
            ]

    def list_constraints(self, account_id: str) -> list[Json]:
        with self.research._connect() as connection:
            return [
                json.loads(r[0])
                for r in connection.execute(
                    "SELECT payload_json FROM execution_constraints WHERE account_id=?",
                    (account_id,),
                )
            ]

    def list_drafts(self, account_id: str) -> list[Json]:
        with self.research._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM execution_constraint_drafts")
            result = []
            for row in rows:
                draft = json.loads(row[0])
                if draft["bundle"]["account_id"] != account_id:
                    continue
                draft.pop("confirmation_digest", None)
                if (
                    draft["status"] == "PENDING"
                    and instant(draft["expires_at"]) <= self.research._now()
                ):
                    draft["status"] = "EXPIRED"
                result.append(draft)
            return result

    def _validate(self, connection: Any, bundle: Json) -> None:
        if not connection.execute(
            "SELECT 1 FROM accounts WHERE id=? AND status='ACTIVE'", (bundle["account_id"],)
        ).fetchone():
            raise LedgerError("EXECUTION_ACCOUNT_INVALID", "Active account required")
        if not connection.execute(
            "SELECT 1 FROM instruments WHERE code=? AND status='ACTIVE'",
            (bundle["instrument_code"],),
        ).fetchone():
            raise LedgerError("EXECUTION_FUND_INVALID", "Active fund required")
        for observation in bundle.get("quota_observations", []):
            if instant(observation["observed_at"]) > self.research._now():
                raise LedgerError("EXECUTION_QUOTA_FUTURE", "Cannot approve a future observation")
        for eid in source_ids(bundle):
            row = connection.execute(
                "SELECT facts_json FROM market_research_evidence WHERE id=?", (eid,)
            ).fetchone()
            if not row or json.loads(row[0]).get("kind") != "EXECUTION_SOURCE_V1":
                raise LedgerError("EXECUTION_SOURCE_MISSING", "Archive the quoted evidence first")
            evidence = json.loads(row[0])
            if (
                evidence["quality"] == "ACCOUNT_OBSERVATION"
                and evidence["facts"].get("account_id") != bundle["account_id"]
            ):
                raise LedgerError("EXECUTION_ACCOUNT_MISMATCH", "Account capture scope mismatch")
            if evidence["quality"] not in {"OFFICIAL", "ACCOUNT_OBSERVATION"} or evidence[
                "facts"
            ].get("unresolved_conflict"):
                raise LedgerError(
                    "EXECUTION_SOURCE_UNVERIFIED", "Resolve source quality/conflicts first"
                )

    def create_draft(self, request: ConstraintDraft) -> Json:
        bundle = request.bundle.model_dump(mode="json")
        token = secrets.token_urlsafe(32)
        with self.research._connect() as connection:
            self._validate(connection, bundle)
            current = connection.execute(
                "SELECT payload_json FROM execution_constraints WHERE id=?", (scope(bundle),)
            ).fetchone()
            draft = {
                "id": str(uuid4()),
                "bundle": bundle,
                "status": "PENDING",
                "baseline": digest(current[0] if current else None),
                "created_by": request.actor_ref,
                "expires_at": stamp(self.research._now() + timedelta(minutes=15)),
                "confirmation_digest": digest(token),
            }
            connection.execute(
                "INSERT INTO execution_constraint_drafts VALUES (?, ?)",
                (draft["id"], json.dumps(draft)),
            )
        return {
            "draft": {k: v for k, v in draft.items() if k != "confirmation_digest"},
            "confirmation_token": token,
            "effect": "Replace scoped execution evidence; no allocation or trade",
        }

    def commit(self, *, draft_id: str, confirmation_token: str, confirmed_by: str) -> Json:
        with self.research._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM execution_constraint_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if not row:
                raise LedgerError("EXECUTION_DRAFT_NOT_FOUND", "Draft not found", http_status=404)
            draft = json.loads(row[0])
            if not hmac.compare_digest(draft["confirmation_digest"], digest(confirmation_token)):
                raise LedgerError(
                    "EXECUTION_CONFIRMATION_INVALID", "Invalid confirmation", http_status=409
                )
            if draft["status"] == "COMMITTED":
                return {"constraint": draft["committed"], "idempotent_replay": True}
            if instant(draft["expires_at"]) <= self.research._now():
                raise LedgerError("EXECUTION_DRAFT_EXPIRED", "Preview again", http_status=409)
            bundle = draft["bundle"]
            self._validate(connection, bundle)
            key = scope(bundle)
            current = connection.execute(
                "SELECT payload_json FROM execution_constraints WHERE id=?", (key,)
            ).fetchone()
            if digest(current[0] if current else None) != draft["baseline"]:
                raise LedgerError("EXECUTION_STATE_CHANGED", "Preview again", http_status=409)
            value = {
                "id": key,
                "bundle": bundle,
                "confirmed_by": confirmed_by,
                "confirmed_at": stamp(self.research._now()),
                "draft_id": draft_id,
            }
            connection.execute(
                "INSERT OR REPLACE INTO execution_constraints VALUES (?, ?, ?)",
                (key, bundle["account_id"], json.dumps(value)),
            )
            draft.update(status="COMMITTED", committed=value)
            connection.execute(
                "UPDATE execution_constraint_drafts SET payload_json=? WHERE id=?",
                (json.dumps(draft), draft_id),
            )
            return {"constraint": value, "idempotent_replay": False}

    def assess(
        self, preview: Json, *, account_id: str, start: datetime, end: datetime, channel: str | None
    ) -> Json:
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or not start < end <= start + timedelta(days=31)
        ):
            raise LedgerError(
                "EXECUTION_PERIOD_INVALID", "Exact timezone-aware period of 1-31 days required"
            )
        now = self.research._now()
        constraints = self.list_constraints(account_id)
        observations = self.list_quotas(account_id)
        for constraint in constraints:
            bundle = constraint["bundle"]
            captures = [
                o["observation"]
                for o in observations
                if all(
                    o[k] == bundle[k]
                    for k in ("account_id", "instrument_code", "channel", "share_class")
                )
            ]
            bundle["quota_observations"] = bundle.get("quota_observations", []) + captures
        with self.research._connect() as connection:
            # Entire relevant history: no pagination truncation and no inferred transaction date.
            submitted = [
                dict(r)
                for r in connection.execute(
                    "SELECT s.*, i.code FROM external_subscriptions s "
                    "JOIN instruments i ON i.id=s.instrument_id "
                    "WHERE s.account_id=?",
                    (account_id,),
                )
            ]
        items = []
        for candidate in preview.get("plan", {}).get("instrument_items", []):
            code = candidate.get("instrument_code")
            if not code:
                continue
            amount = int(Decimal(candidate["candidate_amount"]) * 100)
            matches = [
                r
                for r in constraints
                if r["bundle"]["instrument_code"] == code
                and (channel is None or r["bundle"]["channel"] == channel)
            ]
            bundle = matches[0]["bundle"] if len(matches) == 1 else None
            item = calculate(amount, bundle, start, end, now, submitted)
            item.update(
                instrument_code=code,
                instrument_name=candidate["instrument_name"],
                approved_constraint_id=matches[0]["id"] if bundle else None,
                source_ids=sorted(source_ids(bundle)) if bundle else [],
            )
            item["archived_sources"] = self.research.list_evidence(instrument_code=code, limit=100)
            items.append(item)
        assessment: Json = {
            "period_start": stamp(start),
            "period_end_exclusive": stamp(end),
            "as_of": stamp(now),
            "items": items,
            "allocation_only": True,
            "strategy_unchanged": True,
            "history_carryover": False,
            "status": "VERIFIED"
            if items and all(i["unverified_minor"] == 0 for i in items)
            else "EXECUTION_CONSTRAINT_UNKNOWN",
        }
        assessment["schedule"] = [
            dict(order, instrument_code=i["instrument_code"])
            for i in items
            for order in i["schedule"]
        ]
        assessment["executable_schedule_available"] = bool(assessment["schedule"])
        result = dict(preview)
        result["execution_assessment"] = assessment
        # Retain allocation and valuation warnings; replace the old placeholder.
        text = preview["display_text"].split("\n\n执行条件核查:")[0]
        lines = [
            text,
            f"\n执行期间: {start.astimezone(TZ).isoformat()} 至 "
            f"{end.astimezone(TZ).isoformat()} (不含)",
            "以下为只读安排, 不冻结、不改分配、不自动提交; WARNING 仍保留。",
            "未来日期安排以执行前复核账户剩余额度为条件; 当日须有有效余额度证据。",
        ]
        for i in items:
            lines.append(
                f"- {i['instrument_code']} {i['instrument_name']}: "
                f"候选 {i['candidate_minor'] / 100:.2f} 元; "
                f"已核实可执行 {i['executable_minor'] / 100:.2f}; "
                f"未核实 {i['unverified_minor'] / 100:.2f}; "
                f"本期不可执行 {i['infeasible_minor'] / 100:.2f}"
            )
            lines.append("  原因: " + (", ".join(i["reasons"]) or "VERIFIED"))
            for evidence in i["archived_sources"]:
                facts = evidence["facts"]
                if facts.get("kind") == "EXECUTION_SOURCE_V1":
                    lines.append(
                        f"  归档来源: {evidence['source_name']}; 数据 {facts['data_date']}; "
                        f"获取 {facts['retrieved_at']}; {facts['quality']}; "
                        f"{evidence['source_ref']}"
                    )
            for order in i["schedule"]:
                lines.append(
                    f"  {instant(order['at']).astimezone(TZ).isoformat()} 至 "
                    f"{instant(order['before']).astimezone(TZ).isoformat()} 截止前: "
                    f"{order['amount_minor'] / 100:.2f} 元"
                )
        lines.append("公开来源已归档不等于账户适用性已确认; 未核实资金保留原归属, 不改投其他基金。")
        result["display_text"] = "\n".join(lines)
        return result


def calculate(
    amount: int,
    bundle: Json | None,
    start: datetime,
    end: datetime,
    now: datetime,
    submitted: list[Json],
) -> Json:
    remaining = amount
    schedule: list[Json] = []
    reasons: set[str] = set()
    unknown = False
    if bundle is None:
        return {
            "candidate_minor": amount,
            "executable_minor": 0,
            "unverified_minor": amount,
            "infeasible_minor": 0,
            "schedule": [],
            "reasons": [
                "ACCOUNT_CHANNEL_APPLICABILITY_NOT_CONFIRMED",
                "LIMIT_VALIDITY_AND_CALENDAR_NOT_CONFIRMED",
            ],
        }
    day = start.astimezone(TZ).date()
    while datetime.combine(day, datetime.min.time(), TZ) < end:
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ)
        if min(day_end, end) <= max(start, now):
            reasons.add("PAST_CUTOFF_OR_OUTSIDE_PERIOD")
            day += timedelta(days=1)
            continue
        calendars = [c for c in bundle["calendar"] if c["day"] == day.isoformat()]
        if len(calendars) != 1:
            unknown = True
            reasons.add("CALENDAR_MISSING_OR_CONFLICTING")
            day += timedelta(days=1)
            continue
        c = calendars[0]
        flags = [c["exchange_open"], c["fund_subscription_open"], c["channel_accepting"]]
        if bundle["qdii"]:
            flags.append(c["qdii_subscription_open"])
            if not c["qdii_source_ids"]:
                flags.append(None)
        if False in flags:
            reasons.add("CLOSED_OR_SUBSCRIPTION_SUSPENDED")
            day += timedelta(days=1)
            continue
        if None in flags:
            unknown = True
            reasons.add("CALENDAR_LAYER_UNKNOWN")
            day += timedelta(days=1)
            continue
        lo, hi = max(start, now, instant(c["opens_at"])), min(end, instant(c["cutoff_at"]))
        if lo >= hi:
            reasons.add("PAST_CUTOFF_OR_OUTSIDE_PERIOD")
            day += timedelta(days=1)
            continue
        boundaries = sorted(
            {lo, hi}
            | {
                instant(r[k])
                for r in bundle["limits"]
                for k in ("effective_from", "effective_to")
                if lo < instant(r[k]) < hi
            }
        )
        boundaries = sorted(
            set(boundaries)
            | {
                instant(o["valid_until"])
                for o in bundle.get("quota_observations", [])
                if lo < instant(o["valid_until"]) < hi
            }
        )
        for at, before in pairwise(boundaries):
            rules = [
                r
                for r in bundle["limits"]
                if instant(r["effective_from"]) <= at and before <= instant(r["effective_to"])
            ]
            if len(rules) != 1:
                unknown = True
                reasons.add("LIMIT_MISSING_OR_CONFLICTING")
                continue
            r = rules[0]
            orders = [
                s
                for s in submitted
                if s["code"] == bundle["instrument_code"]
                and (
                    r["quota_scope"] == "FUND_ACCOUNT_ALL_CHANNELS"
                    or s["external_platform"] == bundle["channel"]
                )
            ]
            if any(
                not s["external_platform"]
                for s in submitted
                if s["code"] == bundle["instrument_code"]
            ):
                unknown = True
                reasons.add("SUBMISSION_CHANNEL_UNKNOWN")
                continue

            def occupied(s: Json, rule: Json = r) -> int:
                value = int(s["requested_amount_minor"])
                return (
                    value
                    if rule["cancelled_orders_consume_quota"]
                    else max(
                        0,
                        value - int(s["cancelled_amount_minor"]) - int(s["refunded_amount_minor"]),
                    )
                )

            daily = sum(
                occupied(s) for s in orders if s["submitted_business_date"] == day.isoformat()
            )
            daily += sum(
                s["amount_minor"] for s in schedule if s["business_date"] == day.isoformat()
            )
            total = sum(
                occupied(s)
                for s in orders
                if instant(r["cumulative_from"])
                <= instant(s["submitted_at"])
                < instant(r["cumulative_to"])
            )
            total += sum(
                s["amount_minor"]
                for s in schedule
                if instant(r["cumulative_from"]) <= instant(s["at"]) < instant(r["cumulative_to"])
            )
            capacity = min(
                remaining,
                remaining if r["per_day_minor"] is None else max(0, r["per_day_minor"] - daily),
                remaining
                if r["cumulative_minor"] is None
                else max(0, r["cumulative_minor"] - total),
            )
            if day == now.astimezone(TZ).date():
                observations = [
                    o
                    for o in bundle.get("quota_observations", [])
                    if o["business_date"] == day.isoformat()
                    and o["quota_scope"] == r["quota_scope"]
                    and instant(o["observed_at"]) <= now
                    and at < instant(o["valid_until"])
                ]
                latest = max((instant(o["observed_at"]) for o in observations), default=None)
                observations = [o for o in observations if instant(o["observed_at"]) == latest]
                if len(observations) != 1:
                    unknown = True
                    reasons.add("CURRENT_ACCOUNT_REMAINING_QUOTA_MISSING_STALE_OR_CONFLICTING")
                    continue
                observation = observations[0]
                # The account observation already includes submissions at/before observed_at.
                after_observation = sum(
                    occupied(o)
                    for o in orders
                    if o["submitted_business_date"] == day.isoformat()
                    and instant(o["submitted_at"]) > instant(observation["observed_at"])
                )
                planned_today = sum(
                    o["amount_minor"] for o in schedule if o["business_date"] == day.isoformat()
                )
                capacity = min(
                    capacity,
                    max(0, observation["remaining_minor"] - after_observation - planned_today),
                )
                before = min(before, instant(observation["valid_until"]))
            cap = capacity if r["per_order_minor"] is None else r["per_order_minor"]
            while min(capacity, cap) >= r["minimum_order_minor"] and remaining:
                value = min(capacity, cap)
                # Leave a valid minimum for the last split when possible.
                tail = capacity - value
                if 0 < tail < r["minimum_order_minor"] and value - tail >= r["minimum_order_minor"]:
                    value -= r["minimum_order_minor"] - tail
                schedule.append(
                    {
                        "at": stamp(at),
                        "before": stamp(before),
                        "business_date": day.isoformat(),
                        "amount_minor": value,
                        "channel": bundle["channel"],
                        "confirmation_rule": c["confirmation_rule"],
                        "source_ids": sorted(source_ids(bundle)),
                    }
                )
                remaining -= value
                capacity -= value
                if len(schedule) > 10000:
                    raise LedgerError(
                        "EXECUTION_TOO_MANY_ORDERS", "Split count exceeds safety bound"
                    )
        day += timedelta(days=1)
    if remaining and not unknown:
        reasons.add("PERIOD_CAPACITY_OR_MINIMUM_ORDER")
    return {
        "candidate_minor": amount,
        "executable_minor": amount - remaining,
        "unverified_minor": remaining if unknown else 0,
        "infeasible_minor": 0 if unknown else remaining,
        "schedule": schedule,
        "future_quota_recheck_required": any(
            date.fromisoformat(order["business_date"]) > now.astimezone(TZ).date()
            for order in schedule
        ),
        "reasons": sorted(reasons),
    }
