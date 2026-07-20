"""Health and readiness check models."""

from __future__ import annotations

import platform
import sys
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from investor_core.config import Environment, Settings
from investor_core.database import check_database
from investor_core.version import __version__


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DoctorReport(BaseModel):
    status: str
    version: str
    checks: list[CheckResult]


def check_python(settings: Settings) -> CheckResult:
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual == settings.expected_python_minor:
        return CheckResult(
            name="python",
            status=CheckStatus.PASS,
            message=f"Python {actual} matches the production baseline",
        )

    status = (
        CheckStatus.FAIL if settings.environment == Environment.PRODUCTION else CheckStatus.WARN
    )
    return CheckResult(
        name="python",
        status=status,
        message=(
            f"Python {actual} is running; production requires {settings.expected_python_minor}"
        ),
        details={"implementation": platform.python_implementation()},
    )


def check_timezone(settings: Settings) -> CheckResult:
    try:
        ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        return CheckResult(
            name="business-timezone",
            status=CheckStatus.FAIL,
            message=f"Business timezone is unavailable: {settings.timezone}",
            details={"timezone": settings.timezone},
        )
    return CheckResult(
        name="business-timezone",
        status=CheckStatus.PASS,
        message=f"Business timezone is available: {settings.timezone}",
        details={"timezone": settings.timezone},
    )


def build_doctor_report(settings: Settings) -> DoctorReport:
    checks = [check_python(settings), check_timezone(settings), *check_database(settings)]
    statuses = {check.status for check in checks}
    if CheckStatus.FAIL in statuses:
        overall = "FAIL"
    elif CheckStatus.WARN in statuses:
        overall = "DEGRADED"
    else:
        overall = "PASS"
    return DoctorReport(status=overall, version=__version__, checks=checks)
