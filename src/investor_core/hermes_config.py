"""Atomic Hermes profile configuration used by the Windows installer."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Any

import yaml


def configure_investor_mcp(
    config_path: Path,
    *,
    project_root: Path,
    core_url: str,
    task_name: str,
) -> None:
    """Upsert only the managed investor_core MCP entry and preserve other settings."""
    config: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("Hermes config root must be a YAML mapping")
        config = loaded or {}

    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("Hermes mcp_servers must be a YAML mapping")

    existing = servers.get("investor_core", {})
    entry = dict(existing) if isinstance(existing, dict) else {}
    existing_env = entry.get("env", {})
    environment = dict(existing_env) if isinstance(existing_env, dict) else {}
    environment.update(
        {
            "INVESTOR_CORE_BASE_URL": core_url,
            "INVESTOR_CORE_AUTOSTART": "true",
            "INVESTOR_CORE_WINDOWS_TASK_NAME": task_name,
            "INVESTOR_PROJECT_ROOT": str(project_root.resolve()),
        }
    )

    for incompatible_key in ("url", "headers", "auth"):
        entry.pop(incompatible_key, None)
    # investor_core is a locally managed, versioned server. A tools.include list
    # written by an older interactive install would otherwise hide every tool
    # added by a later release.
    entry.pop("tools", None)
    entry.update(
        {
            "command": "uv",
            "args": [
                "--directory",
                str(project_root.resolve()),
                "run",
                "python",
                "-m",
                "investor_mcp.server",
            ],
            "env": environment,
            "enabled": True,
        }
    )
    servers["investor_core"] = entry

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_suffix(config_path.suffix + ".bak"))
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(temporary_path, config_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the Hermes Investor MCP entry.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--task-name", required=True)
    args = parser.parse_args()
    configure_investor_mcp(
        args.config,
        project_root=args.project_root,
        core_url=args.core_url,
        task_name=args.task_name,
    )
    print(f"Configured investor_core in {args.config}")


if __name__ == "__main__":
    main()
