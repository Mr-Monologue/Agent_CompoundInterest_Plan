"""Process-owned rotating log configuration for the Core HTTP service."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_uvicorn_log_config(log_path: Path, log_level: str) -> dict[str, Any]:
    """Build a file-only Uvicorn config safe for hidden Windows execution."""
    resolved_path = log_path.resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    level = log_level.upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": (
                    '%(asctime)s %(levelname)s %(client_addr)s "%(request_line)s" '
                    "%(status_code)s"
                ),
            },
        },
        "handlers": {
            "default_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "default",
                "filename": str(resolved_path),
                "maxBytes": 5 * 1024 * 1024,
                "backupCount": 3,
                "encoding": "utf-8",
            },
            "access_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "access",
                "filename": str(resolved_path),
                "maxBytes": 5 * 1024 * 1024,
                "backupCount": 3,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default_file"], "level": level, "propagate": False},
            "uvicorn.error": {
                "handlers": ["default_file"],
                "level": level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access_file"],
                "level": level,
                "propagate": False,
            },
        },
    }
