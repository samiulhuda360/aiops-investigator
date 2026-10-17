"""Scoring a report against the labelled root cause, and checking its evidence against the environment.

Evidence precision is checked mechanically, with no model as judge:
- a cited log line must match a real line in the environment (grounded) and that line must be one the fault
  injector planted (supporting);
- a cited metric must be one the fault bent, on the right service;
- a cited change id or cron job must be the labelled culprit (or part of the labelled causal chain);
- a cited health check must be a service whose health the fault changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiops.report import Evidence, IncidentReport
from aiops.sim.world import Label, World

PREFIX_RE = re.compile(r"^\s*(?:\[x\d+[^\]]*\]\s*)?(?:\d{4}-\d\d-\d\dT\S+\s+)?(?:(?:DEBUG|INFO|WARN|ERROR)\s+)?")


def culprit_ok(predicted: str, label: Label) -> bool:
    p = predicted.strip().lower()
    for c in label.culprits:
        c = c.lower()
        if p == c or (c.startswith("cron:") and p == c[5:]):
            return True
        if c.startswith("external:") and p.startswith("external:") and c.split(":", 1)[1].split("-")[0].rstrip("s") in p:
            return True
    return False


@dataclass
class EvidenceScore:
    total: int = 0
    grounded: int = 0
    supporting: int = 0


def _log_matches(world: World, ref: str) -> tuple[bool, bool]:
    core = PREFIX_RE.sub("", ref.strip()).strip()
    if len(core) < 12:
        return False, False
    grounded = supporting = False
    for ln in world.logs:
        m = ln.msg
        if core == m or core in m or (len(m) >= 20 and m in core):
            grounded = True
            if ln.signal:
                supporting = True
                break
    return grounded, supporting


def evidence_item(world: World, label: Label, ev: Evidence) -> tuple[bool, bool]:
    """Returns (grounded, supporting) for one cited item."""
    ref = ev.ref.strip()
    svc = ev.service.strip().lower()
    if ev.kind == "log":
        return _log_matches(world, ref)
    if ev.kind == "metric":
        name = ref.split("{")[0].split("@")[0].strip()
        if not svc:  # accept "name{service=x}" or "name@x"; otherwise any service the fault bent this metric on
            m = re.search(r"service=\"?(\w+)|@(\w+)", ref)
            svc = (m.group(1) or m.group(2)) if m else ""
        grounded = name in world.metrics and (not svc or svc in world.metrics[name])
        if not svc:
            return grounded, any(sm.split("@")[0] == name for sm in label.signal_metrics)
        return grounded, f"{name}@{svc}" in label.signal_metrics
    if ev.kind == "change":
        cid = ref.split()[0] if ref else ""
        return any(c.id == cid for c in world.changes), cid in label.signal_changes
    if ev.kind == "cron":
        job = ref.removeprefix("cron:").split()[0] if ref else ""
        return any(r.job == job for r in world.cron), job in label.signal_cron
    if ev.kind == "health":
        target = svc or ref.split(":")[0].strip().lower()
        return target in world.health, target in label.signal_health
    return False, False


def score_evidence(world: World, label: Label, report: IncidentReport) -> EvidenceScore:
    s = EvidenceScore()
    for ev in report.evidence:
        g, sup = evidence_item(world, label, ev)
        s.total += 1
        s.grounded += int(g)
        s.supporting += int(g and sup)
    return s
