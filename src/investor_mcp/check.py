"""Read-only STDIO connection check, independent of Hermes or Codex."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from investor_core.version import __version__

REQUIRED_TOOLS = {
    "system_health_get",
    "investment_context_get",
    "investment_workspace_get",
    "portfolio_brief_get",
    "weekly_plan_preview",
    "weekly_no_investment_preview",
    "weekly_report_preview",
    "transaction_draft_commit",
}


async def check_connection(
    command: str,
    *,
    project_root: Path,
    core_url: str,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Only initialize, list tools and GET health/readiness; never auto-start Core."""
    environment = dict(os.environ)
    environment.update(
        {
            "INVESTOR_CORE_BASE_URL": core_url,
            "INVESTOR_PROJECT_ROOT": str(project_root),
            "INVESTOR_MCP_ACTOR_REF": "codex",
            "INVESTOR_CORE_AUTOSTART": "false",
        }
    )
    parameters = StdioServerParameters(
        command=command, args=args or [], env=environment, cwd=str(project_root)
    )
    async with stdio_client(parameters) as (reader, writer), ClientSession(
        reader, writer, read_timeout_seconds=timedelta(seconds=20)
    ) as session:
        initialized = await session.initialize()
        if not initialized.instructions:
            raise RuntimeError("MCP operating instructions are missing; upgrade the adapter.")
        listed = await session.list_tools()
        missing = REQUIRED_TOOLS - {tool.name for tool in listed.tools}
        if missing:
            raise RuntimeError(f"MCP tools are missing: {', '.join(sorted(missing))}")
        result = await session.call_tool("system_health_get", {"detail_level": "full"})
        if result.isError or not isinstance(result.structuredContent, dict):
            raise RuntimeError("MCP health check returned an invalid response.")
        payload = result.structuredContent
        if not payload.get("ok"):
            raise RuntimeError("Core is unavailable. Start/check the managed Core first.")
        data = payload.get("data", {})
        adapter = data.get("adapter", {})
        health = data.get("health", {})
        if adapter.get("actor_ref") != "codex":
            raise RuntimeError("MCP adapter does not support Codex audit attribution.")
        if adapter.get("version") != __version__ or health.get("version") != __version__:
            raise RuntimeError("Core and MCP versions differ; complete the authorized upgrade.")
        if data.get("ready", {}).get("status") != "PASS":
            raise RuntimeError("Core readiness failed; inspect the managed Core diagnostics.")
        return {
            "status": "PASS",
            "version": __version__,
            "actor_ref": "codex",
            "tool_count": len(listed.tools),
            "ready": "PASS",
            "business_writes": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--core-url", default="http://127.0.0.1:8710")
    arguments = parser.parse_args()
    root = arguments.project_root.resolve()
    executable = root / ".venv" / (
        "Scripts/investor-mcp.exe" if sys.platform == "win32" else "bin/investor-mcp"
    )
    if not executable.is_file():
        parser.error("Installed investor-mcp entry point is missing; finish installation first.")
    # An overall deadline also bounds initialization and child-process shutdown.
    async def run() -> dict[str, Any]:
        return await asyncio.wait_for(
            check_connection(str(executable), project_root=root, core_url=arguments.core_url),
            timeout=45,
        )

    print(json.dumps(asyncio.run(run()), ensure_ascii=False))


if __name__ == "__main__":
    main()
