"""Immutable, evidence-linked advisory research. Never participates in allocation."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.research import ResearchService
from investor_core.scheduler import digest, stamp

Json = dict[str, Any]
Driver = Literal[
    "LONG_TERM_GROWTH",
    "VALUATION_REVERSION",
    "INCOME_CARRY",
    "CYCLICAL_RECOVERY",
    "TREND",
    "MIXED",
    "UNKNOWN",
]


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["PUBLIC_FACT", "RESEARCH_HYPOTHESIS", "COUNTER_EVIDENCE", "UNKNOWN"]
    evidence_ids: list[str] = Field(default_factory=list)
    as_of: date | None = None
    limitation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def sourced(self) -> Claim:
        if self.kind in {"PUBLIC_FACT", "COUNTER_EVIDENCE"} and (
            not self.evidence_ids or not self.as_of
        ):
            raise ValueError("Public facts and counter evidence require sources and data date")
        return self


class DriverProfile(StrictModel):
    scope: Literal["FUND", "INDEX", "SECTOR", "UNDERLYING"] = "FUND"
    primary: Driver = "UNKNOWN"
    secondary: list[Driver] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    evidence_maturity: Literal["UNVERIFIED", "PROVISIONAL"] = "PROVISIONAL"
    data_date: date
    effective_from: date
    effective_to: date | None = None
    classification_version: str = "manual-research-v1"

    @model_validator(mode="after")
    def validity(self) -> DriverProfile:
        if self.primary != "UNKNOWN" and not self.evidence_ids:
            raise ValueError("A proposed classification requires evidence")
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Invalid profile interval")
        return self


class Thesis(StrictModel):
    playbook: Literal["STRATEGIC_DCA", "VALUATION_REVERSION", "TREND", "RESEARCH_ONLY"] = (
        "RESEARCH_ONLY"
    )
    proposed_why_hold: str = Field(min_length=1, max_length=4000)
    # A research author cannot backfill a user's historical intent.
    original_buy_reason: None = None
    expected_horizon: str = Field(min_length=1)
    expected_adversity: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)
    add_conditions: list[str] = Field(min_length=1)
    hold_conditions: list[str] = Field(min_length=1)
    sell_review_conditions: list[str] = Field(min_length=1)


class CaseDraft(StrictModel):
    portfolio_id: str
    instrument_code: str
    expected_previous_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    as_of: date
    thesis: Thesis
    return_driver: DriverProfile
    maper: dict[str, list[Claim]]
    counter_evidence: list[Claim] = Field(min_length=1)
    unresolved_questions: list[str] = Field(min_length=1)
    revision_reason: str = Field(min_length=1)
    actor_ref: str = "codex"

    @model_validator(mode="after")
    def dimensions(self) -> CaseDraft:
        if set(self.maper) != set("MAPER") or any(not v for v in self.maper.values()):
            raise ValueError("All five MAPER dimensions must explicitly contain facts or unknowns")
        if not all(c.kind in {"COUNTER_EVIDENCE", "UNKNOWN"} for c in self.counter_evidence):
            raise ValueError("Counter evidence must be explicit or marked unknown")
        return self


class JournalEntry(StrictModel):
    portfolio_id: str
    instrument_code: str
    thesis_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    supersedes_id: str | None = None
    decision_frame: str = Field(min_length=1)
    known_facts: list[Claim]
    unknowns: list[str]
    expected_scenarios: list[str]
    chosen_action: Literal["RESEARCH_ONLY", "REQUEST_EVIDENCE", "REQUEST_RULE_REVIEW"]
    process_quality: Literal["PASS", "WARNING", "FAIL", "NOT_REVIEWED"] = "NOT_REVIEWED"
    execution_quality: Literal["PASS", "WARNING", "FAIL", "NOT_APPLICABLE"] = "NOT_APPLICABLE"
    outcome_quality: Literal["FAVORABLE", "UNFAVORABLE", "MIXED", "UNKNOWN"] = "UNKNOWN"
    logic_match: Literal["MATCHED", "DIVERGED", "UNKNOWN"] = "UNKNOWN"
    root_cause: list[str] = Field(default_factory=list)
    lesson: str | None = None
    review_evidence_ids: list[str] = Field(default_factory=list)
    actor_ref: str = "codex"

    @model_validator(mode="after")
    def reviewed(self) -> JournalEntry:
        if (
            self.process_quality != "NOT_REVIEWED"
            or self.outcome_quality != "UNKNOWN"
            or self.logic_match != "UNKNOWN"
        ) and not self.review_evidence_ids:
            raise ValueError(
                "Quality review requires evidence; profit does not establish process quality"
            )
        return self


class NotebookService:
    def __init__(self, research: ResearchService) -> None:
        self.research = research

    def _scope(self, c: Any, portfolio: str, code: str) -> None:
        if not c.execute("SELECT 1 FROM portfolios WHERE id=?", (portfolio,)).fetchone():
            raise LedgerError("PORTFOLIO_NOT_FOUND", "Portfolio does not exist", http_status=404)
        if not c.execute("SELECT 1 FROM instruments WHERE code=?", (code,)).fetchone():
            raise LedgerError("INSTRUMENT_NOT_FOUND", "Instrument does not exist", http_status=404)

    def _evidence(self, c: Any, code: str, ids: set[str]) -> None:
        for eid in ids:
            row = c.execute(
                "SELECT e.instrument_id, i.code FROM market_research_evidence e "
                "JOIN instruments i ON i.id=e.instrument_id WHERE e.id=?",
                (eid,),
            ).fetchone()
            if not row or row["code"] != code:
                raise LedgerError(
                    "NOTEBOOK_EVIDENCE_SCOPE", "Evidence must exist for the exact fund"
                )

    def create(self, request: CaseDraft) -> Json:
        payload = request.model_dump(mode="json")
        claims = [c for items in request.maper.values() for c in items] + request.counter_evidence
        ids = set(request.return_driver.evidence_ids) | {e for c in claims for e in c.evidence_ids}
        if (
            request.as_of > self.research._now().date()
            or any(c.as_of and c.as_of > request.as_of for c in claims)
            or request.return_driver.data_date > request.as_of
        ):
            raise LedgerError("NOTEBOOK_FUTURE", "Research date cannot be in the future")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._scope(c, request.portfolio_id, request.instrument_code)
            replay = c.execute(
                "SELECT payload_json FROM research_case_versions WHERE request_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if replay:
                result = json.loads(replay[0])
                if result["request_hash"] != digest(payload):
                    raise LedgerError(
                        "IDEMPOTENCY_CONFLICT", "Key already used with different content"
                    )
                return dict(result, idempotent_replay=True)
            self._evidence(c, request.instrument_code, ids)
            previous = c.execute(
                "SELECT COALESCE(MAX(version),0) FROM research_case_versions "
                "WHERE portfolio_id=? AND instrument_code=?",
                (request.portfolio_id, request.instrument_code),
            ).fetchone()[0]
            if previous != request.expected_previous_version:
                raise LedgerError(
                    "NOTEBOOK_VERSION_STALE", "Read the latest version before revising"
                )
            result = dict(
                payload,
                id=str(uuid4()),
                version=previous + 1,
                status="ADVISORY_DRAFT",
                approved=False,
                money_action=False,
                request_hash=digest(payload),
                created_at=stamp(self.research._now()),
                evidence_ids=sorted(ids),
            )
            c.execute(
                "INSERT INTO research_case_versions VALUES (?,?,?,?,?,?)",
                (
                    result["id"],
                    request.portfolio_id,
                    request.instrument_code,
                    previous + 1,
                    request.idempotency_key,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            return result

    def journal(self, request: JournalEntry) -> Json:
        payload = request.model_dump(mode="json")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._scope(c, request.portfolio_id, request.instrument_code)
            replay = c.execute(
                "SELECT payload_json FROM research_decision_journal WHERE request_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if replay:
                result = json.loads(replay[0])
                if result["request_hash"] != digest(payload):
                    raise LedgerError("IDEMPOTENCY_CONFLICT", "Journal key content mismatch")
                return dict(result, idempotent_replay=True)
            case = c.execute(
                "SELECT id FROM research_case_versions WHERE portfolio_id=? "
                "AND instrument_code=? AND version=?",
                (request.portfolio_id, request.instrument_code, request.thesis_version),
            ).fetchone()
            if not case:
                raise LedgerError(
                    "NOTEBOOK_VERSION_MISSING", "Referenced thesis version does not exist"
                )
            if request.supersedes_id:
                prior = c.execute(
                    "SELECT payload_json FROM research_decision_journal WHERE id=?",
                    (request.supersedes_id,),
                ).fetchone()
                if not prior or any(
                    json.loads(prior[0])[k] != payload[k]
                    for k in ("portfolio_id", "instrument_code")
                ):
                    raise LedgerError(
                        "NOTEBOOK_JOURNAL_SCOPE", "Review must reference same subject"
                    )
            self._evidence(
                c,
                request.instrument_code,
                set(request.review_evidence_ids)
                | {e for claim in request.known_facts for e in claim.evidence_ids},
            )
            result = dict(
                payload,
                id=str(uuid4()),
                request_hash=digest(payload),
                created_at=stamp(self.research._now()),
                money_action=False,
            )
            c.execute(
                "INSERT INTO research_decision_journal VALUES (?,?,?,?)",
                (
                    result["id"],
                    request.portfolio_id,
                    request.idempotency_key,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            return result

    def read(self, portfolio_id: str, instrument_code: str) -> Json:
        with self.research._connect() as c:
            self._scope(c, portfolio_id, instrument_code)
            versions = [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT payload_json FROM research_case_versions WHERE portfolio_id=? "
                    "AND instrument_code=? ORDER BY version",
                    (portfolio_id, instrument_code),
                )
            ]
            journal = [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT payload_json FROM research_decision_journal "
                    "WHERE portfolio_id=? ORDER BY rowid",
                    (portfolio_id,),
                )
                if json.loads(r[0])["instrument_code"] == instrument_code
            ]
        latest = versions[-1] if versions else None
        all_sources = self.research.list_evidence(instrument_code=instrument_code, limit=500)
        wanted = set(latest["evidence_ids"]) if latest else set()
        sources = [r for r in all_sources if r["id"] in wanted]
        # Resolve every reference even when the fund has more than 500 archived records.
        with self.research._connect() as c:
            for eid in wanted - {r["id"] for r in sources}:
                row = c.execute(
                    "SELECT * FROM market_research_evidence WHERE id=?", (eid,)
                ).fetchone()
                if row:
                    sources.append(self.research._evidence_data(c, row))
        lines = [
            f"{instrument_code} 研究档案 | 研究草稿,不改变投资资格或金额",
            "用户原始买入理由:未知;当前研究假设不代表历史买入理由。",
        ]
        if latest:
            lines += [
                f"论点版本 {latest['version']} | {latest['status']} | 截至 {latest['as_of']}",
                "当前研究假设:" + latest["thesis"]["proposed_why_hold"],
                "拟议收益来源:" + latest["return_driver"]["primary"] + "(未确认)",
            ]
            for dimension, claims in latest["maper"].items():
                for claim in claims:
                    lines.append(
                        f"{dimension} [{claim['kind']}] {claim['text']};限制:{claim['limitation']}"
                    )
            lines += ["反证:" + v["text"] for v in latest["counter_evidence"]]
            lines += ["待核实:" + v for v in latest["unresolved_questions"]]
        else:
            lines.append("尚无版本化研究草稿;不根据持仓名称生成投资论点。")
        lines.append(f"决策日志 {len(journal)} 条;过程质量与结果质量分别记录,不由盈亏推断。")
        for source in sources:
            archive = source.get("facts", {})
            lines.append(
                f"来源: {source['source_name']}"
                f" | 数据 {archive.get('data_date', source['evidence_date'])}"
                f" | {source['source_ref']} | {archive.get('quality', 'UNVERIFIED')}"
            )
        return dict(
            sources=sources,
            instrument_code=instrument_code,
            versions=versions,
            latest=latest,
            journal=journal,
            money_action=False,
            display_text="\n".join(lines),
        )


# This is configuration/data coverage, not a new risk scan and not a safe-to-buy signal.
def risk_coverage(assignment: Json, brief: Json, observations: dict[str, list[Json]]) -> Json:
    specifications = [
        ("SELL_01_HARD_STOP", ("hard_stop_return_bps",), None, "DIRECT", "风险容忍度与历史回撤"),
        (
            "SELL_03_REBALANCE",
            ("maximum_position_weight_bps",),
            None,
            "DIRECT",
            "组合集中度与批准配置",
        ),
        (
            "SELL_04_REPLACE",
            ("replacement_min_score_delta_bps", "replacement_min_consecutive_periods"),
            "REPLACEMENT_CANDIDATE",
            "ALL",
            "可比候选、评分口径与连续观察;候选模型未晋级",
        ),
        (
            "SELL_05_UNDERPERFORMANCE",
            ("underperformance_threshold_bps", "underperformance_min_days"),
            "RELATIVE_PERFORMANCE",
            "ALL",
            "经确认基准、费用及完整比较窗口",
        ),
        (
            "SELL_06_TAKE_PROFIT",
            ("take_profit_return_bps", "take_profit_min_holding_days"),
            None,
            "ALL",
            "投资期限、总回报口径和止盈目的",
        ),
        (
            "SELL_07_OBJECTIVE_COMPLETE",
            ("objective_sell_fraction_bps",),
            "OBJECTIVE_STATUS",
            "ALL",
            "用户投资目标及达成证据",
        ),
        ("SELL_08_LIQUIDITY", ("liquidity_priority",), None, "ALL", "用户真实流动性需求"),
        (
            "CORE_TOOL_QUALITY",
            ("max_tracking_error_bps", "max_expense_ratio_bps"),
            "TOOL_QUALITY",
            "ANY",
            "同类成本与适用跟踪基准",
        ),
    ]
    positions = {p["holding"]["instrument_code"]: p for p in brief["valuation"]["positions"]}
    items = []
    for config in assignment["instruments"]:
        code = config["instrument_code"]
        if code not in positions:
            continue
        position = positions[code]
        rules = config.get("lifecycle_rules", {})
        for name, keys, observation, mode, basis in specifications:
            values = {k: (config if mode == "DIRECT" else rules).get(k) for k in keys}
            configured = (
                any(v is not None for v in values.values())
                if mode == "ANY"
                else all(v is not None for v in values.values())
            )
            data = [o for o in observations.get(code, []) if o["observation_type"] == observation]
            gaps = []
            if not configured:
                gaps.append("RULE_DECISION_REQUIRED")
            if observation and not data:
                gaps.append("OBSERVATION_MISSING")
            elif data:
                gaps.append("OBSERVATION_FRESHNESS_UNCONFIRMED")
            if name == "SELL_05_UNDERPERFORMANCE" and not config.get("benchmark_code"):
                gaps.append("BENCHMARK_NOT_APPROVED")
            if name == "SELL_04_REPLACE":
                gaps.append("MODEL_NOT_PROMOTED")
            if name == "SELL_08_LIQUIDITY":
                gaps.append("USER_NEED_NOT_REQUESTED")
            if position.get("data_quality") != "PASS":
                gaps.append("VALUATION_WARNING")
            applicable = name != "CORE_TOOL_QUALITY" or config["strategy_role"] == "CORE"
            if not applicable:
                gaps = ["NOT_APPLICABLE_TO_ROLE"]
            items.append(
                dict(
                    instrument_code=code,
                    rule_code=name,
                    applicable=applicable,
                    current_values=values,
                    configured=configured,
                    gaps=gaps,
                    evidence_basis=basis,
                    observation_ids=[o["id"] for o in data],
                    proposal=dict(
                        status="REVIEW_ONLY",
                        proposed_values=None,
                        required_evidence=basis,
                        confirmation_required=True,
                    ),
                )
            )
        items.append(
            dict(
                instrument_code=code,
                rule_code="SELL_02_THESIS_INVALID",
                applicable=True,
                configured=True,
                current_values={"thesis_status": config["thesis_status"]},
                gaps=["LEGACY_STATUS_NOT_VERSIONED_THESIS"],
                evidence_basis="当前已批准旧论点状态;研究草稿未激活",
                observation_ids=[],
                proposal=dict(
                    status="REVIEW_ONLY",
                    proposed_values=None,
                    required_evidence="明确投资论点及失效条件,逐只确认",
                    confirmation_required=True,
                ),
            )
        )
    counts: dict[str, int] = {}
    for item in items:
        for gap in item["gaps"]:
            counts[gap] = counts.get(gap, 0) + 1
    lines = [
        "风险覆盖与配置提案 | 只读;未填阈值、未执行扫描或生成卖出动作",
        "先确认基准和数据口径,再讨论期限、集中度与触发阈值;缺配置不等于低风险。",
    ]
    for item in items:
        lines.append(
            f"{item['instrument_code']} {item['rule_code']}: "
            + (", ".join(item["gaps"]) or "配置存在;仍需核验数据时效和扫描结果")
        )
    lines.append("本提案没有可直接提交的数值;需要明确投资规则后再走已有配置草稿确认。")
    return dict(
        as_of=brief["as_of_date"],
        strategy_version=assignment["strategy"]["version"],
        classification_counts=counts,
        items=items,
        money_action=False,
        read_only=True,
        display_text="\n".join(lines),
    )
