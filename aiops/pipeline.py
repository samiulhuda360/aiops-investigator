"""From an alert to a queued proposal: investigate with the agent, fall back to the rules baseline, enqueue the fix."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops.agent import Investigation, investigate
from aiops.approvals import Store
from aiops.llm import CacheMiss, CallCapReached, ChatClient, RateLimited
from aiops.report import Evidence, IncidentReport, ProposedFix, RootCause
from aiops.rules import classify
from aiops.tools import Environment


@dataclass
class Outcome:
    report: IncidentReport
    source: str  # "agent (live)", "agent (replay)", "rules"
    investigation: Investigation | None
    proposal: dict[str, Any] | None
    note: str = ""


def rules_report(env: Environment) -> IncidentReport:
    """The rules baseline's verdict, shaped as a report so it can go through the same approval flow."""
    v = classify(env)
    alert = env.alert()
    svc = alert["labels"].get("service", "api")
    runbook = alert["labels"].get("alertname", "")
    fix = f"Review and roll back {v.culprit} if it is related." if v.culprit.startswith("chg-") else f"Follow runbook {runbook}."
    cmds = [f"git revert {v.culprit} && ./ci deploy"] if v.culprit.startswith("chg-") else []
    return IncidentReport(
        what_broke=alert["annotations"].get("summary", ""),
        affected_service=svc,
        root_cause=RootCause(category=v.category, culprit=v.culprit, explanation=f"Rules baseline match on {v.matched}; no model was used."),
        confidence=0.3,
        evidence=[Evidence(kind="health", service=svc, ref=svc, observation="alerting service")],
        proposed_fix=ProposedFix(summary=fix, commands=cmds, rollback=[], risk="medium"),
    )


def handle_alert(
    env: Environment, store: Store | None = None, client: ChatClient | None = None, max_steps: int = 5, incident: str = "", queue: bool = True
) -> Outcome:
    client = client or ChatClient()
    inv: Investigation | None = None
    note = ""
    try:
        inv = investigate(env, client, max_steps=max_steps, incident_id=incident)
    except CacheMiss:
        note = "no live model configured and this alert is not in the replay cache; used the rules baseline"
    except (RateLimited, CallCapReached) as exc:
        note = f"model unavailable ({exc}); used the rules baseline"
    if inv is not None and inv.report is not None:
        report, source = inv.report, "agent (replay)" if inv.cache_hits == inv.model_calls else "agent (live)"
    else:
        if inv is not None:
            note = inv.error + "; used the rules baseline"
        report, source = rules_report(env), "rules"
    proposal = None
    if queue:
        proposal = (store or Store.default()).propose(env.alert(), json.loads(report.model_dump_json()), source, incident)
    return Outcome(report, source, inv, proposal, note)


def ensure_world(world: Path) -> Environment:
    if not (world / "meta.json").exists():
        raise FileNotFoundError(f"no environment at {world}")
    return Environment(world)
