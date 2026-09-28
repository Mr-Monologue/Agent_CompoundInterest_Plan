"""D1: evidence-bound holding review, isolated from money and approval engines."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field

from investor_core.benchmarks import BenchmarkService
from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.market_data import MarketDataService
from investor_core.research_summary import DRAWDOWN_LABEL, summarize_fund
from investor_core.scheduler import digest, stamp
from investor_core.thesis import ThesisService

Json = dict[str, Any]
RULE_VERSION = "holding-review-v1"
CATEGORY_LABELS = {
    "EVIDENCE_RECONCILIATION": "先核对证据差异",
    "OBSERVED_REVIEW": "复核已观察结果",
    "APPROVAL_GAP": "待配置确认",
    "DATA_GAP": "仅缺证据",
    "CONTEXT_OBSERVATION": "背景事实",
}
ELIGIBILITY_LABELS = {
    "NO_WINDOW": "无可比窗口",
    "CANDIDATE_ONLY": "候选映射,仅研究比较",
    "LIMITED_RESEARCH": "已批准研究参考,适用性有限",
}
TASK_LABELS = {
    "OPEN": "待处理",
    "PROPOSED_NOT_SAVED": "待办建议未保存",
    "REQUEST_EVIDENCE": "已记录补证请求",
    "DEFERRED": "已记录暂缓",
    "REVIEWED_WITH_LIMITATIONS": "已复核,限制仍在",
}
THESIS_LABELS = {
    "UNAPPROVED": "无已确认论点",
    "ACTIVE_RESEARCH": "研究论点已确认",
    "REVIEW_REQUIRED": "已确认论点需复核",
    "INVALIDATED": "论点已确认失效",
}


def semantic(value: Any) -> Any:
    """Ignore execution metadata, retaining evidence identities and business versions."""
    if isinstance(value, dict):
        return {
            k: semantic(v)
            for k, v in value.items()
            if k
            not in {
                "run_id",
                "idempotency_key",
                "request_hash",
                "created_at",
                "expires_at",
                "actor_ref",
                "display_text",
            }
        }
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


class ReviewCapture(StrictModel):
    portfolio_id: str
    account_id: str
    expected_input_hash: str
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = "codex"


class ReviewHandling(StrictModel):
    portfolio_id: str
    account_id: str
    expected_event_id: str | None
    outcome: Literal["REQUEST_EVIDENCE", "DEFERRED", "REVIEWED_WITH_LIMITATIONS"]
    explanation: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = "codex"


def fund_review(code: str, name: str, record: Json, today: date) -> Json:
    record = dict(record, runs=[r for r in record["runs"] if r["end"] <= today.isoformat()])
    summary = summarize_fund(record, today=today)
    approved = [m for m in record["versions"] if m["status"] == "APPROVED_RESEARCH"]
    mapping = (approved or record["versions"] or [None])[-1]
    thesis = record["thesis"]
    case = thesis["active_case"] or thesis["candidate_case"]
    sources = {e["id"]: ThesisService._source(e) for e in record["evidence"]}
    claims = [c for cs in case["maper"].values() for c in cs] if case else []
    reasons: list[Json] = []

    def add(
        kind: str,
        category: str,
        text: str,
        basis: Json,
        ids: list[str],
        dates: list[str],
        next_step: str,
        identity: str = "",
    ) -> None:
        refs = [sources[e] for e in sorted(set(ids)) if e in sources]
        key = digest([code, kind, identity])
        reason = dict(
            reason_key=key,
            kind=kind,
            category=category,
            text=text,
            basis=basis,
            data_dates=sorted(set(dates)),
            sources=refs,
            unresolved_source_ids=sorted(set(ids) - sources.keys()),
            next_step=next_step,
            rule_version=RULE_VERSION,
        )
        reason["evidence_version"] = digest(semantic(reason))
        reasons.append(reason)

    runs = {r["id"]: r for r in record["runs"]}
    for w in summary["windows"]:
        r = runs[w["run_id"]]
        basis = dict(
            window=w,
            mapping_version=summary["mapping_version"],
            mapping_approved=summary["mapping_approved"],
            input_fingerprint=digest(semantic(r["input"])),
            fund_return_basis=r["input"]["fund"]["return_basis"],
            benchmark_periods=mapping["diagnostic_mapping"] if mapping else [],
            fee_treatment="NAV already net of internal fees; no second deduction",
        )
        if w["excess_percentage_points"] < 0:
            add(
                "RELATIVE_LAG",
                "OBSERVED_REVIEW",
                "该观察窗口落后对应基准;不是卖出信号或选股Alpha结论",
                basis,
                summary["evidence_ids"],
                [w["end"]],
                "结合已确认论点复核该窗口及成本、归因限制",
                w["start"] + "/" + w["end"],
            )
    signs = {
        1 if w["excess_percentage_points"] > 0 else -1 if w["excess_percentage_points"] < 0 else 0
        for w in summary["windows"]
    }
    if -1 in signs and 1 in signs:
        add(
            "WINDOW_SENSITIVITY",
            "OBSERVED_REVIEW",
            "领先和落后随窗口变化,不能只选有利端点",
            {"windows": summary["windows"]},
            summary["evidence_ids"],
            [w["end"] for w in summary["windows"]],
            "同时阅读全部已覆盖窗口",
        )
    if "DISCLOSED_RETURN_MISMATCH" in summary["warnings"]:
        add(
            "DISCLOSURE_CONFLICT",
            "EVIDENCE_RECONCILIATION",
            "重算与披露存在差异,原因尚未证实",
            {
                "reconciliations": list(
                    {
                        digest(semantic(r.get("reconciliation"))): r.get("reconciliation")
                        for r in runs.values()
                    }.values()
                ),
                "warnings": summary["warnings"],
            },
            summary["evidence_ids"],
            [w["end"] for w in summary["windows"]],
            "核对原始序列、日期、舍入及分红口径",
        )
    if thesis["status"] == "REVIEW_REQUIRED":
        add(
            "THESIS_REVIEW",
            "EVIDENCE_RECONCILIATION",
            "已确认论点触发证据复核",
            {
                "decision": thesis["decision"],
                "triggers": thesis["review_reasons"],
                "observations": thesis["observations"],
            },
            case["evidence_ids"] if case else [],
            [case["as_of"]] if case else [],
            "核对触发证据;另行确认论点修订或失效",
        )
    coverage_gaps = list(summary["gaps"])
    if not summary["windows"]:
        coverage_gaps.append("NO_COMPARABLE_WINDOW")
    if not summary["mapping_approved"]:
        add(
            "MAPPING_PENDING",
            "APPROVAL_GAP",
            "研究映射尚未批准,仅候选比较",
            {"mapping": mapping},
            summary["evidence_ids"],
            [],
            "审阅具体研究映射;处理待办不构成批准",
        )
    if coverage_gaps:
        add(
            "COVERAGE_GAP",
            "DATA_GAP",
            "比较数据不完整,不等于表现不佳",
            {"gaps": coverage_gaps, "missing": summary["missing_observations"]},
            summary["evidence_ids"],
            [],
            "按缺失日期、身份或口径补证,不填零",
        )
    topics = {}
    for topic, words in {
        "fees": ("费用", "管理费", "托管费", "服务费"),
        "concentration": ("集中", "前十大", "权益占", "持仓权重"),
    }.items():
        matched = [
            c
            for c in claims
            if any(word in c["text"] for word in words)
            and c["kind"] in {"PUBLIC_FACT", "COUNTER_EVIDENCE"}
        ]
        topics[topic] = matched
        if matched:
            for c in matched:
                add(
                    topic.upper(),
                    "CONTEXT_OBSERVATION",
                    c["text"],
                    {"claim": c},
                    c["evidence_ids"],
                    [c["as_of"]],
                    "结合当期披露核对;不推断实时结构或超标",
                    digest(c["text"]),
                )
        else:
            add(
                topic.upper() + "_UNKNOWN",
                "DATA_GAP",
                "费用资料未完整覆盖" if topic == "fees" else "集中度资料未完整覆盖",
                {"case_version": case["version"] if case else None},
                [],
                [],
                "补齐有日期和来源的官方披露",
            )
    mandate = case["maper"]["M"] if case else []
    # Literal source labels only; do not infer formal product class from its name.
    labels = []
    mandate_ids = {eid for c in mandate for eid in c["evidence_ids"]}
    for e in sources.values():
        if e["id"] not in mandate_ids:
            continue
        excerpt = e["facts"].get("excerpt", "")
        for label in ("债券型", "混合型", "股票型", "指数型", "QDII"):
            if label in excerpt:
                labels.append(
                    dict(label=label, source=e, basis="LITERAL_DISCLOSURE_NOT_PEER_ELIGIBILITY")
                )
    if not labels:
        coverage_gaps.append("FORMAL_PRODUCT_CLASS_NOT_STRUCTURED; see disclosed mandate")
    reasons.sort(
        key=lambda r: (
            {
                "EVIDENCE_RECONCILIATION": 0,
                "OBSERVED_REVIEW": 1,
                "APPROVAL_GAP": 2,
                "DATA_GAP": 3,
                "CONTEXT_OBSERVATION": 4,
            }[r["category"]],
            r["reason_key"],
        )
    )
    return dict(
        instrument_code=code,
        instrument_name=name,
        comparison=dict(
            scope="OWN_HISTORICAL_BENCHMARK_ONLY",
            peer_ranking_eligible=False,
            product_labels=labels,
            disclosed_mandate=mandate,
            mapping_approved=summary["mapping_approved"],
            mapping_version=summary["mapping_version"],
            official_disclosures=mapping["official_disclosures"] if mapping else [],
            diagnostic_periods=mapping["diagnostic_mapping"] if mapping else [],
            eligibility="NO_WINDOW"
            if not summary["windows"]
            else "CANDIDATE_ONLY"
            if not summary["mapping_approved"]
            else "LIMITED_RESEARCH",
            gaps=coverage_gaps,
            limitations=summary["limitations"],
        ),
        windows=summary["windows"],
        drawdown_label=DRAWDOWN_LABEL,
        topics=topics,
        reasons=reasons,
        warnings=summary["warnings"],
        thesis=dict(
            status=thesis["status"],
            decision=thesis["decision"],
            active_case=thesis["active_case"],
            candidate_version=thesis["candidate_version"],
            candidate_changes=thesis["candidate_changes"],
            historical_buy_reason=None,
            evidence_gaps=thesis["gaps"],
            counter_evidence=case["counter_evidence"] if case else [],
            supporting_claims=claims,
            candidate_case=thesis["candidate_case"],
        ),
        decision_journal=thesis["decision_journal"],
        money_action=False,
        data_quality="WARNING",
    )


class HoldingReviewService:
    def __init__(
        self, benchmarks: BenchmarkService, theses: ThesisService, market: MarketDataService
    ) -> None:
        self.benchmarks, self.theses, self.market = benchmarks, theses, market
        self.research = benchmarks.notebook.research

    def build(self, portfolio: str, account: str) -> Json:
        today = self.research._now().astimezone(ZoneInfo(self.research.settings.timezone)).date()
        positions = self.market.portfolio_brief(portfolio_id=portfolio, account_id=account)[
            "valuation"
        ]["positions"]
        items = []
        for p in positions:
            h = p["holding"]
            if Decimal(str(h["total_shares"])) <= 0:
                continue
            code = h["instrument_code"]
            record = self.benchmarks.read(portfolio, code)
            record["evidence"] = self.research.list_evidence(instrument_code=code, limit=1000)
            record["thesis"] = self.theses.read(portfolio, code)
            items.append(fund_review(code, h["instrument_name"], record, today))
        items.sort(key=lambda x: x["instrument_code"])
        body = dict(
            portfolio_id=portfolio,
            account_id=account,
            items=items,
            rule_version=RULE_VERSION,
            money_action=False,
            unified_ranking=None,
        )
        return dict(body, input_hash=digest(semantic(body)), observed_on=today.isoformat())

    @staticmethod
    def _rows(c: Any, table: str, portfolio: str, account: str) -> list[Json]:
        return [
            json.loads(r[0])
            for r in c.execute(
                f"SELECT payload_json FROM {table} "
                "WHERE portfolio_id=? AND account_id=? ORDER BY rowid",
                (portfolio, account),
            )
        ]

    @staticmethod
    def _insert(c: Any, table: str, value: Json) -> None:
        c.execute(
            f"INSERT INTO {table}(id,portfolio_id,account_id,payload_json) VALUES(?,?,?,?)",
            (
                value["id"],
                value["portfolio_id"],
                value["account_id"],
                json.dumps(value, ensure_ascii=False),
            ),
        )

    def history(self, portfolio: str, account: str) -> Json:
        # Scope checked even for empty history.
        self.market.portfolio_brief(portfolio_id=portfolio, account_id=account)
        with self.research._connect() as c:
            result: Json = {
                key: self._rows(c, table, portfolio, account)
                for key, table in (
                    ("snapshots", "holding_review_snapshots"),
                    ("tasks", "holding_review_tasks"),
                    ("events", "holding_review_events"),
                )
            }

        lines = [
            f"复核历史: {len(result['snapshots'])}份独立输入快照, "
            f"{len(result['tasks'])}条证据版本待办; 处理记录不构成投资批准。"
        ]
        for e in result["events"]:
            if e["kind"] == "HANDLE":
                task = next(t for t in result["tasks"] if t["id"] == e["task_id"])
                lines.append(
                    f"{e['recorded_at']} {task['instrument_code']} "
                    f"{TASK_LABELS[e['outcome']]}: {e['explanation']}"
                )
        result["display_text"] = "\n".join(lines)
        return result

    def preview(self, portfolio: str, account: str) -> Json:
        result = self.build(portfolio, account)
        history = self.history(portfolio, account)
        captures = [e for e in history["events"] if e["kind"] == "CAPTURE"]
        previous = next(
            (
                s
                for s in history["snapshots"]
                if captures and s["id"] == captures[-1]["snapshot_id"]
            ),
            None,
        )
        old = {i["instrument_code"]: i for i in previous["snapshot"]["items"]} if previous else {}
        tasks = {t["reason"]["evidence_version"]: t for t in history["tasks"]}
        lines = [
            "持仓比较与复核 | 仅当前持仓、各自基准;无统一排名或买卖信号",
            "查询日期: "
            + result["observed_on"]
            + ";变化相对上次明确保存的复核,不把每次查询当新记录。",
        ]
        for item in result["items"]:
            code = item["instrument_code"]
            prior = old.get(code)
            fields = [k for k in item if prior and semantic(item[k]) != semantic(prior.get(k))]
            item["changes"] = dict(
                baseline_snapshot_id=previous["id"] if previous else None,
                status="NO_BASELINE" if not prior else "CHANGED" if fields else "UNCHANGED",
                fields=fields,
            )
            lines.append(
                f"{code} {item['instrument_name']} | "
                f"{ELIGIBILITY_LABELS[item['comparison']['eligibility']]}"
                f" | {THESIS_LABELS[item['thesis']['status']]}"
            )
            active = item["thesis"]["active_case"]
            lines.append(
                "  已确认论点: " + (active["thesis"]["proposed_why_hold"] if active else "无")
            )
            lines.append(
                "  相对上次变化: "
                + ((", ".join(fields) or "无实质变化") if prior else "尚无保存的复核基线")
            )
            lines.append(
                "  产品披露: "
                + "; ".join(c["text"] for c in item["comparison"]["disclosed_mandate"])
            )
            for reason in item["reasons"]:
                task = tasks.get(reason["evidence_version"])
                events = [e for e in history["events"] if task and e.get("task_id") == task["id"]]
                reason["task"] = dict(
                    id=task["id"] if task else None,
                    status=events[-1]["outcome"]
                    if events
                    else "OPEN"
                    if task
                    else "PROPOSED_NOT_SAVED",
                    latest_event_id=events[-1]["id"] if events else None,
                )
                lines.append(
                    f"  [{CATEGORY_LABELS[reason['category']]}] {reason['text']}"
                    f" | 数据 {', '.join(reason['data_dates']) or '缺失/见依据'}"
                    f" | {TASK_LABELS[reason['task']['status']]}"
                )
                lines.append("    下一步: " + reason["next_step"])
                for source in reason["sources"]:
                    lines.append(
                        "    来源: "
                        + source["source_name"]
                        + " | "
                        + source["evidence_date"]
                        + " | "
                        + source["source_ref"]
                    )
            for w in item["windows"]:
                drawdown = w["fund_max_drawdown_pct"]
                drawdown_text = "缺失" if drawdown is None else str(drawdown)
                lines.append(
                    f"  {w['start']} 至 {w['end']}: "
                    f"基金 {w['fund_return_pct']:.2f}%, 基准 {w['benchmark_return_pct']:.2f}%; "
                    f"差额 {w['excess_percentage_points']:+.2f}百分点; "
                    f"{DRAWDOWN_LABEL} "
                    f"{drawdown_text}; "
                    + (
                        "仅管理人披露"
                        if w["basis"] == "ISSUER_REPORTED_ONLY"
                        else "日序列/管理人公布路径,非成分独立重建"
                        if w["benchmark_path"] == "ISSUER_PUBLISHED"
                        else "日序列/成分计算,质量限制仍保留"
                    )
                )
            lines.append("  限制: " + "; ".join(item["comparison"]["gaps"] + item["warnings"]))
        result.update(
            previous_snapshot_id=previous["id"] if previous else None,
            history_count=len(history["snapshots"]),
            display_text="\n".join(lines),
        )
        return result

    def _replay(self, c: Any, portfolio: str, account: str, payload: Json) -> Json | None:
        for e in self._rows(c, "holding_review_events", portfolio, account):
            if e["idempotency_key"] == payload["idempotency_key"]:
                if e["request_hash"] != digest(payload):
                    raise LedgerError("REVIEW_KEY_CONFLICT", "Idempotency key content differs")
                return e
        return None

    def capture(self, request: ReviewCapture) -> Json:
        payload = request.model_dump(mode="json")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            replay = self._replay(c, request.portfolio_id, request.account_id, payload)
            if replay:
                return replay
            current = self.build(request.portfolio_id, request.account_id)
            if current["input_hash"] != request.expected_input_hash:
                raise LedgerError("REVIEW_INPUT_CHANGED", "Read current review before saving")
            snapshots = self._rows(
                c, "holding_review_snapshots", request.portfolio_id, request.account_id
            )
            snapshot = next(
                (s for s in snapshots if s["snapshot"]["input_hash"] == current["input_hash"]), None
            )
            scope = dict(portfolio_id=request.portfolio_id, account_id=request.account_id)
            now = stamp(self.research._now())
            if snapshot is None:
                snapshot = dict(
                    scope,
                    id=str(uuid4()),
                    captured_at=now,
                    snapshot=current,
                    previous_snapshot_id=snapshots[-1]["id"] if snapshots else None,
                )
                self._insert(c, "holding_review_snapshots", snapshot)
            tasks = self._rows(c, "holding_review_tasks", request.portfolio_id, request.account_id)
            added = []
            for item in current["items"]:
                for reason in item["reasons"]:
                    if any(
                        t["reason"]["evidence_version"] == reason["evidence_version"] for t in tasks
                    ):
                        continue
                    prior = [t for t in tasks if t["reason"]["reason_key"] == reason["reason_key"]]
                    task = dict(
                        scope,
                        id=str(uuid4()),
                        instrument_code=item["instrument_code"],
                        snapshot_id=snapshot["id"],
                        created_at=now,
                        reason=reason,
                        supersedes_task_id=prior[-1]["id"] if prior else None,
                    )
                    self._insert(c, "holding_review_tasks", task)
                    tasks.append(task)
                    added.append(task["id"])
            event = dict(
                scope,
                id=str(uuid4()),
                kind="CAPTURE",
                snapshot_id=snapshot["id"],
                created_task_ids=added,
                recorded_at=now,
                request_hash=digest(payload),
                idempotency_key=request.idempotency_key,
                actor_ref=request.actor_ref,
                money_action=False,
            )
            self._insert(c, "holding_review_events", event)
            return event

    def handle(self, task_id: str, request: ReviewHandling) -> Json:
        payload = dict(request.model_dump(mode="json"), task_id=task_id)
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            replay = self._replay(c, request.portfolio_id, request.account_id, payload)
            if replay:
                return replay
            tasks = self._rows(c, "holding_review_tasks", request.portfolio_id, request.account_id)
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task is None:
                raise LedgerError("REVIEW_TASK_MISSING", "Task not in this scope", http_status=404)
            events = [
                e
                for e in self._rows(
                    c, "holding_review_events", request.portfolio_id, request.account_id
                )
                if e.get("task_id") == task_id
            ]
            if request.expected_event_id != (events[-1]["id"] if events else None):
                raise LedgerError("REVIEW_EVENT_CHANGED", "Read latest handling history")
            self.benchmarks.notebook._evidence(
                c, task["instrument_code"], set(request.evidence_ids)
            )
            event = dict(
                payload,
                id=str(uuid4()),
                kind="HANDLE",
                recorded_at=stamp(self.research._now()),
                request_hash=digest(payload),
                snapshot_id=task["snapshot_id"],
                evidence_version=task["reason"]["evidence_version"],
                money_action=False,
                issue_resolved=None,
                approval_effect=False,
            )
            self._insert(c, "holding_review_events", event)
            return event
