from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiops.llm import ChatResult
from aiops.sim.scenarios import build
from aiops.tools import Environment


@pytest.fixture(scope="session")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("worlds")
    for sid in ("s01-api-db-host-typo", "s12-backup-cron-noisy", "s18-worker-oom-limit", "s04-nginx-logrotate-broken"):
        w, _ = build(sid)
        w.save(root / sid)
    return root


@pytest.fixture
def env12(worlds: Path) -> Environment:
    return Environment(worlds / "s12-backup-cron-noisy")


def call(name: str, args: dict[str, Any], cid: str = "c1") -> dict[str, Any]:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


GOOD_REPORT: dict[str, Any] = {
    "what_broke": "API p95 latency rose to 2.9s.",
    "affected_service": "api",
    "root_cause": {"category": "noisy_neighbour", "culprit": "cron:pg-backup", "explanation": "The backup moved into business hours."},
    "confidence": 0.85,
    "evidence": [
        {"kind": "metric", "service": "postgres", "ref": "node_cpu_iowait_ratio", "observation": "0.03 -> 0.62"},
        {"kind": "cron", "service": "postgres", "ref": "pg-backup", "observation": "running since 09:45"},
    ],
    "proposed_fix": {"summary": "Move pg-backup back to 02:00.", "commands": ["crontab -e"], "rollback": ["crontab -e"], "risk": "low"},
}


class FakeClient:
    """Plays back a fixed list of assistant messages."""

    model = "fake-model"

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str = "auto") -> ChatResult:
        self.requests.append({"messages": list(messages), "tools": [t["name"] for t in tools], "tool_choice": tool_choice})
        msg = self.script.pop(0)
        return ChatResult({"role": "assistant", **msg}, {"prompt_tokens": 100, "completion_tokens": 20}, 0.5, False, self.model)
