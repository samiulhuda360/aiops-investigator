"""Connect to the MCP server over stdio, the way an MCP-capable assistant does, and call two tools.

python examples/mcp_client.py var/worlds/s12-backup-cron-noisy
"""

from __future__ import annotations

import os
import sys

import anyio
from mcp import Client, StdioServerParameters


async def main(world: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "aiops.mcp_server"], env={**os.environ, "AIOPS_WORLD": world})
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        print(f"{len(tools)} tools from the aiops-investigator MCP server:")
        for t in tools:
            ro = t.annotations.read_only_hint if t.annotations else None
            print(f"  {t.name:15} read_only={ro}  {(t.description or '')[:70]}")
        for name, args in (
            ("list_cron_runs", {"job": "pg-backup"}),
            ("query_metric", {"name": "node_cpu_iowait_ratio", "service": "postgres", "step_minutes": 30}),
        ):
            res = await client.call_tool(name, args)
            print(f"\n> {name}({args})")
            for block in res.content:
                print(getattr(block, "text", block))


if __name__ == "__main__":
    anyio.run(main, sys.argv[1] if len(sys.argv) > 1 else "var/worlds/s12-backup-cron-noisy")
