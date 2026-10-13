"""Manual decisions only; immutable context and explicit, non-financial confirmation."""

from __future__ import annotations

import hmac
import json
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.notebook import NotebookService
from investor_core.planning import PlanningService
from investor_core.scheduler import digest, instant, stamp
from investor_core.thesis import ThesisConfirmation
from investor_core.weekly_reports import WeeklyReportService

Json = dict[str, Any]


class ResearchReference(StrictModel):
    kind: Literal["CASE", "BENCHMARK", "PEER"]
    record_id: str = Field(min_length=1)


class ManualDecisionDraft(StrictModel):
    portfolio_id: str
    plan_id: str
    plan_revision: int = Field(ge=1)
    intent: Literal["FOLLOW_PLAN", "DEFER", "SKIP", "OTHER"]
    reason_status: Literal["USER_QUOTE", "UNKNOWN"]
    user_quote: str | None = Field(default=None, min_length=1, max_length=4000)
    system_explanation: str | None = Field(default=None, max_length=4000)
    research_references: list[ResearchReference] = Field(default_factory=list, max_length=30)
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = "codex"

    @model_validator(mode="after")
    def consistent(self) -> ManualDecisionDraft:
        if (self.reason_status == "USER_QUOTE") != bool(
            self.user_quote and self.user_quote.strip()
        ):
            raise ValueError("User quotation and unknown reason must remain distinct")
        if self.reason_status == "UNKNOWN" and self.user_quote is not None:
            raise ValueError("Unknown reason cannot contain a quotation")
        keys = [(x.kind, x.record_id) for x in self.research_references]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate research reference")
        return self


class ManualDecisionService:
    def __init__(
        self, notebook: NotebookService, planning: PlanningService, reports: WeeklyReportService
    ) -> None:
        self.notebook = notebook
        self.research = notebook.research
        self.planning = planning
        self.reports = reports

    @staticmethod
    def public(data: Json) -> Json:
        return {k: v for k, v in data.items() if k != "confirmation_digest"}

    def _plan(self, c: Any, plan_id: str, portfolio: str) -> Json:
        row = c.execute(self.planning._plan_query() + " WHERE p.id=?", (plan_id,)).fetchone()
        if not row or row["portfolio_id"] != portfolio:
            raise LedgerError("DECISION_PLAN_SCOPE", "Plan does not belong to this portfolio")
        return self.planning._plan_data(c, row)

    def _reference(self, c: Any, ref: ResearchReference, plan: Json) -> Json:
        tables = {
            "CASE": "research_case_versions",
            "BENCHMARK": "research_benchmark_runs",
            "PEER": "peer_research_runs",
        }
        row = c.execute(f"SELECT * FROM {tables[ref.kind]} WHERE id=?", (ref.record_id,)).fetchone()
        if not row:
            raise LedgerError("DECISION_RESEARCH_MISSING", "Referenced research is unavailable")
        codes = {x["instrument_code"] for x in plan["items"]}
        if ref.kind == "PEER":
            if row["anchor_code"] not in codes:
                raise LedgerError("DECISION_RESEARCH_SCOPE", "Peer anchor is not in this revision")
            value = dict(
                version=row["version"],
                created_at=row["created_at"],
                knowledge_date=row["knowledge_date"],
                inputs=json.loads(row["input_json"]),
                result=json.loads(row["result_json"]),
            )
        else:
            if row["portfolio_id"] != plan["portfolio_id"] or row["instrument_code"] not in codes:
                raise LedgerError("DECISION_RESEARCH_SCOPE", "Research is outside this plan scope")
            value = json.loads(row["payload_json"])
        if instant(value["created_at"]) > self.research._now():
            raise LedgerError("DECISION_FUTURE_EVIDENCE", "Future research cannot be cited")
        # Copy the exact archived version, never a later 'latest' projection.
        ids: set[str] = set()

        def collect(obj: Any) -> None:
            if isinstance(obj, dict):
                for key, item in obj.items():
                    if (
                        key.endswith("evidence_ids")
                        or key in {"admission_sources", "distribution_sources"}
                    ) and isinstance(item, list):
                        ids.update(str(x) for x in item)
                    else:
                        collect(item)
            elif isinstance(obj, list):
                for item in obj:
                    collect(item)

        if ref.kind != "PEER":
            collect(value)
        sources = []
        if ref.kind == "PEER":
            for key, source in value["inputs"]["sources"].items():
                sources.append(
                    dict(
                        id=key,
                        evidence_date=source.get("data_date"),
                        facts_hash=digest(source),
                        facts=source,
                        source_name=source["source_name"],
                        source_ref=source["source_ref"],
                    )
                )
        for eid in sorted(ids):
            source = c.execute(
                "SELECT * FROM market_research_evidence WHERE id=?", (eid,)
            ).fetchone()
            if not source:
                raise LedgerError("DECISION_EVIDENCE_MISSING", "Archived original is missing")
            evidence = self.research._evidence_data(c, source)
            if instant(evidence["created_at"]) > self.research._now():
                raise LedgerError("DECISION_FUTURE_EVIDENCE", "Future evidence cannot be cited")
            sources.append(evidence)
        return dict(
            kind=ref.kind,
            record_id=ref.record_id,
            content=value,
            content_hash=digest(value),
            evidence=sources,
            data_cutoff=(
                value["result"].get("common_research_cutoff")
                if ref.kind == "PEER"
                else value.get("as_of", value.get("input", {}).get("end"))
            ),
            knowledge_date=value.get("knowledge_date"),
            instrument_code=row["anchor_code"] if ref.kind == "PEER" else row["instrument_code"],
            research_version=value.get("version"),
            evidence_cutoffs=[
                dict(
                    evidence_id=e["id"],
                    evidence_date=e["evidence_date"],
                    data_date=e["facts"].get("data_date"),
                    published_date=e["facts"].get("published_date"),
                    retrieved_at=e["facts"].get("retrieved_at"),
                    facts_hash=e["facts_hash"],
                )
                for e in sources
            ],
            quality="WARNING",
            warning="归档研究及原警告保留;不构成投资资格或独立验证",
        )

    def create(self, request: ManualDecisionDraft) -> Json:
        payload = request.model_dump(mode="json")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT payload_json FROM manual_investment_decisions WHERE request_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if row:
                data = json.loads(row[0])
                if data["request_hash"] != digest(payload):
                    raise LedgerError("IDEMPOTENCY_CONFLICT", "Decision key content differs")
                return dict(self.public(data), idempotent_replay=True)
            plan = self._plan(c, request.plan_id, request.portfolio_id)
            if plan["current_revision"] != request.plan_revision:
                raise LedgerError("DECISION_REVISION_CHANGED", "Review the current plan revision")
            references = [self._reference(c, r, plan) for r in request.research_references]
            token = secrets.token_urlsafe(32)
            data = dict(
                payload,
                id=str(uuid4()),
                status="DRAFT",
                request_hash=digest(payload),
                created_at=stamp(self.research._now()),
                expires_at=stamp(self.research._now() + timedelta(hours=24)),
                confirmation_digest=digest(token),
                original_context=dict(
                    plan=plan,
                    research=references,
                    research_status="REFERENCED" if references else "NOT_REFERENCED",
                ),
                time_contract="RECORDED_NOW_NOT_HISTORICAL_KNOWLEDGE",
                money_action=False,
            )
            c.execute(
                "INSERT INTO manual_investment_decisions VALUES (?,?,?,?,?)",
                (
                    data["id"],
                    request.portfolio_id,
                    request.plan_id,
                    request.idempotency_key,
                    json.dumps(data, ensure_ascii=False),
                ),
            )
            return dict(
                self.public(data), confirmation_token=token, display_text=self._decision_text(data)
            )

    @staticmethod
    def _same_revision(data: Json, plan: Json) -> bool:
        original = data["original_context"]["plan"]
        return bool(
            plan["current_revision"] == data["plan_revision"]
            and plan["revision"] == original["revision"]
            and plan["items"] == original["items"]
        )

    def confirm(self, decision_id: str, request: ThesisConfirmation) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            data = self._load(c, decision_id)
            if not hmac.compare_digest(
                data["confirmation_digest"], digest(request.confirmation_token)
            ):
                raise LedgerError("CONFIRMATION_MISMATCH", "Invalid content confirmation")
            if data["status"] == "CONFIRMED":
                return dict(self.public(data), idempotent_replay=True)
            if instant(data["expires_at"]) <= self.research._now():
                raise LedgerError("DRAFT_EXPIRED", "Review a fresh decision draft")
            plan = self._plan(c, data["plan_id"], data["portfolio_id"])
            if not self._same_revision(data, plan):
                raise LedgerError(
                    "DECISION_REVISION_CHANGED", "Plan changed; do not apply old decision"
                )
            data.update(
                status="CONFIRMED",
                confirmed_by=request.confirmed_by,
                recorded_at=stamp(self.research._now()),
            )
            c.execute(
                "UPDATE manual_investment_decisions SET payload_json=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), decision_id),
            )
            return self.public(data)

    def _load(self, c: Any, decision_id: str) -> Json:
        row = c.execute(
            "SELECT payload_json FROM manual_investment_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if not row:
            raise LedgerError("DECISION_NOT_FOUND", "Decision not found", http_status=404)
        result: Json = json.loads(row[0])
        return result

    @staticmethod
    def _decision_text(data: Json) -> str:
        intents = {
            "FOLLOW_PLAN": "计划按此执行",
            "DEFER": "暂缓",
            "SKIP": "不执行",
            "OTHER": "其他",
        }
        lines = [
            f"人工决定: {intents[data['intent']]}; 原计划修订 {data['plan_revision']}; "
            + ("已确认保存" if data["status"] == "CONFIRMED" else "待确认草稿"),
            f"用户原话: {data['user_quote'] or '未知'}",
            f"系统解释: {data['system_explanation'] or '未提供'}",
            f"依据记录时间: {data['created_at']}; 保存时间: {data.get('recorded_at') or '待确认'}",
            "研究引用: 未引用"
            if not data["research_references"]
            else "研究引用: 已引用原版本, WARNING保留",
        ]
        for ref in data["original_context"]["research"]:
            lines.append(
                f"- {ref['instrument_code']} 归档研究 {ref['research_version'] or '独立运行'}: "
                f"研究截止 {ref['data_cutoff'] or '未定义'}; "
                + "证据数据日期 "
                + ", ".join(
                    str(e["data_date"] or e["evidence_date"]) for e in ref["evidence_cutoffs"]
                )
            )
            lines.append(ref["warning"])
        lines.append("按实际保存时间记录, 不追认历史当时已知; 仅保存决定, 不冻结、申购或记账。")
        return "\n".join(lines)

    def read(self, decision_id: str) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN")
            data = self._load(c, decision_id)
            plan = self._plan(c, data["plan_id"], data["portfolio_id"])
            facts = self.reports._facts(c, data["plan_id"])
            reviews = [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT payload_json FROM research_decision_journal WHERE portfolio_id=?",
                    (data["portfolio_id"],),
                )
            ]
            reviews = [r for r in reviews if r.get("manual_decision_id") == decision_id]
            reports = [
                self.reports._report_data(r)
                for r in c.execute(
                    "SELECT * FROM weekly_reports WHERE plan_id=? ORDER BY report_version",
                    (data["plan_id"],),
                )
            ]
        same = self._same_revision(data, plan)
        confirmation_state = data["status"]
        if data["status"] == "DRAFT" and instant(data["expires_at"]) <= self.research._now():
            confirmation_state = "EXPIRED"
        applicable = "SAME_REVISION" if same else "REVISION_CHANGED_REVIEW_REQUIRED"
        if data["status"] != "CONFIRMED":
            applicable = "UNCONFIRMED"
        stages = []
        for item in facts["items"]:
            subs = item["subscriptions"]
            submitted = len({x["subscription_id"] for x in subs})
            confirmations = len({x["confirmation_id"] for x in subs if x["confirmation_id"]})
            booked = len(item["transactions"])
            stages.append(
                dict(
                    instrument_code=item["instrument_code"],
                    submissions=submitted,
                    confirmations=confirmations,
                    transactions=booked,
                    unexecuted_amount=item["unexecuted_amount"],
                )
            )
        applicability_text = {
            "SAME_REVISION": "对应原修订, 不等于已授权执行",
            "REVISION_CHANGED_REVIEW_REQUIRED": "计划已改版, 原决定须重新审阅",
            "UNCONFIRMED": "决定尚未确认, 不适用",
        }[applicable]
        unexecuted = max(
            Decimal(facts["planned_amount"]) - Decimal(facts["executed_amount"]), Decimal(0)
        )
        lines = [
            self._decision_text(data),
            "草稿已过期, 须重新预览确认" if confirmation_state == "EXPIRED" else "",
            f"当前计划: {plan['status']};修订 {plan['current_revision']};"
            f"决定适用性 {applicability_text}",
            "以下执行属于该计划,改版后不能归因于旧决定。"
            if not same
            else "以下为该计划实际执行事实。",
            f"已记账: {facts['executed_amount']} 元;未执行: {unexecuted:.2f} 元;"
            f"放弃: {facts['abandoned_amount']} 元",
            f"周报: {'已生成正式报告' if reports else '待完成'};"
            f"研究复盘: {'已有记录' if reviews else '待完成'}",
            "零投入不等于零收益; 估值缺失与原WARNING保留。",
            *[
                f"- {x['instrument_code']}: "
                f"申购 {str(x['submissions']) + '笔' if x['submissions'] else '待完成'}; "
                f"份额确认 {str(x['confirmations']) + '笔' if x['confirmations'] else '待完成'}; "
                f"记账 {str(x['transactions']) + '笔' if x['transactions'] else '待完成'}; "
                f"未执行 {x['unexecuted_amount']} 元"
                for x in stages
            ],
        ]
        return dict(
            decision=self.public(data),
            applicability=applicable,
            confirmation_state=confirmation_state,
            current_plan=plan,
            execution=facts,
            stages=stages,
            reports=reports,
            reviews=reviews,
            query_only=True,
            display_text="\n".join(lines),
        )

    def list(self, portfolio_id: str, account_id: str, plan_id: str | None = None) -> Json:
        with self.research._connect() as c:
            rows = c.execute(
                "SELECT d.id FROM manual_investment_decisions d "
                "JOIN investment_plans p ON p.id=d.plan_id "
                "WHERE d.portfolio_id=? AND p.account_id=? "
                "AND (? IS NULL OR p.id=?) ORDER BY d.rowid",
                (portfolio_id, account_id, plan_id, plan_id),
            ).fetchall()
        items = [self.read(r[0]) for r in rows]
        return dict(
            items=items,
            query_only=True,
            display_text="\n\n".join(x["display_text"] for x in items)
            or "暂无记录: 尚无已保存人工决定;未创建任何记录。",
        )
