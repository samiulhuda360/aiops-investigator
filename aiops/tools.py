"""Read-only investigation tools over one environment snapshot.

These eight functions are the only way the agent can look at the environment. Each one reads files (or, in live
mode, calls a service's ``/health`` endpoint) and returns text. None of them can change anything: there is no
write, restart, exec or shell tool to call, and the dispatcher refuses any tool name that is not on the read-only
list and counts the attempt.

The same functions back the MCP server (``aiops.mcp_server``), so any MCP-capable assistant gets the same view.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from aiops.sim.world import SERVICES, parse_ts

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "runbooks"
LINE_RE = re.compile(r"^(\S+) (\S+)\s+(.*)$")
MASK_RE = re.compile(r"\b[0-9a-f]{8}\b|\d+(?:\.\d+)?")


class ToolError(ValueError):
    """A bad argument. The message goes back to the model so it can correct itself."""


@dataclass
class Environment:
    """One environment snapshot on disk, plus optional live health endpoints."""

    root: Path
    health_urls: dict[str, str] = field(default_factory=dict)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def _json(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = json.loads((self.root / name).read_text(encoding="utf-8"))
        return self._cache[name]

    @property
    def now(self) -> datetime:
        return parse_ts(self._json("meta.json")["now"])

    def alert(self) -> dict[str, Any]:
        data: dict[str, Any] = self._json("alert.json")
        return data

    def log_lines(self, service: str) -> list[tuple[datetime, str, str, str]]:
        key = f"logs:{service}"
        if key not in self._cache:
            rows = []
            for raw in (self.root / "logs" / f"{service}.log").read_text(encoding="utf-8").splitlines():
                m = LINE_RE.match(raw)
                if m:
                    rows.append((parse_ts(m.group(1)), m.group(2), m.group(3), raw))
            self._cache[key] = rows
        result: list[tuple[datetime, str, str, str]] = self._cache[key]
        return result

    def ago(self, ts: datetime) -> str:
        minutes = int((self.now - ts).total_seconds() // 60)
        if minutes < 120:
            return f"{minutes}m ago"
        if minutes < 2880:
            return f"{minutes / 60:.1f}h ago"
        return f"{minutes / 1440:.1f}d ago"


def _service(name: str, allow_empty: bool = False) -> str:
    name = (name or "").strip().lower()
    if allow_empty and not name:
        return ""
    if name not in SERVICES:
        raise ToolError(f"unknown service {name!r}; services are {', '.join(SERVICES)}")
    return name


def _fmt(v: float) -> str:
    if abs(v) >= 1024**3:
        return f"{v / 1024**3:.2f}Gi"
    if abs(v) >= 1024**2:
        return f"{v / 1024**2:.0f}Mi"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


# --------------------------------------------------------------------------- the tools
def check_health(env: Environment, service: str = "") -> str:
    svc = _service(service, allow_empty=True)
    targets = [svc] if svc else SERVICES
    out = []
    for s in targets:
        if s in env.health_urls:
            try:
                r = httpx.get(env.health_urls[s].rstrip("/") + "/health", timeout=3.0)
                data = r.json()
                out.append(f"{s}: {data.get('status', '?')} ({data.get('detail', '')})")
            except httpx.HTTPError as exc:
                out.append(f"{s}: unreachable ({type(exc).__name__})")
        else:
            h = env._json("health.json").get(s, {"status": "unknown", "detail": ""})
            out.append(f"{s}: {h['status']} ({h['detail']})")
    return "\n".join(out)


def query_logs(env: Environment, service: str, pattern: str = "", level: str = "", since_minutes: int = 60, until_minutes: int = 0, limit: int = 12) -> str:
    svc = _service(service)
    since_minutes = max(1, min(int(since_minutes), 1440))
    until_minutes = max(0, min(int(until_minutes), since_minutes))
    limit = max(1, min(int(limit), 30))
    start, end = env.now - timedelta(minutes=since_minutes), env.now - timedelta(minutes=until_minutes)
    try:
        rx = re.compile(pattern, re.IGNORECASE) if pattern else None
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    levels = {lv.strip().upper() for lv in level.split(",") if lv.strip()} if level else set()
    groups: dict[str, list[Any]] = {}
    matched = 0
    for ts, lv, msg, raw in env.log_lines(svc):
        if ts < start or ts > end or (levels and lv not in levels) or (rx and not rx.search(msg)):
            continue
        matched += 1
        key = f"{lv} {MASK_RE.sub('#', msg)}"
        g = groups.setdefault(key, [0, ts, ts, raw])
        g[0] += 1
        g[2], g[3] = ts, raw  # keep the most recent example
    head = f"{svc} logs, last {since_minutes}m" + (f" until {until_minutes}m ago" if until_minutes else "")
    head += f", pattern={pattern!r}" if pattern else ""
    head += f", level={level}" if level else ""
    if not matched:
        return head + ": no matching lines"
    ranked = sorted(groups.values(), key=lambda g: (-g[0], g[1]))
    lines = [f"{head}: {matched} lines in {len(groups)} distinct patterns (showing {min(limit, len(ranked))}, most frequent first)"]
    for count, first, last, raw in ranked[:limit]:
        span = f"first {env.ago(first)}, last {env.ago(last)}" if count > 1 else env.ago(first)
        lines.append(f"[x{count} | {span}] {raw}")  # raw is the latest matching line
    return "\n".join(lines)


def list_metrics(env: Environment, service: str) -> str:
    svc = _service(service)
    names = sorted(m for m, by in env._json("metrics.json").items() if svc in by)
    return f"metrics for {svc}: " + ", ".join(names)


def query_metric(env: Environment, name: str, service: str, since_minutes: int = 180, step_minutes: int = 15) -> str:
    svc = _service(service)
    metrics = env._json("metrics.json")
    if name not in metrics or svc not in metrics[name]:
        return f"no metric {name!r} for {svc}. " + list_metrics(env, svc)
    values: list[float] = metrics[name][svc]
    since_minutes = max(5, min(int(since_minutes), len(values)))
    step = max(1, min(int(step_minutes), 60))
    window = values[-since_minutes:]
    points = []
    for i in range(0, len(window), step):
        chunk = window[i : i + step]
        ago = since_minutes - i
        points.append(f"-{ago}m={_fmt(sum(chunk) / len(chunk))}")
    baseline = values[: max(1, len(values) - since_minutes)][-60:] if len(values) > since_minutes else values[:15]
    return (
        f"{name}{{service={svc}}} last {since_minutes}m, avg per {step}m step: "
        + " ".join(points)
        + f"\n  now={_fmt(window[-1])} min={_fmt(min(window))} max={_fmt(max(window))}"
        + f" | baseline before window avg={_fmt(sum(baseline) / len(baseline))}"
    )


def list_changes(env: Environment, service: str = "", since_hours: int = 24) -> str:
    svc = _service(service, allow_empty=True)
    since_hours = max(1, min(int(since_hours), 168))
    cutoff = env.now - timedelta(hours=since_hours)
    rows = [c for c in env._json("changes.json") if parse_ts(c["ts"]) >= cutoff and (not svc or c["service"] == svc)]
    if not rows:
        return f"no changes in the last {since_hours}h" + (f" for {svc}" if svc else "")
    out = [f"changes in the last {since_hours}h" + (f" for {svc}" if svc else "") + " (newest first):"]
    for c in reversed(rows):
        out.append(f"{c['id']} {c['ts']} ({env.ago(parse_ts(c['ts']))}) [{c['service']}/{c['kind']}] by {c['author']}: {c['summary']}")
    return "\n".join(out)


def get_change(env: Environment, change_id: str) -> str:
    for c in env._json("changes.json"):
        if c["id"] == change_id.strip():
            return f"{c['id']} [{c['service']}/{c['kind']}] {c['ts']} by {c['author']}: {c['summary']}\n{c['diff']}"
    raise ToolError(f"no change {change_id!r}; use list_changes to see ids")


def list_cron_runs(env: Environment, job: str = "", since_hours: int = 24) -> str:
    since_hours = max(1, min(int(since_hours), 168))
    cutoff = env.now - timedelta(hours=since_hours)
    rows = [r for r in env._json("cron.json") if parse_ts(r["ts"]) >= cutoff and (not job or r["job"] == job)]
    if not rows:
        return "no cron runs found" + (f" for {job}" if job else "")
    out = [f"cron runs in the last {since_hours}h (newest first):"]
    for r in reversed(rows):
        status = "RUNNING" if r["exit_code"] == -1 else f"exit={r['exit_code']}"
        dur = "" if r["exit_code"] == -1 else f" in {r['duration_s']}s"
        out.append(f"cron:{r['job']} on {r['host']} started {r['ts']} ({env.ago(parse_ts(r['ts']))}) {status}{dur}")
        out.extend("    | " + ln for ln in r["output"].splitlines())
    return "\n".join(out)


def read_runbook(env: Environment, name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]", "", name)
    path = RUNBOOK_DIR / f"{slug}.md"
    if slug and path.is_file():
        return path.read_text(encoding="utf-8")
    available = sorted(p.stem for p in RUNBOOK_DIR.glob("*.md"))
    return f"no runbook {name!r}. Available: {', '.join(available)}"


# --------------------------------------------------------------------------- registry
_S = {"type": "string"}
_I = {"type": "integer"}
_SVC = {"type": "string", "enum": SERVICES}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "check_health",
        "description": "Current health check result for one service, or all services if service is empty.",
        "parameters": {"type": "object", "properties": {"service": _S}},
    },
    {
        "name": "query_logs",
        "description": "Search one service's logs in a time window. Lines are grouped into patterns with counts; each shows one exact example line. pattern is a case-insensitive regex; level filters e.g. 'ERROR' or 'WARN,ERROR'.",
        "parameters": {
            "type": "object",
            "properties": {"service": _SVC, "pattern": _S, "level": _S, "since_minutes": _I, "until_minutes": _I, "limit": _I},
            "required": ["service"],
        },
    },
    {
        "name": "list_metrics",
        "description": "List the metric names available for a service.",
        "parameters": {"type": "object", "properties": {"service": _SVC}, "required": ["service"]},
    },
    {
        "name": "query_metric",
        "description": "A metric's values over a window, averaged per step, with now/min/max and the baseline before the window.",
        "parameters": {"type": "object", "properties": {"name": _S, "service": _SVC, "since_minutes": _I, "step_minutes": _I}, "required": ["name", "service"]},
    },
    {
        "name": "list_changes",
        "description": "Recent deploys, config changes and infrastructure changes (newest first), optionally for one service.",
        "parameters": {"type": "object", "properties": {"service": _S, "since_hours": _I}},
    },
    {
        "name": "get_change",
        "description": "The full diff of one change by id (e.g. chg-1234).",
        "parameters": {"type": "object", "properties": {"change_id": _S}, "required": ["change_id"]},
    },
    {
        "name": "list_cron_runs",
        "description": "Recent scheduled job runs with exit codes and output, optionally for one job.",
        "parameters": {"type": "object", "properties": {"job": _S, "since_hours": _I}},
    },
    {
        "name": "read_runbook",
        "description": "Read the team runbook for an alert name (e.g. HighLatency).",
        "parameters": {"type": "object", "properties": {"name": _S}, "required": ["name"]},
    },
]

_FUNCS: dict[str, Callable[..., str]] = {
    "check_health": check_health,
    "query_logs": query_logs,
    "list_metrics": list_metrics,
    "query_metric": query_metric,
    "list_changes": list_changes,
    "get_change": get_change,
    "list_cron_runs": list_cron_runs,
    "read_runbook": read_runbook,
}
READ_ONLY_TOOLS = frozenset(_FUNCS)


@dataclass
class ToolResult:
    name: str
    output: str
    ok: bool
    refused: bool = False


def dispatch(env: Environment, name: str, args: dict[str, Any]) -> ToolResult:
    """Run one tool call. Anything that is not a read-only tool is refused, never executed."""
    if name not in READ_ONLY_TOOLS:
        return ToolResult(
            name, f"refused: {name!r} is not an available read-only tool. Changes are proposed in the report for a person to approve.", False, True
        )
    spec = next(s for s in TOOL_SPECS if s["name"] == name)
    allowed = set(spec["parameters"]["properties"])
    clean = {k: v for k, v in args.items() if k in allowed}
    try:
        return ToolResult(name, _FUNCS[name](env, **clean), True)
    except (ToolError, TypeError, ValueError) as exc:
        return ToolResult(name, f"error: {exc}", False)
