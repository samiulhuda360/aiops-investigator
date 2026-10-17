import json
from pathlib import Path

import anyio
from mcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from aiops.agent import investigate
from aiops.mcp_server import build_server
from aiops.telemetry import JsonLinesSpanExporter, setup_tracing, shutdown
from aiops.tools import Environment
from tests.conftest import GOOD_REPORT, FakeClient, call


def test_mcp_server_exposes_only_read_only_tools(worlds: Path) -> None:
    server = build_server(worlds / "s12-backup-cron-noisy")

    async def go() -> None:
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 8
            for t in tools:
                assert t.annotations is not None and t.annotations.read_only_hint and not t.annotations.destructive_hint
            res = await client.call_tool("list_cron_runs", {"job": "pg-backup"})
            text = res.content[0].text  # type: ignore[union-attr]
            assert "RUNNING" in text

    anyio.run(go)


def test_every_step_and_tool_call_is_traced(tmp_path: Path, env12: Environment) -> None:
    trace_file = tmp_path / "t.jsonl"
    setup_tracing(tmp_path / "default.jsonl")  # the provider is configured once per process
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(JsonLinesSpanExporter(trace_file)))
    client = FakeClient([{"tool_calls": [call("check_health", {})]}, {"tool_calls": [call("submit_report", GOOD_REPORT)]}])
    investigate(env12, client, incident_id="trace-test")  # type: ignore[arg-type]
    shutdown()
    spans = [json.loads(ln) for ln in trace_file.read_text().splitlines()]
    root = [s for s in spans if s["name"] == "investigate" and s["attributes"].get("incident.id") == "trace-test"][-1]
    mine = [s for s in spans if s["trace_id"] == root["trace_id"]]
    names = [s["name"] for s in mine]
    assert names.count("llm.chat") == 2 and "tool check_health" in names and "agent.step 1" in names
    tool = next(s for s in mine if s["name"] == "tool check_health")
    assert tool["attributes"]["tool.read_only"] is True
    assert root["attributes"]["agent.write_attempts"] == 0
