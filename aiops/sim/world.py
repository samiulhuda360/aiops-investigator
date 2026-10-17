"""The simulated environment: logs, metrics, change history, cron runs and health for four services.

A ``World`` is one snapshot of an observability stack at the moment an alert fires. It is written to a directory
in plain files (one log file per service, a metrics file, a change log, cron history, health) so the investigation
tools, the plain-process services and a person with ``grep`` all read the same data.

Nothing in a world directory says which lines are the real signal. The labels live in a separate file that only
the evaluator reads.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVICES = ["nginx", "api", "worker", "postgres"]
WINDOW_MIN = 360  # six hours of per-minute metrics
NOW = datetime(2026, 9, 14, 10, 30, tzinfo=UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@dataclass
class LogLine:
    ts: datetime
    service: str
    level: str
    msg: str
    signal: bool = False

    def render(self) -> str:
        return f"{iso(self.ts)} {self.level:<5} {self.msg}"


@dataclass
class Change:
    id: str
    ts: datetime
    service: str
    kind: str  # deploy | config | infra
    author: str
    summary: str
    diff: str
    signal: bool = False


@dataclass
class CronRun:
    job: str
    host: str
    ts: datetime
    exit_code: int
    duration_s: float
    output: str
    signal: bool = False


@dataclass
class Label:
    """The ground truth for one injected incident (never shown to the agent)."""

    id: str
    title: str
    category: str
    culprits: list[str]  # accepted culprit ids: a change id, "cron:<job>", "external:<name>" or "none"
    service: str
    signal_metrics: list[str] = field(default_factory=list)  # "metric@service"
    signal_changes: list[str] = field(default_factory=list)
    signal_cron: list[str] = field(default_factory=list)
    signal_health: list[str] = field(default_factory=list)
    fix_hint: str = ""


@dataclass
class World:
    now: datetime
    rng: random.Random
    logs: list[LogLine] = field(default_factory=list)
    metrics: dict[str, dict[str, list[float]]] = field(default_factory=dict)  # metric -> service -> values (oldest first)
    changes: list[Change] = field(default_factory=list)
    cron: list[CronRun] = field(default_factory=list)
    health: dict[str, dict[str, str]] = field(default_factory=dict)
    alert: dict[str, Any] = field(default_factory=dict)

    # ----- builders used by the fault injector -----
    def at(self, minutes_ago: float) -> datetime:
        return self.now - timedelta(minutes=minutes_ago)

    def series(self, metric: str, service: str, base: float, noise: float = 0.05, floor: float = 0.0) -> None:
        values = []
        for i in range(WINDOW_MIN):
            diurnal = 1.0 + 0.08 * math.sin((i / WINDOW_MIN) * math.pi)
            values.append(max(floor, base * diurnal * (1.0 + self.rng.uniform(-noise, noise))))
        self.metrics.setdefault(metric, {})[service] = values

    def shape(self, metric: str, service: str, start_ago: int, fn: Callable[[float, float], float]) -> None:
        """Rewrite a series from ``start_ago`` minutes ago to now. ``fn(minutes_since_start, old) -> new``."""
        values = self.metrics[metric][service]
        start = max(0, WINDOW_MIN - start_ago)
        for i in range(start, WINDOW_MIN):
            values[i] = fn(float(i - start), values[i])

    def emit(
        self,
        service: str,
        start_ago: float,
        end_ago: float,
        every_s: float,
        level: str,
        template: str,
        signal: bool = True,
        jitter: float = 0.3,
    ) -> None:
        t = self.at(start_ago)
        end = self.at(end_ago)
        n = 0
        while t <= end:
            msg = template.format(n=n, rid=f"{self.rng.randrange(16**8):08x}", ms=self.rng.randint(1800, 9500), job=self.rng.randint(40000, 99999))
            self.logs.append(LogLine(t, service, level, msg, signal))
            n += 1
            t += timedelta(seconds=every_s * (1.0 + self.rng.uniform(-jitter, jitter)))

    def drop_logs(self, service: str, start_ago: float) -> None:
        """Remove background lines from a service after a point in time (for a service that went quiet)."""
        cut = self.at(start_ago)
        self.logs = [ln for ln in self.logs if not (ln.service == service and ln.ts >= cut and not ln.signal)]

    def log(self, service: str, minutes_ago: float, level: str, msg: str, signal: bool = True) -> None:
        self.logs.append(LogLine(self.at(minutes_ago), service, level, msg, signal))

    def change_id(self) -> str:
        used = {c.id for c in self.changes}
        while True:
            cid = f"chg-{self.rng.randint(1000, 9999)}"
            if cid not in used:
                return cid

    def add_change(self, minutes_ago: float, service: str, kind: str, author: str, summary: str, diff: str, signal: bool = True) -> str:
        cid = self.change_id()
        self.changes.append(Change(cid, self.at(minutes_ago), service, kind, author, summary, diff.strip("\n"), signal))
        return cid

    def set_health(self, service: str, status: str, detail: str) -> None:
        self.health[service] = {"status": status, "detail": detail}

    def set_alert(self, name: str, service: str, severity: str, minutes_ago: int, summary: str, description: str) -> None:
        self.alert = {
            "status": "firing",
            "labels": {"alertname": name, "service": service, "severity": severity, "instance": f"{service}-0", "env": "prod"},
            "annotations": {"summary": summary, "description": description},
            "startsAt": iso(self.at(minutes_ago)),
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": f"http://prometheus:9090/graph?g0.expr={name}",
            "fingerprint": f"{self.rng.randrange(16**16):016x}",
        }

    # ----- persistence -----
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "logs").mkdir(exist_ok=True)
        for svc in SERVICES:
            lines = sorted((ln for ln in self.logs if ln.service == svc), key=lambda ln: ln.ts)
            (directory / "logs" / f"{svc}.log").write_text("\n".join(ln.render() for ln in lines) + "\n", encoding="utf-8")
        metrics = {m: {s: [round(v, 6) for v in vals] for s, vals in by_svc.items()} for m, by_svc in self.metrics.items()}
        meta = {"now": iso(self.now), "window_minutes": WINDOW_MIN, "services": SERVICES}
        changes = [
            {"id": c.id, "ts": iso(c.ts), "service": c.service, "kind": c.kind, "author": c.author, "summary": c.summary, "diff": c.diff}
            for c in sorted(self.changes, key=lambda c: c.ts)
        ]
        cron = [
            {"job": r.job, "host": r.host, "ts": iso(r.ts), "exit_code": r.exit_code, "duration_s": r.duration_s, "output": r.output}
            for r in sorted(self.cron, key=lambda r: r.ts)
        ]
        _dump(directory / "meta.json", meta)
        _dump(directory / "metrics.json", metrics, indent=None)
        _dump(directory / "changes.json", changes)
        _dump(directory / "cron.json", cron)
        _dump(directory / "health.json", self.health)
        _dump(directory / "alert.json", self.alert)


def _dump(path: Path, data: Any, indent: int | None = 1) -> None:
    path.write_text(json.dumps(data, indent=indent, sort_keys=False) + "\n", encoding="utf-8")


def label_to_dict(label: Label) -> dict[str, Any]:
    return asdict(label)
