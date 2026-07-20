from __future__ import annotations

from pathlib import Path

from investor_core.logging_config import build_uvicorn_log_config


def test_uvicorn_logging_is_file_owned_and_rotating(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "investor-core.log"

    config = build_uvicorn_log_config(log_path, "INFO")

    assert log_path.parent.exists()
    handler = config["handlers"]["default_file"]
    assert handler["class"] == "logging.handlers.RotatingFileHandler"
    assert handler["filename"] == str(log_path.resolve())
    assert handler["backupCount"] == 3
    assert config["formatters"]["access"]["()"] == "uvicorn.logging.AccessFormatter"
    assert config["loggers"]["uvicorn.error"]["propagate"] is False
