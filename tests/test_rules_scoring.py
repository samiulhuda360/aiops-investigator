from pathlib import Path

from aiops.report import Evidence, IncidentReport
from aiops.rules import classify
from aiops.scoring import culprit_ok, evidence_item, score_evidence
from aiops.sim.scenarios import build
from aiops.tools import Environment
from tests.conftest import GOOD_REPORT


def test_rules_tiers(worlds: Path) -> None:
    env = Environment(worlds / "s01-api-db-host-typo")
    assert classify(env, use_logs=False).category == "code_regression"
    # the DNS-looking error misleads the log-signature rule: the real cause is a config typo
    assert classify(env, use_logs=True).category == "dns_resolution"
    assert classify(Environment(worlds / "s18-worker-oom-limit")).category == "memory_leak"  # truth: lowered memory limit


def test_rules_blames_last_change(worlds: Path) -> None:
    _, label = build("s01-api-db-host-typo")
    assert classify(Environment(worlds / "s01-api-db-host-typo")).culprit == label.culprits[0]


def test_culprit_matching() -> None:
    _, label = build("s12-backup-cron-noisy")
    assert culprit_ok("cron:pg-backup", label) and culprit_ok("pg-backup", label)
    assert culprit_ok(label.culprits[1], label)
    assert not culprit_ok("none", label)
    _, ext = build("s15-payments-upstream")
    assert culprit_ok("external:payments-provider", ext) and culprit_ok("external:payment-gateway", ext)
    assert not culprit_ok("external:smtp", ext)


def test_evidence_scoring() -> None:
    world, label = build("s12-backup-cron-noisy")
    line = next(ln for ln in world.logs if ln.signal and "duration" in ln.msg)
    assert evidence_item(world, label, Evidence(kind="log", service="postgres", ref=line.render())) == (True, True)
    noise = next(ln for ln in world.logs if not ln.signal and ln.service == "api")
    assert evidence_item(world, label, Evidence(kind="log", service="api", ref=noise.render())) == (True, False)
    assert evidence_item(world, label, Evidence(kind="log", service="api", ref="ERROR a made-up line that never happened")) == (False, False)
    assert evidence_item(world, label, Evidence(kind="metric", service="postgres", ref="node_cpu_iowait_ratio")) == (True, True)
    assert evidence_item(world, label, Evidence(kind="metric", service="api", ref="queue_depth")) == (False, False)
    assert evidence_item(world, label, Evidence(kind="cron", service="postgres", ref="cron:pg-backup")) == (True, True)
    s = score_evidence(world, label, IncidentReport.model_validate(GOOD_REPORT))
    assert (s.total, s.grounded, s.supporting) == (2, 2, 2)
