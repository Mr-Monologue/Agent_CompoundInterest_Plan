"""Explicit research thesis governance. Never imported by allocation or risk engines."""

from __future__ import annotations

import hmac
import json
import secrets
from datetime import date, timedelta
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.notebook import NotebookService
from investor_core.scheduler import digest, instant, stamp

Json = dict[str, Any]


class EvidenceWatch(StrictModel):
    evidence_id: str
    valid_until: date | None = None
    basis: str = Field(min_length=1)


class ThesisDraft(StrictModel):
    portfolio_id: str
    instrument_code: str
    case_version: int = Field(ge=1)
    expected_decision_id: str | None
    action: Literal["ACTIVATE", "INVALIDATE"]
    evidence_watches: list[EvidenceWatch] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = "codex"

    @model_validator(mode="after")
    def unique(self) -> ThesisDraft:
        if len({x.evidence_id for x in self.evidence_watches}) != len(self.evidence_watches):
            raise ValueError("Duplicate evidence watch")
        return self


class ThesisConfirmation(StrictModel):
    confirmation_token: str
    confirmed_by: str = Field(min_length=1)


class ThesisObservation(StrictModel):
    portfolio_id: str
    instrument_code: str
    case_version: int = Field(ge=1)
    kind: Literal["SUPPORTS", "CONTRADICTS", "INVALIDATION_OBSERVED", "DATA_GAP"]
    evidence_ids: list[str] = Field(min_length=1)
    observed_on: date
    explanation: str = Field(min_length=1)
    invalidation_condition: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = "codex"


class ThesisService:
    def __init__(self, notebook: NotebookService) -> None:
        self.notebook = notebook
        self.research = notebook.research

    def _today(self) -> date:
        return self.research._now().astimezone(ZoneInfo(self.research.settings.timezone)).date()

    @staticmethod
    def _public(value: Json) -> Json:
        return {k: v for k, v in value.items() if k != "confirmation_digest"}

    @staticmethod
    def _source(value: Json) -> Json:
        # Immutable fingerprint retains provenance without copying entire price series.
        result = {
            k: value[k]
            for k in (
                "id",
                "source_name",
                "source_ref",
                "source_lineage",
                "evidence_date",
                "created_at",
                "facts_hash",
            )
        }
        result["facts"] = {
            k: v
            for k, v in value["facts"].items()
            if k
            in {
                "published_date",
                "data_date",
                "retrieved_at",
                "original_sha256",
                "excerpt_sha256",
                "quality",
                "excerpt",
            }
        }
        return result

    @staticmethod
    def _changes(before: Json, after: Json) -> list[str]:
        changes = [
            k
            for k in ("return_driver", "maper", "counter_evidence", "unresolved_questions")
            if before.get(k) != after.get(k)
        ]
        changes += [
            k
            for k in before["thesis"].keys() | after["thesis"].keys()
            if before["thesis"].get(k) != after["thesis"].get(k)
        ]
        return sorted(changes)

    def _rows(self, c: Any, table: str, portfolio: str, code: str) -> list[Json]:
        # Table names are internal constants only.
        return [
            json.loads(row[0])
            for row in c.execute(
                f"SELECT payload_json FROM {table} "
                "WHERE portfolio_id=? AND instrument_code=? ORDER BY rowid",
                (portfolio, code),
            )
        ]

    def _case(self, c: Any, portfolio: str, code: str, version: int) -> Json:
        row = c.execute(
            "SELECT payload_json FROM research_case_versions "
            "WHERE portfolio_id=? AND instrument_code=? AND version=?",
            (portfolio, code, version),
        ).fetchone()
        if not row:
            raise LedgerError("THESIS_CASE_MISSING", "Referenced research version does not exist")
        return dict(json.loads(row[0]))

    def _context(self, c: Any, portfolio: str, code: str, version: int) -> Json:
        self.notebook._scope(c, portfolio, code)
        case = self._case(c, portfolio, code, version)
        actions = self._rows(c, "research_thesis_actions", portfolio, code)
        approved = [x for x in actions if x["status"] == "CONFIRMED"]
        observations = self._rows(c, "research_thesis_observations", portfolio, code)
        ids = set(case["evidence_ids"]) | {
            e for o in observations if o["case_version"] == version for e in o["evidence_ids"]
        }
        sources = [
            self._source(self.research._evidence_data(c, row))
            for row in c.execute(
                "SELECT e.* FROM market_research_evidence e "
                "JOIN instruments i ON i.id=e.instrument_id "
                "WHERE i.code=? ORDER BY e.rowid",
                (code,),
            )
        ]
        used = [x for x in sources if x["id"] in ids]
        refs = {s["source_ref"] for s in used}
        return dict(
            case=case,
            decision={
                k: approved[-1][k]
                for k in ("id", "action", "case_version", "effective_at", "evidence_watches")
            }
            if approved
            else None,
            latest_case_version=c.execute(
                "SELECT MAX(version) FROM research_case_versions "
                "WHERE portfolio_id=? AND instrument_code=?",
                (portfolio, code),
            ).fetchone()[0],
            observations=observations,
            sources=[s for s in sources if s["source_ref"] in refs or s["id"] in ids],
            observed_on=self._today().isoformat(),
        )

    def _replay(self, c: Any, table: str, payload: Json) -> Json | None:
        row = c.execute(
            f"SELECT payload_json FROM {table} WHERE request_key=?", (payload["idempotency_key"],)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if data["request_hash"] != digest(payload):
            raise LedgerError("IDEMPOTENCY_CONFLICT", "Request key content differs")
        return dict(self._public(data), idempotent_replay=True)

    def _insert(self, c: Any, table: str, data: Json) -> None:
        c.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            (
                data["id"],
                data["portfolio_id"],
                data["instrument_code"],
                data["idempotency_key"],
                json.dumps(data, ensure_ascii=False),
            ),
        )

    def create(self, request: ThesisDraft) -> Json:
        payload = request.model_dump(mode="json")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            replay = self._replay(c, "research_thesis_actions", payload)
            if replay:
                return replay
            context = self._context(
                c, request.portfolio_id, request.instrument_code, request.case_version
            )
            previous = context["decision"]
            if request.expected_decision_id != (previous["id"] if previous else None):
                raise LedgerError("THESIS_STATE_STALE", "Read current confirmed thesis")
            if (
                request.action == "ACTIVATE"
                and request.case_version != context["latest_case_version"]
            ):
                raise LedgerError("THESIS_VERSION_STALE", "Review the latest research draft")
            if request.action == "INVALIDATE" and (
                not previous
                or previous["action"] != "ACTIVATE"
                or previous["case_version"] != request.case_version
            ):
                raise LedgerError(
                    "THESIS_NOT_ACTIVE", "Only the current active thesis can be invalidated"
                )
            if any(
                w.evidence_id not in context["case"]["evidence_ids"]
                for w in request.evidence_watches
            ):
                raise LedgerError("THESIS_EVIDENCE_SCOPE", "Watch must reference the selected case")
            token = secrets.token_urlsafe(32)
            data = dict(
                payload,
                id=str(uuid4()),
                status="DRAFT",
                created_at=stamp(self.research._now()),
                expires_at=stamp(self.research._now() + timedelta(hours=24)),
                confirmation_digest=digest(token),
                request_hash=digest(payload),
                context_hash=digest(context),
                reviewed_context=context,
                money_action=False,
                original_buy_reason=None,
            )
            self._insert(c, "research_thesis_actions", data)
            return dict(self._public(data), confirmation_token=token)

    def confirm(self, action_id: str, request: ThesisConfirmation) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT payload_json FROM research_thesis_actions WHERE id=?", (action_id,)
            ).fetchone()
            if not row:
                raise LedgerError("THESIS_DRAFT_MISSING", "Thesis draft not found", http_status=404)
            data = json.loads(row[0])
            if not hmac.compare_digest(
                data["confirmation_digest"], digest(request.confirmation_token)
            ):
                raise LedgerError("CONFIRMATION_MISMATCH", "Invalid content confirmation")
            if data["status"] == "CONFIRMED":
                return dict(self._public(data), idempotent_replay=True)
            if instant(data["expires_at"]) <= self.research._now():
                raise LedgerError("DRAFT_EXPIRED", "Review a fresh draft before confirming")
            context = self._context(
                c, data["portfolio_id"], data["instrument_code"], data["case_version"]
            )
            if digest(context) != data["context_hash"]:
                raise LedgerError(
                    "THESIS_CONTEXT_CHANGED",
                    "Evidence, version or confirmed state changed; review a new draft",
                )
            data.update(
                status="CONFIRMED",
                confirmed_by=request.confirmed_by,
                effective_at=stamp(self.research._now()),
            )
            c.execute(
                "UPDATE research_thesis_actions SET payload_json=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), action_id),
            )
            return self._public(data)

    def observe(self, request: ThesisObservation) -> Json:
        payload = request.model_dump(mode="json")
        if request.observed_on > self._today():
            raise LedgerError("THESIS_FUTURE", "Future observations are not evidence")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            replay = self._replay(c, "research_thesis_observations", payload)
            if replay:
                return replay
            self.notebook._scope(c, request.portfolio_id, request.instrument_code)
            case = self._case(
                c, request.portfolio_id, request.instrument_code, request.case_version
            )
            self.notebook._evidence(c, request.instrument_code, set(request.evidence_ids))
            if (
                request.kind == "INVALIDATION_OBSERVED"
                and request.invalidation_condition not in case["thesis"]["invalidation_conditions"]
            ):
                raise LedgerError(
                    "THESIS_CONDITION_UNKNOWN", "Reference an exact existing invalidation condition"
                )
            data = dict(
                payload,
                id=str(uuid4()),
                request_hash=digest(payload),
                created_at=stamp(self.research._now()),
                money_action=False,
            )
            self._insert(c, "research_thesis_observations", data)
            return data

    def read(self, portfolio: str, code: str) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN")
            self.notebook._scope(c, portfolio, code)
            actions = self._rows(c, "research_thesis_actions", portfolio, code)
            confirmed = [x for x in actions if x["status"] == "CONFIRMED"]
            decision = confirmed[-1] if confirmed else None
            latest = c.execute(
                "SELECT MAX(version) FROM research_case_versions "
                "WHERE portfolio_id=? AND instrument_code=?",
                (portfolio, code),
            ).fetchone()[0]
            version = decision["case_version"] if decision else latest
            context = self._context(c, portfolio, code, version) if version else None
            candidate = self._case(c, portfolio, code, latest) if latest else None
            journal = [
                json.loads(row[0])
                for row in c.execute(
                    "SELECT payload_json FROM research_decision_journal "
                    "WHERE portfolio_id=? ORDER BY rowid",
                    (portfolio,),
                )
            ]
            journal = [j for j in journal if j["instrument_code"] == code]
        active = decision and decision["action"] == "ACTIVATE"
        case = context["case"] if context else None
        reasons: list[Json] = []
        gaps = ["历史买入理由未知;当前提出的论点不代表当年的意图。"]
        sources = context["sources"] if context else []
        ids = set(case["evidence_ids"]) if case else set()
        selected = [s for s in sources if s["id"] in ids]
        watches = {w["evidence_id"]: w for w in decision["evidence_watches"]} if decision else {}
        for source in selected:
            watch = watches.get(source["id"])
            if (
                watch
                and watch["valid_until"]
                and self._today() > date.fromisoformat(watch["valid_until"])
            ):
                reasons.append(
                    dict(
                        code="EVIDENCE_EXPIRED",
                        evidence_ids=[source["id"]],
                        basis=watch["basis"],
                        valid_until=watch["valid_until"],
                    )
                )
            if not watch or not watch["valid_until"]:
                gaps.append("证据有效期未指定,不推断永久有效: " + source["source_name"])
            fingerprint = (
                source.get("facts", {}).get("original_sha256")
                or source.get("facts", {}).get("excerpt_sha256")
                or source.get("facts_hash")
            )
            for newer in sources:
                other = (
                    newer.get("facts", {}).get("original_sha256")
                    or newer.get("facts", {}).get("excerpt_sha256")
                    or newer.get("facts_hash")
                )
                if (
                    newer["source_ref"] == source["source_ref"]
                    and newer["id"] != source["id"]
                    and fingerprint
                    and other
                    and fingerprint != other
                    and sources.index(newer) > sources.index(source)
                ):
                    reasons.append(
                        dict(
                            code="SOURCE_CHANGED",
                            evidence_ids=[source["id"], newer["id"]],
                            basis="同一来源归档内容指纹变化,需核对含义,不自动判断矛盾",
                        )
                    )
        observations = context["observations"] if context else []
        for observation in observations:
            if observation["case_version"] == version and observation["kind"] != "SUPPORTS":
                reasons.append(
                    dict(
                        code=observation["kind"],
                        evidence_ids=observation["evidence_ids"],
                        observation_id=observation["id"],
                        basis=observation["explanation"],
                        condition=observation["invalidation_condition"],
                    )
                )
        if case:
            gaps.extend(case["unresolved_questions"])
            profile = case["return_driver"]
            if profile["effective_to"] and self._today() > date.fromisoformat(
                profile["effective_to"]
            ):
                reasons.append(
                    dict(
                        code="PROFILE_EXPIRED",
                        evidence_ids=profile["evidence_ids"],
                        valid_until=profile["effective_to"],
                        basis="已确认内容中收益来源画像的适用期结束",
                    )
                )
            if self._today() < date.fromisoformat(profile["effective_from"]):
                reasons.append(
                    dict(
                        code="PROFILE_NOT_YET_VALID",
                        evidence_ids=profile["evidence_ids"],
                        effective_from=profile["effective_from"],
                        basis="画像声明的适用期尚未开始",
                    )
                )
        if not selected:
            gaps.append("缺少引用证据,不能证明持有解释")
        state = (
            "INVALIDATED"
            if decision and not active
            else "REVIEW_REQUIRED"
            if active and reasons
            else "ACTIVE_RESEARCH"
            if active
            else "UNAPPROVED"
        )
        pending = bool(latest and (not decision or latest != version))
        changes = []
        if decision and decision["expected_decision_id"]:
            prior = next(x for x in confirmed if x["id"] == decision["expected_decision_id"])
            before = prior["reviewed_context"]["case"]
            after = decision["reviewed_context"]["case"]
            changes = self._changes(before, after)
            if prior["evidence_watches"] != decision["evidence_watches"]:
                changes.append("evidence_watches")
        candidate_changes = self._changes(case, candidate) if case and candidate else []
        lines = [
            f"{code} 持仓论点 | {state} | 仅研究,不改变投资金额",
            f"已确认论点版本: {version if decision else '无'};最新草稿版本: {latest or '无'}",
            "历史买入理由: 未知;以下为现在提出的研究论点。",
        ]
        if case:
            lines.append(
                (
                    "已确认研究解释: "
                    if active
                    else "历史失效解释: "
                    if decision
                    else "待确认研究假设: "
                )
                + case["thesis"]["proposed_why_hold"]
            )
            lines += [
                "支持/研究 [" + x["kind"] + "]: " + x["text"] + "; 限制: " + x["limitation"]
                for xs in case["maper"].values()
                for x in xs
            ]
            lines += ["反证: " + x["text"] for x in case["counter_evidence"]]
        lines += ["复核: " + x["code"] + " — " + x["basis"] for x in reasons]
        lines += ["数据不足: " + g for g in dict.fromkeys(gaps)]
        if pending:
            lines.append("待办: 审阅具体论点草稿,内容确认后方可研究生效")
        if reasons:
            lines.append("待办: 核对所列证据,补新版本或明确确认失效;不自动卖出")
        if journal:
            entry = journal[-1]
            lines.append(
                f"最近复盘绑定论点版本 {entry['thesis_version']}: "
                f"过程 {entry['process_quality']}, 执行 {entry['execution_quality']}, "
                f"结果 {entry['outcome_quality']}, 逻辑一致性 {entry['logic_match']}"
            )
            lines.append("复盘待核实: " + "; ".join(entry["unknowns"]))
        else:
            lines.append("复盘日志尚缺; 不由盈亏反推过程质量")
        if candidate_changes:
            lines.append("候选相对已确认版本变化: " + ", ".join(candidate_changes))
        for source in selected:
            lines.append(
                "证据来源: "
                + source["source_name"]
                + " | 数据 "
                + source["evidence_date"]
                + " | "
                + source["source_ref"]
            )
        if changes:
            lines.append("较前次确认变化: " + ", ".join(changes))
        return dict(
            instrument_code=code,
            status=state,
            active_case=case if active else None,
            candidate_version=latest,
            candidate_case=candidate,
            candidate_changes=candidate_changes,
            candidate_pending=pending,
            decision=self._public(decision) if decision else None,
            history=[self._public(x) for x in actions],
            observations=observations,
            decision_journal=journal,
            review_reasons=reasons,
            sources=sources,
            gaps=list(dict.fromkeys(gaps)),
            changed_fields=changes,
            original_buy_reason=None,
            money_action=False,
            data_quality="WARNING",
            display_text="\n".join(lines),
        )
