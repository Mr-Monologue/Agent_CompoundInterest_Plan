"""Deterministic read-only Hermes workspace and V1 readiness facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from investor_core.capital import CapitalService
from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError, LedgerService, utc_now
from investor_core.market_data import MarketDataService
from investor_core.operations import OperationsService


class WorkspaceService:
    """Aggregate existing Core facts without running jobs or mutating financial state."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utc_now,
        market_data: MarketDataService | None = None,
        operations: OperationsService | None = None,
        capital: CapitalService | None = None,
        ledger: LedgerService | None = None,
    ) -> None:
        self.settings = settings
        self._now = now
        self._market_data = market_data or MarketDataService(settings, now=now)
        self._operations = operations or OperationsService(settings, now=now)
        self._capital = capital or CapitalService(settings, now=now)
        self._ledger = ledger or LedgerService(settings, now=now)

    def _connect(self) -> sqlite3.Connection:
        database_path = (
            ":memory:"
            if str(self.settings.db_path) == ":memory:"
            else str(Path(self.settings.db_path).resolve())
        )
        connection = sqlite3.connect(database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _counts(rows: list[sqlite3.Row]) -> dict[str, int]:
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _portfolio_brief(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        as_of_date: date,
    ) -> JsonDict:
        try:
            return self._market_data.portfolio_brief(
                portfolio_id=portfolio_id,
                account_id=account_id,
                as_of_date_value=as_of_date.isoformat(),
            )
        except LedgerError as exc:
            if exc.code != "ALLOCATION_POLICY_NOT_CONFIGURED":
                raise
        valuation = self._market_data.portfolio_valuation(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date.isoformat(),
        )
        portfolios = {str(item["id"]): item for item in self._ledger.list_portfolios()}
        accounts = {
            str(item["id"]): item
            for item in self._ledger.list_accounts(portfolio_id=portfolio_id)
        }
        portfolio = portfolios.get(portfolio_id)
        account = accounts.get(account_id)
        if portfolio is None or account is None:
            raise LedgerError(
                "INVESTMENT_CONTEXT_NOT_FOUND",
                "portfolio or account is not active",
                http_status=404,
            )
        return {
            "context": {"portfolio": portfolio, "account": account},
            "valuation": valuation,
            "factual_findings": [],
            "allocation_assessment": {
                "available": False,
                "state": "NOT_EVALUATED",
                "reason_code": "ALLOCATION_POLICY_NOT_CONFIGURED",
            },
        }

    def _workflow_facts(
        self,
        *,
        portfolio_id: str,
        account_id: str,
    ) -> JsonDict:
        with self._connect() as connection:
            strategy = connection.execute(
                """
                SELECT id FROM strategy_assignments
                WHERE portfolio_id=? AND status='ACTIVE'
                """,
                (portfolio_id,),
            ).fetchone()
            eligible_count = 0
            if strategy is not None:
                eligible_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM strategy_instrument_configs
                        WHERE strategy_assignment_id=? AND status='ACTIVE'
                          AND contribution_eligible=1
                        """,
                        (strategy["id"],),
                    ).fetchone()[0]
                )
            plan_counts = self._counts(
                connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM investment_plans
                    WHERE portfolio_id=? AND account_id=? GROUP BY status
                    """,
                    (portfolio_id, account_id),
                ).fetchall()
            )
            proposal_counts = self._counts(
                connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM sell_proposals
                    WHERE portfolio_id=? GROUP BY status
                    """,
                    (portfolio_id,),
                ).fetchall()
            )
            review_action_counts = self._counts(
                connection.execute(
                    """
                    SELECT a.status, COUNT(*) AS count
                    FROM review_action_items a
                    JOIN periodic_reviews r ON r.id=a.review_id
                    WHERE r.portfolio_id=? GROUP BY a.status
                    """,
                    (portfolio_id,),
                ).fetchall()
            )
            research_task_counts = self._counts(
                connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM research_collection_tasks
                    WHERE portfolio_id=? GROUP BY status
                    """,
                    (portfolio_id,),
                ).fetchall()
            )
            active_policy_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM automation_policies
                    WHERE status='ACTIVE' AND enabled=1
                      AND (portfolio_id=? OR portfolio_id IS NULL)
                    """,
                    (portfolio_id,),
                ).fetchone()[0]
            )
            periodic_review_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM periodic_reviews WHERE portfolio_id=?",
                    (portfolio_id,),
                ).fetchone()[0]
            )
            latest_notification = connection.execute(
                """
                SELECT o.status, o.delivered_at, n.created_at
                FROM notification_test_requests n
                JOIN notification_outbox o ON o.notification_test_request_id=n.id
                ORDER BY n.created_at DESC LIMIT 1
                """
            ).fetchone()
            latest_backup = connection.execute(
                """
                SELECT verification_status, created_at, verified_at
                FROM backups ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            run_span = connection.execute(
                """
                SELECT MIN(started_at) AS first_started_at,
                       MAX(started_at) AS last_started_at,
                       COUNT(*) AS run_count,
                       SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed_count
                FROM job_runs
                """
            ).fetchone()

        first_started_at = run_span["first_started_at"] if run_span is not None else None
        last_started_at = run_span["last_started_at"] if run_span is not None else None
        observed_days = 0
        if first_started_at and last_started_at:
            first = datetime.fromisoformat(str(first_started_at).replace("Z", "+00:00"))
            last = datetime.fromisoformat(str(last_started_at).replace("Z", "+00:00"))
            observed_days = max(1, (last.date() - first.date()).days + 1)

        return {
            "strategy_assignment_active": strategy is not None,
            "contribution_eligible_instrument_count": eligible_count,
            "plan_counts": plan_counts,
            "sell_proposal_counts": proposal_counts,
            "review_action_counts": review_action_counts,
            "research_task_counts": research_task_counts,
            "active_automation_policy_count": active_policy_count,
            "periodic_review_count": periodic_review_count,
            "latest_notification_test": (
                {
                    "status": str(latest_notification["status"]),
                    "created_at": str(latest_notification["created_at"]),
                    "delivered_at": latest_notification["delivered_at"],
                }
                if latest_notification is not None
                else None
            ),
            "latest_backup": (
                {
                    "verification_status": str(latest_backup["verification_status"]),
                    "created_at": str(latest_backup["created_at"]),
                    "verified_at": latest_backup["verified_at"],
                }
                if latest_backup is not None
                else None
            ),
            "continuous_operations": {
                "observed_days": observed_days,
                "run_count": int(run_span["run_count"] or 0) if run_span is not None else 0,
                "failed_count": int(run_span["failed_count"] or 0) if run_span is not None else 0,
                "first_started_at": first_started_at,
                "last_started_at": last_started_at,
                "required_days": 14,
            },
        }

    @staticmethod
    def _check(
        code: str,
        status: str,
        reason_code: str,
        *,
        required: bool,
        facts: JsonDict,
    ) -> JsonDict:
        return {
            "code": code,
            "status": status,
            "reason_code": reason_code,
            "required_for_v1": required,
            "facts": facts,
        }

    def _readiness(
        self,
        *,
        brief: JsonDict,
        operations: JsonDict,
        workflows: JsonDict,
        runtime_mode: JsonDict,
    ) -> JsonDict:
        valuation_quality = str(brief["valuation"]["data_quality"])
        scheduler_status = str(operations["scheduler_status"])
        active_policy_count = int(workflows["active_automation_policy_count"])
        notification = workflows["latest_notification_test"]
        backup = workflows["latest_backup"]
        continuity = workflows["continuous_operations"]
        checks = [
            self._check(
                "INVESTMENT_CONTEXT",
                "PASS",
                "DEFAULT_CONTEXT_RESOLVED",
                required=True,
                facts={"context": brief["context"]},
            ),
            self._check(
                "STRATEGY_INSTANCE",
                (
                    "BLOCKED"
                    if not workflows["strategy_assignment_active"]
                    else (
                        "PASS"
                        if int(workflows["contribution_eligible_instrument_count"]) > 0
                        else "NOT_CONFIGURED"
                    )
                ),
                (
                    "ACTIVE_STRATEGY_MISSING"
                    if not workflows["strategy_assignment_active"]
                    else (
                        "ACTIVE_STRATEGY_AND_ALLOWLIST_CONFIGURED"
                        if int(workflows["contribution_eligible_instrument_count"]) > 0
                        else "CONTRIBUTION_ALLOWLIST_EMPTY"
                    )
                ),
                required=True,
                facts={
                    "active": workflows["strategy_assignment_active"],
                    "eligible_instrument_count": workflows[
                        "contribution_eligible_instrument_count"
                    ],
                },
            ),
            self._check(
                "VALUATION_FACTS",
                "BLOCKED" if valuation_quality == "SOURCE_ERROR" else valuation_quality,
                (
                    "VALUATION_FACTS_COMPLETE"
                    if valuation_quality == "PASS"
                    else (
                        "VALUATION_FACTS_LIMITED"
                        if valuation_quality == "WARNING"
                        else "VALUATION_FACTS_UNAVAILABLE"
                    )
                ),
                required=True,
                facts={
                    "data_quality": valuation_quality,
                    "warnings": brief["valuation"]["warnings"],
                },
            ),
            self._check(
                "RUNTIME_MODE",
                "BLOCKED" if runtime_mode["level"] in {"L2", "L3"} else "PASS",
                str(runtime_mode["reason_code"]),
                required=True,
                facts={"level": runtime_mode["level"]},
            ),
            self._check(
                "AUTOMATION_SCHEDULER",
                (
                    "NOT_CONFIGURED"
                    if active_policy_count == 0
                    else ("PASS" if scheduler_status == "IN_SYNC" else "BLOCKED")
                ),
                (
                    "AUTOMATION_POLICY_NOT_CONFIGURED"
                    if active_policy_count == 0
                    else (
                        "SCHEDULER_IN_SYNC"
                        if scheduler_status == "IN_SYNC"
                        else f"SCHEDULER_{scheduler_status}"
                    )
                ),
                required=True,
                facts={
                    "active_policy_count": active_policy_count,
                    "scheduler_status": scheduler_status,
                },
            ),
            self._check(
                "NOTIFICATION_DELIVERY",
                (
                    "NOT_TESTED"
                    if notification is None
                    else ("PASS" if notification["status"] == "DELIVERED" else "BLOCKED")
                ),
                (
                    "NOTIFICATION_TEST_NOT_RUN"
                    if notification is None
                    else (
                        "VERIFIED_PROVIDER_ACCEPTANCE"
                        if notification["status"] == "DELIVERED"
                        else f"NOTIFICATION_{notification['status']}"
                    )
                ),
                required=True,
                facts={"latest_test": notification},
            ),
            self._check(
                "VERIFIED_BACKUP",
                (
                    "NOT_TESTED"
                    if backup is None
                    else ("PASS" if backup["verification_status"] == "PASS" else "BLOCKED")
                ),
                (
                    "VERIFIED_BACKUP_NOT_RECORDED"
                    if backup is None
                    else f"BACKUP_{backup['verification_status']}"
                ),
                required=True,
                facts={"latest_backup": backup},
            ),
            self._check(
                "OPERATIONAL_ALERTS",
                "PASS" if int(operations["open_alert_count"]) == 0 else "BLOCKED",
                (
                    "NO_OPEN_ALERTS"
                    if int(operations["open_alert_count"]) == 0
                    else "OPEN_ALERTS_REQUIRE_REVIEW"
                ),
                required=True,
                facts={"open_alert_count": int(operations["open_alert_count"])},
            ),
            self._check(
                "WEEKLY_PLAN_LIFECYCLE",
                (
                    "PASS"
                    if sum(
                        int(workflows["plan_counts"].get(status, 0))
                        for status in ("EXECUTED", "SKIPPED")
                    )
                    > 0
                    else "NOT_TESTED"
                ),
                (
                    "CLOSED_PLAN_LIFECYCLE_OBSERVED"
                    if sum(
                        int(workflows["plan_counts"].get(status, 0))
                        for status in ("EXECUTED", "SKIPPED")
                    )
                    > 0
                    else "CLOSED_PLAN_LIFECYCLE_NOT_OBSERVED"
                ),
                required=True,
                facts={"plan_counts": workflows["plan_counts"]},
            ),
            self._check(
                "PERIODIC_REVIEW_HISTORY",
                "PASS" if int(workflows["periodic_review_count"]) > 0 else "NOT_ESTABLISHED",
                (
                    "PERIODIC_REVIEW_FACTS_EXIST"
                    if int(workflows["periodic_review_count"]) > 0
                    else "PERIODIC_REVIEW_HISTORY_EMPTY"
                ),
                required=True,
                facts={"review_count": int(workflows["periodic_review_count"])},
            ),
            self._check(
                "FOURTEEN_DAY_OPERATION",
                "PASS" if int(continuity["observed_days"]) >= 14 else "NOT_ESTABLISHED",
                (
                    "CONTINUOUS_OPERATION_ESTABLISHED"
                    if int(continuity["observed_days"]) >= 14
                    else "CONTINUOUS_OPERATION_WINDOW_INCOMPLETE"
                ),
                required=True,
                facts=continuity,
            ),
            self._check(
                "RESEARCH_CONNECTOR",
                (
                    "PASS"
                    if int(workflows["research_task_counts"].get("COMPLETED", 0)) > 0
                    else "OPTIONAL_NOT_CONFIGURED"
                ),
                (
                    "AUDITED_RESEARCH_TASK_COMPLETION_EXISTS"
                    if int(workflows["research_task_counts"].get("COMPLETED", 0)) > 0
                    else "PUBLIC_CORE_HAS_NO_BUNDLED_CONNECTOR"
                ),
                required=False,
                facts={"task_counts": workflows["research_task_counts"]},
            ),
        ]
        required_checks = [item for item in checks if item["required_for_v1"]]
        if any(item["status"] == "BLOCKED" for item in required_checks):
            status = "BLOCKED"
        elif all(item["status"] == "PASS" for item in required_checks):
            status = "READY"
        else:
            status = "PARTIAL"
        counts: dict[str, int] = {}
        for item in checks:
            counts[str(item["status"])] = counts.get(str(item["status"]), 0) + 1
        return {
            "status": status,
            "checks": checks,
            "check_counts": counts,
            "required_check_count": len(required_checks),
            "boundary": "PRODUCT_READINESS_FACTS_NOT_INVESTMENT_ADVICE",
        }

    @staticmethod
    def _action(
        priority: int,
        code: str,
        category: str,
        reason_code: str,
        tool: str,
        facts: JsonDict,
    ) -> JsonDict:
        return {
            "priority": priority,
            "code": code,
            "category": category,
            "reason_code": reason_code,
            "suggested_read_tool": tool,
            "facts": facts,
            "automatic_action": False,
        }

    def _actions(
        self,
        *,
        brief: JsonDict,
        operations: JsonDict,
        workflows: JsonDict,
    ) -> list[JsonDict]:
        actions: list[JsonDict] = []
        if not workflows["strategy_assignment_active"]:
            actions.append(
                self._action(
                    5,
                    "STRATEGY_INSTANCE_REQUIRED",
                    "CONFIGURATION",
                    "ACTIVE_STRATEGY_MISSING",
                    "strategy_current_get",
                    {"active": False},
                )
            )
        elif int(workflows["contribution_eligible_instrument_count"]) == 0:
            actions.append(
                self._action(
                    6,
                    "CONTRIBUTION_ALLOWLIST_EMPTY",
                    "CONFIGURATION",
                    "NO_EXPLICIT_CONTRIBUTION_ELIGIBILITY",
                    "strategy_current_get",
                    {"eligible_instrument_count": 0},
                )
            )
        if int(operations["open_alert_count"]):
            actions.append(
                self._action(
                    10,
                    "OPEN_OPERATIONAL_ALERTS",
                    "BLOCKING",
                    "OPEN_ALERTS_REQUIRE_REVIEW",
                    "automation_alert_list",
                    {"count": int(operations["open_alert_count"])},
                )
            )
        if str(brief["valuation"]["data_quality"]) == "SOURCE_ERROR":
            actions.append(
                self._action(
                    20,
                    "VALUATION_DATA_BLOCKED",
                    "BLOCKING",
                    "VALUATION_FACTS_UNAVAILABLE",
                    "market_data_status_get",
                    {"warnings": brief["valuation"]["warnings"]},
                )
            )
        if (
            int(workflows["active_automation_policy_count"]) > 0
            and str(operations["scheduler_status"]) != "IN_SYNC"
        ):
            actions.append(
                self._action(
                    25,
                    "AUTOMATION_SCHEDULER_NOT_IN_SYNC",
                    "OPERATIONS",
                    f"SCHEDULER_{operations['scheduler_status']}",
                    "automation_status_get",
                    {
                        "active_policy_count": workflows["active_automation_policy_count"],
                        "scheduler_status": operations["scheduler_status"],
                    },
                )
            )
        if int(operations["due_retry_count"]) or int(operations["missed_run_count"]):
            actions.append(
                self._action(
                    30,
                    "AUTOMATION_RECOVERY_DUE",
                    "OPERATIONS",
                    "DUE_RETRIES_OR_MISSED_RUNS",
                    "automation_status_get",
                    {
                        "due_retry_count": int(operations["due_retry_count"]),
                        "missed_run_count": int(operations["missed_run_count"]),
                    },
                )
            )
        review_required = int(workflows["sell_proposal_counts"].get("REVIEW_REQUIRED", 0))
        if review_required:
            actions.append(
                self._action(
                    40,
                    "SELL_PROPOSALS_REQUIRE_REVIEW",
                    "USER_REVIEW",
                    "RULE_PROPOSALS_NOT_EXECUTED",
                    "sell_proposal_list",
                    {"count": review_required},
                )
            )
        draft_count = int(workflows["plan_counts"].get("DRAFT", 0))
        frozen_count = int(workflows["plan_counts"].get("FROZEN", 0))
        if draft_count or frozen_count:
            actions.append(
                self._action(
                    50,
                    "WEEKLY_PLANS_AWAIT_USER_STATE",
                    "USER_REVIEW",
                    "PLAN_STATE_IS_NOT_BROKERAGE_EXECUTION",
                    "weekly_plan_list",
                    {"draft_count": draft_count, "frozen_count": frozen_count},
                )
            )
        open_actions = int(workflows["review_action_counts"].get("OPEN", 0))
        if open_actions:
            actions.append(
                self._action(
                    60,
                    "REVIEW_ACTIONS_OPEN",
                    "USER_REVIEW",
                    "PERIODIC_REVIEW_ACTIONS_REQUIRE_DECISION",
                    "periodic_review_list",
                    {"count": open_actions},
                )
            )
        exhausted = int(workflows["research_task_counts"].get("EXHAUSTED", 0))
        if exhausted:
            actions.append(
                self._action(
                    70,
                    "RESEARCH_TASKS_EXHAUSTED",
                    "RESEARCH",
                    "COLLECTION_RETRY_LIMIT_REACHED",
                    "research_collection_task_list",
                    {"count": exhausted},
                )
            )
        return sorted(actions, key=lambda item: (int(item["priority"]), str(item["code"])))

    @staticmethod
    def _daily_display(
        *,
        as_of_date: str,
        state: str,
        brief: JsonDict,
        actions: list[JsonDict],
        readiness: JsonDict,
    ) -> str:
        context = brief["context"]
        lines = [
            "Hermes 投资工作台",
            f"数据日期: {as_of_date}",
            f"工作台状态: {state}",
            f"组合: {context['portfolio']['name']} | 账户: {context['account']['name']}",
            f"估值数据质量: {brief['valuation']['data_quality']}",
            f"V1 就绪度: {readiness['status']}",
            "",
            "待处理事实:",
        ]
        if not actions:
            lines.append("- 当前没有 Core 识别出的待处理事实。")
        else:
            for index, action in enumerate(actions, start=1):
                lines.append(
                    f"{index}. {action['code']} | {action['category']} | "
                    f"{action['reason_code']} | 查看: {action['suggested_read_tool']}"
                )
        lines.extend(
            [
                "",
                "边界: 以上为确定性状态与工作流优先级，不是基金排名、投资建议或交易执行。",  # noqa: RUF001
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _readiness_display(*, as_of_date: str, readiness: JsonDict) -> str:
        lines = [
            "Value DCA V1 就绪度",
            f"数据日期: {as_of_date}",
            f"总体状态: {readiness['status']}",
            "",
            "检查项:",
        ]
        for item in readiness["checks"]:
            required = "必需" if item["required_for_v1"] else "可选"
            lines.append(
                f"- {item['code']} | {item['status']} | {required} | {item['reason_code']}"
            )
        lines.extend(
            [
                "",
                "边界: 就绪度只评价产品运行条件，不评价组合、策略、基金或投资结果。",  # noqa: RUF001
            ]
        )
        return "\n".join(lines)

    def get(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        as_of_date: date,
        view: str = "DAILY",
    ) -> JsonDict:
        normalized_view = view.strip().upper()
        if normalized_view not in {"DAILY", "READINESS", "FULL"}:
            raise LedgerError(
                "WORKSPACE_VIEW_INVALID",
                "view must be DAILY, READINESS or FULL",
            )
        brief = self._portfolio_brief(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date=as_of_date,
        )
        operations = self._operations.status_summary()
        workflows = self._workflow_facts(
            portfolio_id=portfolio_id,
            account_id=account_id,
        )
        runtime_mode = self._capital.runtime_mode(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            persist=False,
        )
        readiness = self._readiness(
            brief=brief,
            operations=operations,
            workflows=workflows,
            runtime_mode=runtime_mode,
        )
        actions = self._actions(
            brief=brief,
            operations=operations,
            workflows=workflows,
        )
        if int(operations["open_alert_count"]):
            state = "BLOCKED"
        elif not workflows["strategy_assignment_active"]:
            state = "SETUP_REQUIRED"
        elif str(brief["valuation"]["data_quality"]) == "SOURCE_ERROR":
            state = "BLOCKED"
        elif actions:
            state = "ACTION_REQUIRED"
        else:
            state = "READY"

        daily_display = self._daily_display(
            as_of_date=as_of_date.isoformat(),
            state=state,
            brief=brief,
            actions=actions,
            readiness=readiness,
        )
        readiness_display = self._readiness_display(
            as_of_date=as_of_date.isoformat(), readiness=readiness
        )
        if normalized_view == "DAILY":
            display_text = daily_display
        elif normalized_view == "READINESS":
            display_text = readiness_display
        else:
            display_text = f"{daily_display}\n\n{readiness_display}"

        return {
            "contract_version": "investment-workspace-v1",
            "view": normalized_view,
            "as_of_date": as_of_date.isoformat(),
            "state": state,
            "context": brief["context"],
            "valuation_summary": {
                "data_quality": brief["valuation"]["data_quality"],
                "warnings": brief["valuation"]["warnings"],
                "position_count": len(brief["valuation"]["positions"]),
                "totals": brief["valuation"]["totals"],
            },
            "runtime_mode": runtime_mode,
            "operations": {
                "open_alert_count": operations["open_alert_count"],
                "due_retry_count": operations["due_retry_count"],
                "missed_run_count": operations["missed_run_count"],
                "scheduler_status": operations["scheduler_status"],
                "pending_outbox_count": operations["pending_outbox_count"],
            },
            "workflows": workflows,
            "next_actions": actions,
            "v1_readiness": readiness,
            "narrative_contract": {
                "mode": "EXACT_TEXT",
                "response_field": "display_text",
                "additions_allowed": False,
            },
            "display_text": display_text,
            "automatic_trade": False,
            "financial_state_changed": False,
            "generated_at": self._now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
