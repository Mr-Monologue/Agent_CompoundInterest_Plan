"""Read-only review orchestration and explicit, append-only observation saves."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from investor_core.execution import StrictModel
from investor_core.holding_review import HoldingReviewService
from investor_core.ledger import LedgerError
from investor_core.review_delta import COMPARISON_VERSION, DeltaEngine, project, state_hash
from investor_core.scheduler import digest, stamp

Json = dict[str, Any]


class ObservationSave(StrictModel):
    portfolio_id: str
    account_id: str
    expected_state_hash: str
    expected_baseline_id: str
    explicit_save: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = "codex"


class BaselineService:
    @staticmethod
    def first(history: Json) -> Json:
        if not history["snapshots"]:
            raise LedgerError(
                "REVIEW_BASELINE_MISSING", "请先预览并明确保存首次观察基线", http_status=409
            )
        # Append-only insertion order, never the last query or most recent capture event.
        return dict(history["snapshots"][0])


class CurrentResearchService:
    def __init__(self, reviews: HoldingReviewService) -> None:
        self.reviews = reviews

    def read(self, portfolio: str, account: str) -> Json:
        raw = self.reviews.build(portfolio, account)
        # A new candidate after an approved mapping must remain visible and unapproved.
        pending = {}
        for item in raw["items"]:
            c = item["comparison"]
            if c["mapping_approved"]:
                versions = self.reviews.benchmarks.read(portfolio, item["instrument_code"])[
                    "versions"
                ]
                newer = [
                    v
                    for v in versions
                    if v["version"] > c["mapping_version"] and v["status"] != "APPROVED_RESEARCH"
                ]
                if newer:
                    pending[item["instrument_code"]] = [newer[-1]]
        if pending:
            raw["pending_mappings"] = pending
        return raw


class ReviewService:
    def __init__(self, reviews: HoldingReviewService) -> None:
        self.reviews = reviews
        self.current = CurrentResearchService(reviews)

    def read(
        self,
        portfolio: str,
        account: str,
        *,
        status: str | None = None,
        code: str | None = None,
        details: bool = False,
    ) -> Json:
        history = self.reviews.history(portfolio, account)
        baseline = BaselineService.first(history)
        current = self.current.read(portfolio, account)
        result = self.compare(baseline, current)
        result["snapshot_count"] = len(history["snapshots"])
        from investor_core.delta_presentation import present

        result.update(present(result, status=status, code=code, details=details))
        return result

    @staticmethod
    def compare(baseline: Json, current: Json) -> Json:
        result = DeltaEngine().evaluate(project(baseline["snapshot"]), project(current))
        result["baseline"] = dict(
            snapshot_id=baseline["id"],
            captured_at=baseline["captured_at"],
            observed_on=baseline["snapshot"]["observed_on"],
        )
        result["raw_current"] = current
        # This path reads archived Core research only; comparison never refreshes sources.
        # Acquisition context is not research state and must not affect snapshot identity.
        result["evidence_acquisition"] = dict(
            status="NOT_PERFORMED", new_evidence_fetched=False, scope="CORE_ARCHIVE_ONLY"
        )
        warnings = {w for i in current["items"] for w in i["warnings"]}
        data_codes = {
            "SINGLE_SOURCE_WARNING",
            "SOURCE_UNAVAILABLE",
            "SOURCE_REQUEST_FAILED",
            "BENCHMARK_DATE_MISSING",
            "FUND_DATE_MISSING",
            "EXACT_COMMON_CALENDAR_MISSING",
        }
        result["warnings"] = dict(
            research=sorted(warnings - data_codes), data=sorted(warnings & data_codes), system=[]
        )
        return result


class SnapshotService:
    def __init__(self, reviews: HoldingReviewService) -> None:
        self.reviews = reviews
        self.current = CurrentResearchService(reviews)

    def save(self, request: ObservationSave) -> Json:
        payload = request.model_dump(mode="json")
        portfolio, account = request.portfolio_id, request.account_id
        with self.reviews.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            replay = self.reviews._replay(c, portfolio, account, payload)
            if replay:
                return replay
            snapshots = self.reviews._rows(c, "holding_review_snapshots", portfolio, account)
            baseline = BaselineService.first(dict(snapshots=snapshots))
            if baseline["id"] != request.expected_baseline_id:
                raise LedgerError("REVIEW_BASELINE_CHANGED", "首次观察基线不匹配,请重新预览")
            raw = self.current.read(portfolio, account)
            current = project(raw)
            if state_hash(current) != request.expected_state_hash:
                raise LedgerError("REVIEW_INPUT_CHANGED", "当前研究状态已改变,请重新预览后保存")
            existing = next(
                (
                    s
                    for s in snapshots
                    if state_hash(project(s["snapshot"])) == request.expected_state_hash
                ),
                None,
            )
            if existing:
                return dict(
                    snapshot_id=existing["id"],
                    reused=True,
                    created_task_ids=[],
                    writes_performed=False,
                    approval_mutation=False,
                    holding_mutation=False,
                    display_text="相同研究状态已有观察点,复用原记录;未新增快照或待办。",
                )
            scope = dict(portfolio_id=portfolio, account_id=account)
            now = stamp(self.reviews.research._now())
            snapshot = dict(
                scope,
                id=str(uuid4()),
                captured_at=now,
                snapshot=raw,
                previous_snapshot_id=snapshots[-1]["id"],
            )
            delta = ReviewService.compare(baseline, raw)
            event = dict(
                scope,
                id=str(uuid4()),
                kind="CAPTURE",
                snapshot_id=snapshot["id"],
                created_task_ids=[],
                recorded_at=now,
                request_hash=digest(payload),
                idempotency_key=request.idempotency_key,
                actor_ref=request.actor_ref,
                comparison=dict(
                    from_snapshot_id=baseline["id"],
                    to_snapshot_id=snapshot["id"],
                    comparison_version=COMPARISON_VERSION,
                    delta_summary=delta["summary"],
                ),
                money_action=False,
                approval_mutation=False,
                holding_mutation=False,
                display_text="已保存新的研究观察点;未处理问题、批准配置或修改投资事实。",
            )
            self.reviews._insert(c, "holding_review_snapshots", snapshot)
            self.reviews._insert(c, "holding_review_events", event)
            return event

    def recompute(self, portfolio: str, account: str, target: str) -> Json:
        history = self.reviews.history(portfolio, account)
        snapshot = next((s for s in history["snapshots"] if s["id"] == target), None)
        if not snapshot:
            raise LedgerError(
                "REVIEW_SNAPSHOT_MISSING", "观察点不在当前组合账户范围", http_status=404
            )
        event = next(
            (
                e
                for e in history["events"]
                if e.get("comparison", {}).get("to_snapshot_id") == target
            ),
            None,
        )
        if not event or event["comparison"]["comparison_version"] != COMPARISON_VERSION:
            raise LedgerError(
                "REVIEW_COMPARISON_VERSION_MISSING", "该观察点无此版本的已保存比较", http_status=409
            )
        baseline = next(
            s for s in history["snapshots"] if s["id"] == event["comparison"]["from_snapshot_id"]
        )
        result = ReviewService.compare(baseline, snapshot["snapshot"])
        result.update(persistence="persisted", to_snapshot_id=target)
        return result
