"""The read-only investigation tools as an MCP server, for any MCP-capable assistant.

    AIOPS_WORLD=var/worlds/s12-backup-cron-noisy python -m aiops.mcp_server

Every tool is annotated read-only and non-destructive (``readOnlyHint`` and ``destructiveHint`` on the wire).
The server exposes exactly the eight read-only tools from ``aiops.tools``; nothing on it can change the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from aiops import tools as t

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_server(world: Path) -> MCPServer:
    env = t.Environment(world)
    server = MCPServer(
        name="aiops-investigator",
        instructions="Read-only tools for investigating a production alert: logs, metrics, changes, cron runs, health, runbooks.",
    )
    desc = {s["name"]: s["description"] for s in t.TOOL_SPECS}

    def run(tool: str, **kwargs: object) -> str:
        return t.dispatch(env, tool, {k: v for k, v in kwargs.items() if v is not None}).output

    @server.tool(description=desc["check_health"], annotations=READ_ONLY)
    def check_health(service: str = "") -> str:
        return run("check_health", service=service)

    @server.tool(description=desc["query_logs"], annotations=READ_ONLY)
    def query_logs(service: str, pattern: str = "", level: str = "", since_minutes: int = 60, until_minutes: int = 0, limit: int = 12) -> str:
        return run("query_logs", service=service, pattern=pattern, level=level, since_minutes=since_minutes, until_minutes=until_minutes, limit=limit)

    @server.tool(description=desc["list_metrics"], annotations=READ_ONLY)
    def list_metrics(service: str) -> str:
        return run("list_metrics", service=service)

    @server.tool(description=desc["query_metric"], annotations=READ_ONLY)
    def query_metric(name: str, service: str, since_minutes: int = 180, step_minutes: int = 15) -> str:
        return run("query_metric", name=name, service=service, since_minutes=since_minutes, step_minutes=step_minutes)

    @server.tool(description=desc["list_changes"], annotations=READ_ONLY)
    def list_changes(service: str = "", since_hours: int = 24) -> str:
        return run("list_changes", service=service, since_hours=since_hours)

    @server.tool(description=desc["get_change"], annotations=READ_ONLY)
    def get_change(change_id: str) -> str:
        return run("get_change", change_id=change_id)

    @server.tool(description=desc["list_cron_runs"], annotations=READ_ONLY)
    def list_cron_runs(job: str = "", since_hours: int = 24) -> str:
        return run("list_cron_runs", job=job, since_hours=since_hours)

    @server.tool(description=desc["read_runbook"], annotations=READ_ONLY)
    def read_runbook(name: str) -> str:
        return run("read_runbook", name=name)

    return server


def main() -> None:
    world = Path(os.environ.get("AIOPS_WORLD", "var/active"))
    if not (world / "meta.json").exists():
        raise SystemExit(f"no environment at {world}; run `aiops env build` or `aiops inject <id>` first, or set AIOPS_WORLD")
    build_server(world).run("stdio")


if __name__ == "__main__":
    main()
