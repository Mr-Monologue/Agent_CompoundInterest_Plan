"""User-triggered research runs; fenced staging and atomic peer publication."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from investor_core.execution import TZ, StrictModel
from investor_core.ledger import LedgerError
from investor_core.peer_models import PeerStudy
from investor_core.peer_research import PeerResearchService
from investor_core.research import ResearchService
from investor_core.research_update_build import build
from investor_core.research_update_sources import Fetcher, canonical, fingerprint, parse
from investor_core.scheduler import instant, stamp

Json = dict[str, Any]


class UpdateRequest(StrictModel):
    scope: Literal["ALL", "022463", "003096"] = "ALL"
    idempotency_key: str = Field(min_length=1, max_length=200)
    resume: bool = False


class ResearchUpdates:
    def __init__(
        self, research: ResearchService, fetch_factory: Callable[[], Any] = Fetcher
    ) -> None:
        self.research = research
        self.peers = PeerResearchService(research)
        self.fetch_factory = fetch_factory

    def check(self) -> Json:
        cases = []
        for code in ["022463", "003096"]:
            try:
                r = self.peers.read(code, details=True)
                sources = r["archived_input"]["sources"]
                cases.append(
                    dict(
                        anchor_code=code,
                        version=r["version"],
                        archived_at=r["archived_at"],
                        research_cutoff=r["common_research_cutoff"],
                        source_dates=[
                            dict(
                                key=k,
                                data_date=s["data_date"],
                                published_date=s["published_date"],
                                retrieved_at=s["retrieved_at"],
                                validity_rule=s.get("facts", {}).get("valid_until") or "UNDEFINED",
                            )
                            for k, s in sources.items()
                        ],
                        gaps=r["archived_input"]["limitations"],
                    )
                )
            except LedgerError as exc:
                cases.append(dict(anchor_code=code, error=str(exc)))
        out: Json = dict(
            cases=cases,
            network_performed=False,
            writes_performed=False,
            scope="仅022463/022424、003096/009163;其他持仓未刷新",
            last_run=self.latest(),
        )
        out["display_text"] = "研究更新检查(只读,不联网、不保存)\n" + "\n".join(
            f"{c['anchor_code']}: 归档{c.get('archived_at', '未知')};"
            f"研究截止{c.get('research_cutoff', '未知')};"
            "按来源显示已有有效期,未定义不推定过期。"
            for c in cases
        )
        out["display_text"] += (
            "\n范围仅指数联接与医疗两个现有案例;结构披露、现金腿及独立上游缺口仍保留。"
        )
        return out

    def latest(self) -> Json | None:
        with self.research._connect() as c:
            row = c.execute(
                "SELECT idempotency_key FROM research_update_runs ORDER BY started"
                "_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
        return self.read(row["idempotency_key"]) if row else None

    def read(self, key: str) -> Json:
        with self.research._connect() as c:
            row = c.execute(
                "SELECT * FROM research_update_runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            previous = c.execute(
                "SELECT result_json FROM research_update_runs "
                "WHERE status IN ('SUCCESS','PARTIAL') AND started_at<=? "
                "ORDER BY finished_at DESC,rowid DESC LIMIT 1",
                (row["started_at"] if row else "",),
            ).fetchone()
        if not row:
            raise LedgerError("RESEARCH_UPDATE_NOT_FOUND", "尚未收到该更新请求", http_status=404)
        r: Json = json.loads(row["result_json"])
        r.update(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            scope=row["scope"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            lease_until=row["lease_until"],
        )
        if r["status"] == "RUNNING" and instant(row["lease_until"]) <= self.research._now():
            r["status"] = "INTERRUPTED"
            r["next_step"] = "原请求已中断;用原标识显式恢复,已发布案例不重做"
        r["last_successful_result"] = json.loads(previous["result_json"]) if previous else None
        r["display_text"] = present(r)
        return r

    def evidence(self, key: str) -> Json:
        with self.research._connect() as c:
            row = c.execute("SELECT * FROM research_update_evidence WHERE id=?", (key,)).fetchone()
        if not row:
            raise LedgerError("RESEARCH_UPDATE_EVIDENCE_MISSING", "无此公开证据", http_status=404)
        return dict(row)

    def run(self, request: UpdateRequest) -> Json:
        now = self.research._now()
        owner = str(uuid4())
        key = request.idempotency_key
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM research_update_runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row:
                if row["scope"] != request.scope:
                    raise LedgerError(
                        "UPDATE_KEY_CONFLICT", "同一请求的范围已改变", http_status=409
                    )
                if (
                    row["status"] != "RUNNING"
                    or instant(row["lease_until"]) > now
                    or not request.resume
                ):
                    return self.read(key)
                r: Json = json.loads(row["result_json"])
            else:
                active = c.execute(
                    "SELECT idempotency_key FROM research_update_runs WHERE status='RUNNING'"
                ).fetchone()
                if active:
                    # An expired run is explicitly resumed, never silently replaced by a new key.
                    return self.read(active["idempotency_key"])
                latest = c.execute(
                    "SELECT started_at FROM research_update_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if latest and now - instant(latest["started_at"]) < timedelta(seconds=30):
                    raise LedgerError(
                        "RESEARCH_UPDATE_COOLDOWN",
                        "刚完成检查;请先查看上次结果,至少间隔30秒",
                        http_status=429,
                    )
                r = dict(
                    cases=[],
                    method_version="research-update-v1",
                    approval_mutation=False,
                    holding_mutation=False,
                    baseline_mutation=False,
                )
                c.execute(
                    "INSERT INTO research_update_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        key,
                        request.scope,
                        "RUNNING",
                        stamp(now),
                        None,
                        owner,
                        stamp(now + timedelta(minutes=30)),
                        canonical(r),
                    ),
                )
            c.execute(
                "UPDATE research_update_runs SET owner=?,lease_until=? WHERE idempotency_key=?",
                (owner, stamp(now + timedelta(minutes=30)), key),
            )
        codes = ["022463", "003096"] if request.scope == "ALL" else [request.scope]
        for code in codes:
            if any(x["anchor_code"] == code for x in r["cases"]):
                continue
            staged_case = r.get("staged_case")
            if staged_case and staged_case["anchor_code"] == code:
                case, candidate = staged_case, r.get("staged_input")
            else:
                fetch = self.fetch_factory()
                collected: list[Json] = []

                def probe(
                    source: str,
                    url: str,
                    kind: str,
                    identity: str,
                    fetch: Any = fetch,
                    collected: list[Json] = collected,
                ) -> Json:
                    receipt: Json = dict(
                        source=source, url=url, kind=kind, retrieved_at=stamp(self.research._now())
                    )
                    try:
                        raw = fetch(url)
                        receipt["raw_hash"] = hashlib.sha256(raw).hexdigest()
                        parse_error = None
                        try:
                            parsed = parse(raw, kind, identity)
                        except Exception as exc:
                            parse_error = type(exc).__name__ + ":" + str(exc)[:350]
                            parsed = {"parse_error": parse_error, "raw_hash": receipt["raw_hash"]}
                        receipt["published_date"] = None
                        if isinstance(parsed, list):
                            dates = [v.get("day") or v.get("ex_date") for v in parsed]
                            dates = [d for d in dates if d]
                            receipt["data_from"] = min(dates) if dates else None
                            receipt["data_through"] = max(dates) if dates else None
                        semantic = fingerprint(parsed)
                        # Identity ignores acquisition time and JSON/URL decoration.
                        eid = fingerprint(
                            dict(
                                identity=identity,
                                host=url.split("/")[2],
                                kind=kind,
                                semantic=semantic,
                            )
                        )
                        with self.research._connect() as c:
                            c.execute("BEGIN IMMEDIATE")
                            self._fence(c, key, owner)
                            inserted = c.execute(
                                (
                                    "INSERT OR IGNORE INTO research_update_evidence "
                                    "VALUES (?,?,?,?,?,"
                                    "?,?,?,?)"
                                ),
                                (
                                    eid,
                                    identity,
                                    kind,
                                    url,
                                    receipt["retrieved_at"],
                                    receipt["raw_hash"],
                                    semantic,
                                    base64.b64encode(raw).decode(),
                                    canonical(parsed),
                                ),
                            ).rowcount
                        receipt["archive_new"] = inserted == 1
                        receipt.update(
                            status="FAILED" if parse_error else "SUCCESS",
                            evidence_id=eid,
                            parsed=parsed,
                        )
                        if parse_error:
                            receipt["error"] = parse_error
                    except Exception as exc:
                        receipt.update(
                            status="FAILED", error=type(exc).__name__ + ":" + str(exc)[:350]
                        )
                    receipt["finished_at"] = stamp(self.research._now())
                    collected.append(receipt)
                    return receipt

                try:
                    old = self.peers.read(code, details=True)
                    case = build(old, now.astimezone(TZ).date(), probe)
                except Exception as exc:
                    case = dict(
                        anchor_code=code,
                        status="FAILED",
                        blockers=[type(exc).__name__ + ":" + str(exc)[:350]],
                        checks=collected,
                        candidate=None,
                        changes=[],
                    )
                finally:
                    if hasattr(fetch, "close"):
                        fetch.close()
                candidate = case.pop("candidate", None)
            r.pop("staged_case", None)
            r.pop("staged_input", None)
            # Staged evidence and candidate are durable before publication; replay is deterministic.
            with self.research._connect() as c:
                c.execute("BEGIN IMMEDIATE")
                self._fence(c, key, owner)
                staged = dict(r, staged_case=case, staged_input=candidate)
                c.execute(
                    "UPDATE research_update_runs SET result_json=? WHERE idempotency_key=?",
                    (canonical(staged), key),
                )
            with self.research._connect() as c:
                c.execute("BEGIN IMMEDIATE")
                self._fence(c, key, owner)
                if candidate and case.get("publication_needed"):
                    try:
                        receipt = self.peers.archive(
                            PeerStudy.model_validate(candidate), connection=c
                        )
                        case["publication"] = receipt
                    except LedgerError as exc:
                        case.update(
                            status="BLOCKED", blockers=["RESEARCH_VERSION_DRIFT:" + str(exc)]
                        )
                elif candidate:
                    case["publication"] = dict(version=case["before_version"], reused=True)
                # Keep parsed/raw evidence available by evidence id, not duplicated in each event.
                for item in case.get("checks", []):
                    item.pop("parsed", None)
                case.pop("calculated", None)
                r["cases"].append(case)
                c.execute(
                    "UPDATE research_update_runs SET result_json=? WHERE idempotency_key=?",
                    (canonical(r), key),
                )
        statuses = [x["status"] for x in r["cases"]]
        status = (
            "SUCCESS"
            if all(x == "SUCCESS" for x in statuses)
            else "PARTIAL"
            if any(x in ("SUCCESS", "PARTIAL") for x in statuses)
            else "FAILED"
        )
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._fence(c, key, owner)
            c.execute(
                (
                    "UPDATE research_update_runs SET status=?,finished_at=?,result_jso"
                    "n=? WHERE idempotency_key=?"
                ),
                (status, stamp(self.research._now()), canonical(r), key),
            )
        return self.read(key)

    @staticmethod
    def _fence(c: Any, key: str, owner: str) -> None:
        row = c.execute(
            "SELECT owner,status FROM research_update_runs WHERE idempotency_key=?", (key,)
        ).fetchone()
        if not row or row["owner"] != owner or row["status"] != "RUNNING":
            raise LedgerError(
                "UPDATE_OWNERSHIP_LOST", "原更新已被接管;停止旧进程写入", http_status=409
            )


def present(r: Json) -> str:
    started = instant(r["started_at"]).astimezone(TZ).isoformat(timespec="seconds")
    ended = (
        instant(r["finished_at"]).astimezone(TZ).isoformat(timespec="seconds")
        if r.get("finished_at")
        else "尚未结束"
    )
    labels = {
        "SUCCESS": "检查完成",
        "PARTIAL": "部分完成",
        "FAILED": "更新受阻",
        "BLOCKED": "核验阻断",
        "RUNNING": "正在更新",
        "INTERRUPTED": "中断待恢复",
    }
    kinds = {"NEW_EVIDENCE": "取得新证据", "NO_NEW_CONTENT": "检查成功,计算输入未变"}
    metrics = {
        "return_pct": "收益",
        "max_drawdown_pct": "基金最大回撤(共同交易日)",
        "peer_difference_pp": "同类收益差额",
    }
    lines = [
        f"研究更新结果: {labels.get(r['status'], r['status'])} | 开始 {started} | 结束 {ended}",
        "范围仅指数联接022463/022424及医疗003096/009163;其他持仓未刷新。",
    ]
    for c in r.get("cases", []):
        lines.append(
            f"{c['anchor_code']}: {labels.get(c['status'], c['status'])};"
            f"{kinds.get(c.get('content_status'), '未形成新结论')};"
            f"截止 {c.get('before_cutoff', '未知')} → {c.get('after_cutoff', '保留旧结果')};"
            f"重算{c.get('recomputed_windows', 0)}个份额窗口"
        )
        for b in c.get("blockers", []):
            lines.append("阻塞: " + b)
        failed = [x for x in c.get("checks", []) if x["status"] != "SUCCESS"]
        for x in failed:
            lines.append(f"来源失败 {x['source']}: {x['error']}")
        for channel in c.get("channel_checks", []):
            lines.append(
                f"{channel['code']}渠道核验: {channel['status']};"
                f"核对{channel.get('dates_checked', 0)}日;上游独立性未知"
            )
            for delta in channel.get("dividend_differences", []):
                lines.append(
                    f"分红冲突 {delta['ex_date']}:每份现金 "
                    f"管理人 {delta['official']} / 渠道 {delta['channel']} 元;待核实"
                )
        lines.append(
            f"成功访问{sum(x['status'] == 'SUCCESS' for x in c.get('checks', []))}项来源;"
            f"失败{len(failed)}项。披露目录完整性仍待核验。"
        )
        visible = [x for x in c.get("changes", []) if x["materiality"] == "VISIBLE"]
        new_evidence = sum(x.get("archive_new", False) for x in c.get("checks", []))
        lines.append(
            f"新归档资料 {new_evidence}项;计算输入新增/修订 "
            f"{len(c.get('evidence_changes', []))}项;可见数值变化 {len(visible)}项。"
        )
        if new_evidence and not c.get("evidence_changes"):
            lines.append("取得新资料但尚未改变可发布计算输入;不等于没有新证据。")
        for d in visible[:6]:

            def show(v: Any) -> str:
                return "未计算" if v is None else f"{v:.2f}"

            lines.append(
                f"{d['code']} {d['window']} {metrics[d['metric']]}: "
                f"{show(d['before'])} → {show(d['after'])}{d['unit']};{d['kind']}"
            )
        if not visible and c.get("recomputed_windows"):
            lines.append("已核验可计算部分未发现显示精度以上数值变化;不代表受阻来源没有变化。")
        lines.extend(c.get("limitations", []))
        lines.append(
            "下一步: "
            + (
                "补官方95/5现金计息依据、目标ETF/现金配置解释及渠道上游。"
                if c["anchor_code"] == "022463"
                else "补同期持仓变动与结构披露,核验窗口反转解释及渠道上游。"
            )
        )
    lines.append("滚动窗口推进不等于基金能力变化;不新增D1观察、不处理旧问题,不批准或改变投资金额。")
    return "\n".join(lines)
