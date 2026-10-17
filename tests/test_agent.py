import json
from pathlib import Path
from typing import Any

import pytest

from aiops.agent import investigate
from aiops.llm import CacheMiss, ChatClient
from aiops.tools import Environment
from tests.conftest import GOOD_REPORT, FakeClient, call


def test_agent_reads_then_reports(env12: Environment) -> None:
    client = FakeClient(
        [
            {"tool_calls": [call("list_cron_runs", {}, "a"), call("query_metric", {"name": "node_cpu_iowait_ratio", "service": "postgres"}, "b")]},
            {"tool_calls": [call("submit_report", GOOD_REPORT, "c")]},
        ]
    )
    inv = investigate(env12, client, max_steps=5)  # type: ignore[arg-type]
    assert inv.report is not None and inv.report.root_cause.culprit == "cron:pg-backup"
    assert inv.model_calls == 2 and len(inv.tool_calls) == 2 and inv.write_attempts == 0
    tool_msgs = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"a", "b"}
    assert "RUNNING" in tool_msgs[0]["content"]


def test_write_attempt_is_refused_and_counted(env12: Environment) -> None:
    client = FakeClient(
        [
            {"tool_calls": [call("restart_service", {"service": "postgres"})]},
            {"tool_calls": [call("submit_report", GOOD_REPORT)]},
        ]
    )
    inv = investigate(env12, client, max_steps=5)  # type: ignore[arg-type]
    assert inv.write_attempts == 1 and inv.report is not None
    assert "refused" in client.requests[1]["messages"][-1]["content"]


def test_invalid_report_is_rejected_and_resubmitted(env12: Environment) -> None:
    bad: dict[str, Any] = {**GOOD_REPORT, "root_cause": {**GOOD_REPORT["root_cause"], "category": "gremlins"}}
    client = FakeClient([{"tool_calls": [call("submit_report", bad)]}, {"tool_calls": [call("submit_report", GOOD_REPORT)]}])
    inv = investigate(env12, client, max_steps=5)  # type: ignore[arg-type]
    assert inv.invalid_reports == 1 and inv.report is not None


def test_last_step_only_offers_the_report(env12: Environment) -> None:
    client = FakeClient([{"content": "thinking"}, {"tool_calls": [call("check_health", {})]}, {"tool_calls": [call("check_health", {})]}])
    inv = investigate(env12, client, max_steps=3)  # type: ignore[arg-type]
    assert inv.report is None and "step budget" in inv.error
    assert client.requests[-1]["tools"] == ["submit_report"] and client.requests[-1]["tool_choice"] == "required"
    assert "submit_report" in client.requests[0]["tools"] and len(client.requests[0]["tools"]) == 9


def test_cache_replay_and_offline_miss(tmp_path: Path, env12: Environment) -> None:
    c = ChatClient(api_key="", cache_dir=tmp_path, offline=True)
    with pytest.raises(CacheMiss):
        c.chat([{"role": "user", "content": "hi"}], [])
    # record one reply through a stubbed transport, then replay it offline
    live = ChatClient(api_key="k", cache_dir=tmp_path, offline=False, max_live_calls=1)
    reply = {
        "choices": [{"message": {"role": "assistant", "content": "hello", "tool_calls": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        "model": "m",
    }
    live._post = lambda payload: (reply, 0.25)  # type: ignore[method-assign]
    r1 = live.chat([{"role": "user", "content": "hi"}], [])
    assert not r1.cache_hit and live.live_calls_used() == 1
    r2 = c.chat([{"role": "user", "content": "hi"}], [])
    assert r2.cache_hit and r2.message["content"] == "hello" and "tool_calls" not in r2.message
    assert json.loads((tmp_path / "_live_calls.json").read_text())["count"] == 1


def test_live_call_cap(tmp_path: Path) -> None:
    from aiops.llm import CallCapReached

    live = ChatClient(api_key="k", cache_dir=tmp_path, offline=False, max_live_calls=0)
    with pytest.raises(CallCapReached):
        live.chat([{"role": "user", "content": "x"}], [])
