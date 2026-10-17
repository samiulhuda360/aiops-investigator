"""The investigation loop: a tool-calling model, a step budget and a validated JSON report.

The model sees the alert, the eight read-only tools and one ``submit_report`` tool. Each turn it may call several
tools at once. On the last turn only ``submit_report`` is offered and a tool call is required, so every run ends
with a report or a recorded failure. Any tool name that is not read-only is refused and counted as a write attempt.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from aiops.llm import ChatClient
from aiops.report import REPORT_TOOL, IncidentReport
from aiops.sim.scenarios import CATEGORIES
from aiops.telemetry import tracer
from aiops.tools import READ_ONLY_TOOLS, TOOL_SPECS, Environment, dispatch

SYSTEM_PROMPT = """You are an on-call site reliability engineer investigating a production alert for a small online shop
(nginx -> api -> postgres, plus a background worker). You can only READ: logs, metrics, change history, cron runs,
health and runbooks. You cannot change anything; a person will review and approve any fix you propose.

Platform: Kubernetes namespace "shop" runs the nginx, api and worker deployments; postgres is a VM. Every change id
(chg-1234) is a git commit in the infra repo, applied by CI; reverting it means "git revert" plus a CI deploy.
Cron jobs run from the crontab on the host shown by list_cron_runs.

How to work:
- You have at most {steps} turns. Call several tools in the same turn when you can.
- Look beyond the alerting service: the cause is often a dependency, a change on another service, or a scheduled job.
- Line up when the symptom started with recent changes and cron runs. A change is only the culprit if its timing
  and content explain the symptom; recent harmless changes are common.
- Then call submit_report.
  - Evidence (3 to 6 items) must be real and must support the cause. Always set "service". For a log, "ref" is one
    log line copied exactly as the tool printed it (without the [xN | ...] prefix). For a metric, "ref" is the
    metric name and "observation" gives the values. For a change, "ref" is the change id; for cron, the job name.
  - culprit is the change id that caused it (chg-1234), "cron:<job>" for a scheduled job, "external:<name>" for a
    third party, or "none".
  - The fix: exact commands or a config diff, plus the rollback. Prefer the smallest reversible fix.
  - confidence: 0.9 or more only when logs, metrics and a change or job all agree.

Root-cause categories:
{categories}"""


@dataclass
class ToolCallRecord:
    step: int
    name: str
    args: dict[str, Any]
    ok: bool
    refused: bool
    chars: int


@dataclass
class Investigation:
    report: IncidentReport | None = None
    error: str = ""
    model_calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_latency_s: float = 0.0
    tool_time_s: float = 0.0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    write_attempts: int = 0
    invalid_reports: int = 0
    model: str = ""

    @property
    def time_to_report_s(self) -> float:
        """Model latency (as recorded live) plus tool time. Excludes the free-tier rate-limit pauses."""
        return self.model_latency_s + self.tool_time_s


def system_prompt(max_steps: int) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    return SYSTEM_PROMPT.format(steps=max_steps, categories=cats)


def alert_message(env: Environment) -> str:
    alert = env.alert()
    return (
        f"Alert received at {env.now:%Y-%m-%dT%H:%M:%SZ} (this is 'now' for all tools):\n"
        + json.dumps({k: alert[k] for k in ("status", "labels", "annotations", "startsAt")}, indent=1)
        + "\nInvestigate and submit a report."
    )


def investigate(env: Environment, client: ChatClient, max_steps: int = 5, incident_id: str = "") -> Investigation:
    inv = Investigation()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(max_steps)},
        {"role": "user", "content": alert_message(env)},
    ]
    alert = env.alert()
    with tracer().start_as_current_span("investigate") as root:
        root.set_attribute("incident.id", incident_id)
        root.set_attribute("alert.name", alert["labels"].get("alertname", ""))
        root.set_attribute("alert.service", alert["labels"].get("service", ""))
        root.set_attribute("agent.max_steps", max_steps)
        for step in range(1, max_steps + 1):
            last = step == max_steps
            tools = [REPORT_TOOL] if last else [*TOOL_SPECS, REPORT_TOOL]
            with tracer().start_as_current_span(f"agent.step {step}") as span:
                span.set_attribute("agent.step", step)
                with tracer().start_as_current_span("llm.chat") as llm_span:
                    llm_span.set_attribute("gen_ai.system", "openai-compatible")
                    llm_span.set_attribute("gen_ai.request.model", client.model)
                    res = client.chat(messages, tools, tool_choice="required" if last else "auto")
                    llm_span.set_attribute("gen_ai.usage.input_tokens", res.usage["prompt_tokens"])
                    llm_span.set_attribute("gen_ai.usage.output_tokens", res.usage["completion_tokens"])
                    llm_span.set_attribute("llm.cache_hit", res.cache_hit)
                    llm_span.set_attribute("llm.recorded_latency_s", round(res.latency_s, 3))
                inv.model_calls += 1
                inv.cache_hits += int(res.cache_hit)
                inv.prompt_tokens += res.usage["prompt_tokens"]
                inv.completion_tokens += res.usage["completion_tokens"]
                inv.model_latency_s += res.latency_s
                inv.model = res.model
                msg = res.message
                messages.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    messages.append({"role": "user", "content": "Use the tools, then call submit_report."})
                    continue
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if name == "submit_report":
                        try:
                            inv.report = IncidentReport.model_validate(args)
                            out = "report accepted"
                        except ValidationError as exc:
                            inv.invalid_reports += 1
                            out = f"report rejected, fix and resubmit: {exc.errors()[:3]}"
                    else:
                        t0 = time.perf_counter()
                        with tracer().start_as_current_span(f"tool {name}") as tspan:
                            result = dispatch(env, name, args)
                            tspan.set_attribute("tool.name", name)
                            tspan.set_attribute("tool.args", json.dumps(args, sort_keys=True))
                            tspan.set_attribute("tool.read_only", name in READ_ONLY_TOOLS)
                            tspan.set_attribute("tool.result_chars", len(result.output))
                            if result.refused:
                                tspan.set_status(Status(StatusCode.ERROR, "refused: not a read-only tool"))
                        inv.tool_time_s += time.perf_counter() - t0
                        inv.write_attempts += int(result.refused)
                        inv.tool_calls.append(ToolCallRecord(step, name, args, result.ok, result.refused, len(result.output)))
                        out = result.output
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name, "content": out})
                if inv.report is not None:
                    break
        if inv.report is None:
            inv.error = "no valid report within the step budget"
            root.set_status(Status(StatusCode.ERROR, inv.error))
        else:
            root.set_attribute("report.category", inv.report.root_cause.category)
            root.set_attribute("report.culprit", inv.report.root_cause.culprit)
            root.set_attribute("report.confidence", inv.report.confidence)
        root.set_attribute("agent.model_calls", inv.model_calls)
        root.set_attribute("agent.tool_calls", len(inv.tool_calls))
        root.set_attribute("agent.write_attempts", inv.write_attempts)
    return inv
