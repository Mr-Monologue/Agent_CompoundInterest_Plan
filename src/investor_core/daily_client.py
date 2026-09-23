"""Codex daily HTTP gateway. Business rules and financial facts remain in Core."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter

Json = dict[str, Any]
# Named workflows only: no generic POST, trading, source switch or market refresh.
WORKFLOWS = {
    "plan-draft": ("/v1/weekly-plans", "draft", True),
    "plan-freeze": ("/v1/weekly-plans/{id}/freeze", "confirm", False),
    "skip-draft": ("/v1/weekly-plans/{id}/skip-drafts", "draft", False),
    "skip-commit": ("/v1/weekly-plan-skip-drafts/{id}/commit", "confirm", False),
    "no-investment-draft": ("/v1/weekly-no-investment-drafts", "draft", True),
    "no-investment-commit": ("/v1/weekly-no-investment-drafts/{id}/commit", "confirm", False),
    "close-draft": ("/v1/weekly-plans/{id}/partial-close-drafts", "draft", True),
    "close-commit": ("/v1/weekly-plan-partial-close-drafts/{id}/commit", "confirm", False),
    "report-draft": ("/v1/weekly-plans/{id}/report-drafts", "draft", True),
    "report-commit": ("/v1/weekly-report-drafts/{id}/commit", "confirm", False),
    "purchase-draft": ("/v1/external-subscription-drafts", "draft", True),
    "shares-draft": ("/v1/external-subscriptions/{id}/confirmation-drafts", "draft", True),
    "purchase-commit": ("/v1/external-subscription-drafts/{id}/commit", "financial", False),
    "transaction-draft": (
        "/v1/external-subscription-confirmations/{id}/transaction-drafts",
        "draft",
        True,
    ),
    "transaction-commit": (
        "/v1/external-subscription-confirmations/{parent}/transaction-drafts/{id}/commit",
        "financial",
        False,
    ),
}


class AssistantError(Exception):
    """A safe, actionable client error without credentials or payload logging."""


class DailyClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8710",
        *,
        client: httpx.Client | None = None,
        journal: Path | None = None,
        portfolio_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme != "http" or url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise AssistantError("仅允许本机 Core HTTP 地址。")
        self.selection = (portfolio_id, account_id)
        if bool(portfolio_id) != bool(account_id):
            raise AssistantError("显式选择必须同时提供组合和账户。")
        self.http = client or httpx.Client(base_url=base_url, timeout=30, trust_env=False)
        self.journal = journal or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / (
            "InvestorCore/http-request-journal.sqlite3"
        )

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, **params: Any) -> Json:
        try:
            response = self.http.get(
                path, params={k: v for k, v in params.items() if v is not None}
            )
            response.raise_for_status()
            result: Json = response.json()
        except httpx.HTTPStatusError as exc:
            try:
                error = exc.response.json().get("error", {})
                details = error.get("details", {})
                names = [
                    x["name"]
                    for key in ("portfolio_candidates", "account_candidates")
                    for x in details.get(key, [])
                ]
                reason = error.get("code", "HTTP_ERROR")
            except (ValueError, TypeError, KeyError):
                names, reason = [], "INVALID_ERROR_RESPONSE"
            raise AssistantError(
                f"只读查询失败: HTTP {exc.response.status_code}; {reason}; "
                f"候选名称: {', '.join(names) or '无'}。先核对原因, 不自动写入修复。"
            ) from None
        except (httpx.HTTPError, ValueError) as exc:
            raise AssistantError(
                f"只读查询失败: {path.split('?')[0]} ({type(exc).__name__})"
            ) from None
        if result.get("ok") is False:
            raise AssistantError(f"Core 拒绝查询: {result.get('error', {}).get('code', 'UNKNOWN')}")
        data: Json = result.get("data", result)
        if "data" in result and isinstance(data, dict):
            data.setdefault("data_quality", result.get("meta", {}).get("data_quality", "UNKNOWN"))
            data.setdefault("warnings", result.get("warnings", []))
        return data  # Preserve Core warnings and exact display text.

    def inspect(self, kind: str, subject: str) -> Json:
        paths = {
            "plan": "weekly-plans",
            "report": "weekly-reports",
            "report-draft": "weekly-report-drafts",
            "purchase": "external-subscriptions",
            "purchase-draft": "external-subscription-drafts",
            "transaction-draft": "transaction-drafts",
            "skip-draft": "weekly-plan-skip-drafts",
            "close-draft": "weekly-plan-partial-close-drafts",
            "no-investment-draft": "weekly-no-investment-drafts",
        }
        if kind not in paths or not subject:
            raise AssistantError("需要明确的对象种类和当前操作对象。")
        return self.get(f"/v1/{paths[kind]}/{quote(subject, safe='')}")

    def schema(self, operation: str) -> Json:
        if operation not in WORKFLOWS:
            raise AssistantError("未支持的业务操作。")
        import re

        spec = self.get("/openapi.json")
        target = re.sub(r"\{[^}]+\}", "{}", WORKFLOWS[operation][0])
        for path, verbs in spec["paths"].items():
            if re.sub(r"\{[^}]+\}", "{}", path) == target and "post" in verbs:
                ref = verbs["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
                return {
                    "operation": operation,
                    "schema": spec["components"]["schemas"][ref.split("/")[-1]],
                }
        raise AssistantError("当前 Core 未提供该流程, 请检查版本。")

    def context(self) -> Json:
        if self.selection[0]:
            portfolios = self.get("/v1/portfolios")["items"]
            accounts = self.get("/v1/accounts", portfolio_id=self.selection[0])["items"]
            ps = [p for p in portfolios if p["id"] == self.selection[0] and p["status"] == "ACTIVE"]
            ac = [
                a
                for a in accounts
                if a["id"] == self.selection[1]
                and a["status"] == "ACTIVE"
                and a["portfolio_id"] == self.selection[0]
            ]
            if len(ps) != 1 or len(ac) != 1:
                raise AssistantError("所选组合/账户无效, 不自动改选。")
            return {"portfolio": ps[0], "account": ac[0], "source": "EXPLICIT_READ_ONLY"}
        # Prefer the saved context; singleton resolution does not persist a default.
        return self.get("/v1/investment-context/resolve")

    def scope(self) -> Json:
        c = self.context()
        return {"portfolio_id": c["portfolio"]["id"], "account_id": c["account"]["id"]}

    def investment(self) -> Json:
        scope = self.scope()
        brief = self.get("/v1/portfolio-brief", **scope)
        workspace = self.get("/v1/investment-workspace", **scope, view="DAILY")
        dates = [
            {
                "code": p["holding"]["instrument_code"],
                "nav_date": (p.get("nav_snapshot") or {}).get("nav_date"),
                "nav_age_days": p.get("nav_age_days"),
                "data_quality": p["data_quality"],
            }
            for p in brief["valuation"]["positions"]
        ]
        return {
            "brief": brief,
            "nav_dates": dates,
            "workspace": workspace,
            "display_text": brief["display_text"]
            + "\n\n净值日期(不等于查询日期):\n"
            + "\n".join(
                f"- {p['code']}: {p['nav_date'] or '缺失'}; {p['data_quality']}" for p in dates
            )
            + "\n\n"
            + workspace["display_text"],
        }

    def plan(self, budget: str) -> Json:
        try:
            value = Decimal(budget)
            if not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.01")):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise AssistantError("需要用户明确给出的正数预算, 最多两位小数。") from None
        return self.get("/v1/weekly-plan-preview", **self.scope(), contribution_amount=str(value))

    def week(self) -> Json:
        scope = self.scope()
        plans = self.get("/v1/weekly-plans", portfolio_id=scope["portfolio_id"], limit=500)["items"]
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        current = [p for p in plans if p["period_start"] <= today <= p["period_end"]]
        reports = self.get("/v1/weekly-reports", portfolio_id=scope["portfolio_id"], limit=500)
        drafts = {
            p["id"]: self.get("/v1/weekly-report-drafts", plan_id=p["id"])["items"] for p in plans
        }
        lines = [
            f"周计划状态(截至 {today})",
            "当前周期计划: "
            + ("、".join(p["status"] for p in current) if current else "尚无覆盖今日的计划"),
        ]
        for p in plans:
            lines.append(f"- {p['period_start']}—{p['period_end']}: {p['status']}")
            for d in drafts[p["id"]]:
                lines.append(
                    f"  周报草稿: {d['status']}; {d['data_quality']}; 估值 {d['valuation_status']}"
                )
        lines.append("零投入仅说明本期执行金额为零, 不表示组合收益为零。本次查询未提交周报。")
        return {
            "current_plans": current,
            "plans": plans,
            "report_drafts": drafts,
            "reports": reports,
            "history_may_be_truncated": len(plans) == 500,
            "display_text": "\n".join(lines),
        }

    def system(self, since: str | None = None) -> Json:
        health, ready = self.get("/health"), self.get("/ready")
        scheduler = self.get("/v1/background-scheduler")
        now = datetime.now(UTC)
        start = datetime.combine(
            date.fromisoformat(since) if since else (now - timedelta(days=7)).date(),
            time(),
            ZoneInfo("Asia/Shanghai"),
        )
        if start > now or now - start > timedelta(days=31):
            raise AssistantError("调度审计范围须为过去 31 天内。")
        rows: list[Json] = []
        for policy in scheduler["policies"]:
            runs = self.get("/v1/automation-runs", job_name=policy["job_name"], limit=500)["items"]
            it = croniter(
                policy["schedule"],
                start.astimezone(ZoneInfo(policy["timezone"])) - timedelta(microseconds=1),
            )
            due = it.get_next(datetime)
            while due <= now:
                matches = [
                    r
                    for r in runs
                    if datetime.fromisoformat(r["scheduled_for"].replace("Z", "+00:00")) == due
                    and r["input"].get("portfolio_id") == policy["portfolio_id"]
                ]
                cutover = scheduler.get("cutover_at")
                coverage = "CURRENT_POLICY_RECONSTRUCTION"
                if cutover and due < datetime.fromisoformat(cutover.replace("Z", "+00:00")):
                    coverage = "BEFORE_SOURCE_CUTOVER_REVIEW_ONLY"
                if not policy["enabled"]:
                    coverage = "DISABLED_NOW_HISTORY_REQUIRES_REVIEW"
                rows.append(
                    {
                        "coverage": coverage,
                        "job_name": policy["job_name"],
                        "scheduled_for": due.isoformat(),
                        "policy_enabled_now": policy["enabled"],
                        "occurrence_count": len(matches),
                        "runs": matches,
                        "history_may_be_truncated": len(runs) == 500,
                    }
                )
                due = it.get_next(datetime)
        lines = [
            f"系统状态: {health.get('version')}; 就绪 {ready.get('status')}",
            f"调度来源: {scheduler['source']}; "
            f"心跳: {(scheduler['heartbeat'] or {}).get('received_at', '未收到')}",
            f"历史工作进程异常数: {len((scheduler['heartbeat'] or {}).get('failures', []))}",
            f"异常记录: {scheduler['anomalies'] or '无'}",
            "历史连接失败不等于当前仍离线; 以下按当前政策重建应执行时刻, 政策历史变更需另核。",
        ]
        for r in rows:
            states = ", ".join(
                f"{x['status']} / {x['input'].get('scheduler_source', '未知来源')} / "
                f"{(x.get('output') or {}).get('reason_code', x.get('error_code'))}"
                for x in r["runs"]
            )
            lines.append(
                f"- {r['job_name']} {r['scheduled_for']}: {states or '无记录'}; "
                f"记录数 {r['occurrence_count']}; {r['coverage']}"
            )
        lines.append("下一步: 缺失/失败先只读核对原因; 不自动补跑。通知投递未迁移。")
        market = self.get("/v1/market-data/status")
        if market.get("runs"):
            latest = market["runs"][0]
            lines.append(f"最近行情同步: {latest.get('completed_at')}; {latest.get('status')}")
            for item in latest.get("details", {}).get("items", []):
                snapshot = item.get("snapshot") or {}
                lines.append(
                    f"- {item.get('instrument_code')}: "
                    f"净值日期 {item.get('nav_date') or snapshot.get('nav_date') or '缺失'}; "
                    f"{snapshot.get('data_quality', 'UNKNOWN')}"
                )
        else:
            lines.append("行情同步记录缺失, 不能判定数据已更新。")
        return {
            "health": health,
            "ready": ready,
            "scheduler": scheduler,
            "occurrences": rows,
            "market_data": market,
            "display_text": "\n".join(lines),
        }

    def research(self, topic: str) -> Json:
        scope = self.scope()
        brief = self.get("/v1/portfolio-brief", **scope)
        strategy = self.get("/v1/strategy-assignment", portfolio_id=scope["portfolio_id"])
        keys = ("医疗", "医药", "健康") if topic == "医疗" else (topic,)
        instruments = [
            i for i in strategy["instruments"] if any(k in i["instrument_name"] for k in keys)
        ]
        positions = {p["holding"]["instrument_code"]: p for p in brief["valuation"]["positions"]}
        lines = [
            f"{topic}主题受限研究 | 查询日期 {brief['as_of_date']} | WARNING",
            "事实: 下列净值来自 Core 已保存快照; 不是板块指数涨幅或新闻因果证据。",
            "推断: 基金名称仅用于识别候选相关性, 不能证明穿透行业暴露。",
        ]
        evidence: list[Json] = []
        for i in instruments:
            p = positions.get(i["instrument_code"], {})
            nav = p.get("nav_snapshot") or {}
            evidence.append({"instrument": i, "holding": p, "source_ref": nav.get("source_ref")})
            lines.append(
                f"- {i['instrument_code']} {i['instrument_name']}: "
                f"市值 {p.get('market_value', '无持仓估值')}; "
                f"组合权重 {p.get('weight_pct', '未知')}%; "
                f"净值日期 {nav.get('nav_date', '缺失')}; "
                f"来源 {nav.get('source_ref') or '缺失'}; "
                f"质量 {p.get('data_quality', 'UNKNOWN')}"
            )
            lines.append(
                f"  批准角色 {i['strategy_role']}; 定投资格 {i['contribution_eligible']}; "
                f"基准 {i['benchmark_code'] or '未配置'}; 投资论点 {i['thesis_status']}"
            )
            lines.append(
                "  新增资金: "
                + (
                    "当前不具备定投资格。"
                    if not i["contribution_eligible"]
                    else "资格不等于可买入, 仍须明确预算后由 Core 预览核对。"
                )
            )
        lines += [
            "缺失: 板块定义与区间涨幅、事件原文及时间、基金最新行业持仓、独立验证净值。",
            "上涨及原因未获证实; 不把单只基金变化当作板块上涨, 不推导买入信号。",
            "适用条件: 新资金须满足现有批准资格、舱位、在途占用、限购及信号条件。",
            "策略决定: 如需改变资格/映射/风险阈值, 必须另建草稿并明确确认; 本研究不改变配置。",
        ]
        return {
            "topic": topic,
            "data_quality": "WARNING",
            "evidence": evidence,
            "causal_claim": "NOT_ESTABLISHED",
            "lookthrough_exposure": "UNKNOWN",
            "display_text": "\n".join(lines),
        }

    def workflow(
        self,
        operation: str,
        payload: Json,
        *,
        subject: str = "",
        confirmation: str = "",
        parent: str = "",
    ) -> Json:
        if operation not in WORKFLOWS:
            raise AssistantError("未支持的业务操作。")
        path, level, needs_key = WORKFLOWS[operation]
        if "{id}" in path and (
            not subject
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in subject
            )
        ):
            raise AssistantError("需要已读取并核对的业务对象。")
        if "{parent}" in path:
            if not parent or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in parent
            ):
                raise AssistantError("需要对应的已确认份额记录。")
            path = path.replace("{parent}", quote(parent, safe=""))
        payload = dict(payload)
        if level == "draft":
            payload.setdefault("actor_ref", "codex")
        if level != "draft":
            expected = "确认记录" if level == "financial" else "确认"
            if (
                confirmation != expected
                or not payload.get("confirmation_token")
                or not payload.get("confirmed_by")
            ):
                raise AssistantError(f"须先展示具体内容并取得用户{expected}。")
        if needs_key and not payload.get("idempotency_key"):
            raise AssistantError("缺少当前业务意图的固定幂等标识, 禁止临时重试创建。")
        path = path.replace("{id}", quote(subject, safe=""))
        identity = {k: v for k, v in payload.items() if k != "confirmation_token"}
        key = hashlib.sha256(
            json.dumps([operation, subject, parent, identity], sort_keys=True).encode()
        ).hexdigest()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.journal) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            try:
                db.execute("INSERT INTO requests VALUES (?, 'UNKNOWN')", (key,))
                # Persist before dispatch; concurrent/restarted clients cannot resend.
                db.commit()
            except sqlite3.IntegrityError:
                raise AssistantError(
                    "该请求已发送或结果不明。先查询 Core 的原草稿/事实, 禁止盲目重发。"
                ) from None
        try:
            response = self.http.post(path, json=payload)
            response.raise_for_status()
            result: Json = response.json()
        except (httpx.HTTPError, ValueError):
            raise AssistantError(
                "写入结果未确认。已停止重试; 保留原业务标识, 只读核对 Core 后处理。"
            ) from None
        with sqlite3.connect(self.journal) as db:
            db.execute("UPDATE requests SET state='RECEIVED' WHERE key=?", (key,))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-url", default="http://127.0.0.1:8710")
    parser.add_argument("--portfolio")
    parser.add_argument("--account")
    parser.add_argument("--json", action="store_true", help="Agent internal structured output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("investment")
    sub.add_parser("context")
    sub.add_parser("week")
    sub.add_parser("plan").add_argument("--budget", required=True)
    sub.add_parser("system").add_argument("--since")
    sub.add_parser("research").add_argument("--topic", default="医疗")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("kind")
    inspect.add_argument("subject")
    sub.add_parser("schema").add_argument("operation", choices=sorted(WORKFLOWS))
    flow = sub.add_parser("workflow")
    flow.add_argument("operation", choices=sorted(WORKFLOWS))
    flow.add_argument("--subject", default="")
    flow.add_argument("--confirmation", default="")
    flow.add_argument("--parent", default="")
    args = parser.parse_args()
    client = DailyClient(args.core_url, portfolio_id=args.portfolio, account_id=args.account)
    try:
        if args.command == "plan":
            result = client.plan(args.budget)
        elif args.command == "system":
            result = client.system(args.since)
        elif args.command == "research":
            result = client.research(args.topic)
        elif args.command == "inspect":
            result = client.inspect(args.kind, args.subject)
        elif args.command == "schema":
            result = client.schema(args.operation)
        elif args.command == "workflow":
            result = client.workflow(
                args.operation,
                json.load(sys.stdin),
                subject=args.subject,
                confirmation=args.confirmation,
                parent=args.parent,
            )
        else:
            result = getattr(client, args.command)()
        if args.json or args.command in {"context", "workflow", "inspect", "schema"}:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["display_text"])
    except (AssistantError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    finally:
        client.close()


if __name__ == "__main__":
    main()
