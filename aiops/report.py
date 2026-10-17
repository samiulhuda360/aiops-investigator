"""The structured incident report: what broke, why, the evidence, and a fix that needs a person's approval."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from aiops.sim.scenarios import CATEGORIES
from aiops.sim.world import SERVICES

EvidenceKind = Literal["log", "metric", "change", "cron", "health"]


class Evidence(BaseModel):
    kind: EvidenceKind
    service: str = ""
    ref: str = Field(description="log: the exact log line; metric: the metric name; change: the change id; cron: the job name; health: the check")
    observation: str = Field(default="", description="what the evidence shows, with values and times")


class RootCause(BaseModel):
    category: str
    culprit: str = Field(description='change id (chg-1234), "cron:<job>", "external:<name>" or "none"')
    explanation: str

    @field_validator("category")
    @classmethod
    def known_category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
        return v


class ProposedFix(BaseModel):
    summary: str
    commands: list[str] = Field(default_factory=list)
    config_diff: str = ""
    rollback: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "medium"


class IncidentReport(BaseModel):
    what_broke: str
    affected_service: str
    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    proposed_fix: ProposedFix
    requires_approval: Literal[True] = True

    @field_validator("affected_service")
    @classmethod
    def known_service(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in SERVICES:
            raise ValueError(f"affected_service must be one of {SERVICES}")
        return v


_STR = {"type": "string"}

# Hand-written (no $ref) so every OpenAI-compatible endpoint accepts it as a tool schema.
REPORT_TOOL: dict[str, Any] = {
    "name": "submit_report",
    "description": "Submit the final incident report. Call this exactly once, when you know the root cause or run out of steps.",
    "parameters": {
        "type": "object",
        "properties": {
            "what_broke": {"type": "string", "description": "One sentence: the user-visible symptom."},
            "affected_service": {"type": "string", "enum": SERVICES},
            "root_cause": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": sorted(CATEGORIES)},
                    "culprit": {"type": "string", "description": 'The change id (e.g. chg-1234), "cron:<job>", "external:<name>", or "none".'},
                    "explanation": _STR,
                },
                "required": ["category", "culprit", "explanation"],
            },
            "confidence": {"type": "number", "description": "0 to 1"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["log", "metric", "change", "cron", "health"]},
                        "service": {"type": "string", "enum": SERVICES},
                        "ref": {
                            "type": "string",
                            "description": "log: one exact log line copied verbatim; metric: metric name; change: change id; cron: job name; health: service",
                        },
                        "observation": _STR,
                    },
                    "required": ["kind", "service", "ref", "observation"],
                },
            },
            "proposed_fix": {
                "type": "object",
                "properties": {
                    "summary": _STR,
                    "commands": {"type": "array", "items": _STR},
                    "config_diff": _STR,
                    "rollback": {"type": "array", "items": _STR},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["summary", "commands", "rollback", "risk"],
            },
        },
        "required": ["what_broke", "affected_service", "root_cause", "confidence", "evidence", "proposed_fix"],
    },
}


def render_markdown(report: IncidentReport, alert: dict[str, Any] | None = None) -> str:
    """A human-readable version of the report for the console, the approval page and chat."""
    rc = report.root_cause
    lines = []
    if alert:
        lb = alert.get("labels", {})
        lines.append(f"## {lb.get('alertname', 'Alert')} on {lb.get('service', '?')}  ({lb.get('severity', '')})")
    lines += [
        f"**What broke:** {report.what_broke}",
        f"**Root cause:** `{rc.category}` -- culprit `{rc.culprit}` (confidence {report.confidence:.0%})",
        f"{rc.explanation}",
        "",
        "**Evidence**",
    ]
    for ev in report.evidence:
        where = f" [{ev.service}]" if ev.service else ""
        lines.append(f"- {ev.kind}{where}: `{ev.ref}`" + (f" -- {ev.observation}" if ev.observation else ""))
    fx = report.proposed_fix
    lines += ["", f"**Proposed fix (needs approval, risk {fx.risk}):** {fx.summary}"]
    lines += [f"    $ {c}" for c in fx.commands]
    if fx.config_diff:
        lines += ["", "```diff", fx.config_diff, "```"]
    if fx.rollback:
        lines += ["**Rollback:**"] + [f"    $ {c}" for c in fx.rollback]
    return "\n".join(lines)
