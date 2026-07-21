"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Application settings shared by Core, CLI and MCP."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="INVESTOR_",
        extra="ignore",
    )

    app_name: str = "value-dca-agent"
    environment: Environment = Environment.DEVELOPMENT
    host: str = "127.0.0.1"
    port: int = Field(default=8710, ge=1, le=65535)
    db_path: Path = Path("data/investor.db")
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    expected_python_minor: str = "3.11"
    core_base_url: str = "http://127.0.0.1:8710"
    core_autostart: bool = False
    core_windows_task_name: str = ""
    project_root: Path = Path(".")
    core_start_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    core_log_path: Path = Path("logs/investor-core.log")
    confirmation_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    transaction_amount_tolerance_bps: int = Field(default=100, ge=0, le=1000)
    transaction_amount_tolerance_minor: int = Field(default=5, ge=0, le=10000)
    market_nav_max_age_days: int = Field(default=7, ge=0, le=31)

    @property
    def database_url(self) -> str:
        if str(self.db_path) == ":memory:":
            return "sqlite+pysqlite:///:memory:"
        return f"sqlite+pysqlite:///{self.db_path.resolve()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
