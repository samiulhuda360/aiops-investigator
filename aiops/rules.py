"""The no-model baseline: classify an alert by pattern matching, the way a static alert-to-runbook table does.

Two tiers, both reported in the evaluation:
- ``alert``: a lookup on the alert name only.
- ``alert+logs``: first look for known error signatures in the alerting service's recent logs, and fall back to
  the alert-name lookup.

The culprit is the most recent change to the alerting service in the last six hours ("blame the last deploy"),
or the failing cron job for cron alerts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from aiops.sim.world import parse_ts
from aiops.tools import Environment

ALERT_TABLE = {
    "HighErrorRate": "code_regression",
    "HighLatency": "slow_query",
    "DiskSpaceLow": "log_volume",
    "HighMemoryUsage": "memory_leak",
    "ContainerRestarting": "memory_leak",
    "CertificateExpiringSoon": "certificate_expiring",
    "ProbeFailed": "config_error",
    "CronJobFailed": "cron_failure",
    "JobFailureRateHigh": "code_regression",
    "QueueBacklogHigh": "resource_limits",
}

LOG_SIGNATURES: list[tuple[str, str]] = [
    (r"too many clients", "db_connection_limit"),
    (r"QueuePool limit", "connection_pool_exhausted"),
    (r"password authentication failed", "credentials_mismatch"),
    (r"certificate expired", "certificate_expired"),
    (r"Too many open files", "fd_exhaustion"),
    (r"not yet valid|ImmatureSignature", "clock_skew"),
    (r"name resolution|could not translate host name|Name or service not known", "dns_resolution"),
    (r"OOMKilled", "memory_leak"),
    (r"Connection refused", "config_error"),
    (r"timeout|timed out", "upstream_outage"),
]


@dataclass
class RulesVerdict:
    category: str
    culprit: str
    matched: str


def classify(env: Environment, use_logs: bool = True) -> RulesVerdict:
    alert = env.alert()
    name = alert["labels"].get("alertname", "")
    service = alert["labels"].get("service", "")
    category, matched = ALERT_TABLE.get(name, "code_regression"), f"alert:{name}"
    if use_logs and service:
        since = env.now - timedelta(minutes=60)
        recent = [msg for ts, lv, msg, _ in env.log_lines(service) if ts >= since and lv in ("ERROR", "WARN")]
        for pattern, cat in LOG_SIGNATURES:
            rx = re.compile(pattern, re.IGNORECASE)
            if any(rx.search(m) for m in recent):
                category, matched = cat, f"log:{pattern}"
                break
    return RulesVerdict(category, _culprit(env, name, service), matched)


def _culprit(env: Environment, alertname: str, service: str) -> str:
    if alertname == "CronJobFailed":
        failing = [r for r in env._json("cron.json") if r["exit_code"] not in (0, -1)]
        if failing:
            return f"cron:{failing[-1]['job']}"
    cutoff = env.now - timedelta(hours=6)
    changes = [c for c in env._json("changes.json") if c["service"] == service and parse_ts(c["ts"]) >= cutoff]
    return changes[-1]["id"] if changes else "none"
