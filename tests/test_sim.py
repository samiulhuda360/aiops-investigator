import hashlib
from pathlib import Path

from aiops.sim.scenarios import CATEGORIES, SCENARIOS, build


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_twenty_four_labelled_incidents() -> None:
    assert len(SCENARIOS) == 24
    for sid in SCENARIOS:
        world, label = build(sid)
        assert label.category in CATEGORIES
        assert world.alert["labels"]["alertname"]
        assert label.culprits
        ids = {c.id for c in world.changes}
        for culprit in label.culprits:
            if culprit.startswith("chg-"):
                assert culprit in ids, sid
            if culprit.startswith("cron:"):
                assert any(r.job == culprit[5:] for r in world.cron), sid
        for sm in label.signal_metrics:
            name, svc = sm.split("@")
            assert svc in world.metrics[name], sid


def test_generation_is_deterministic(tmp_path: Path) -> None:
    for sid in ("s01-api-db-host-typo", "s24-postgres-wal-slot"):
        a, _ = build(sid)
        b, _ = build(sid)
        a.save(tmp_path / "a" / sid)
        b.save(tmp_path / "b" / sid)
        assert _digest(tmp_path / "a" / sid) == _digest(tmp_path / "b" / sid)


def test_world_directory_holds_no_labels(worlds: Path) -> None:
    files = {p.name for p in (worlds / "s01-api-db-host-typo").rglob("*") if p.is_file()}
    assert files == {
        "meta.json",
        "metrics.json",
        "changes.json",
        "cron.json",
        "health.json",
        "alert.json",
        "nginx.log",
        "api.log",
        "worker.log",
        "postgres.log",
    }


def test_alerts_are_ambiguous() -> None:
    by_alert: dict[str, set[str]] = {}
    for sid in SCENARIOS:
        w, label = build(sid)
        by_alert.setdefault(w.alert["labels"]["alertname"], set()).add(label.category)
    assert len(by_alert["HighLatency"]) >= 4
    assert len(by_alert["HighErrorRate"]) >= 4
    assert by_alert["ContainerRestarting"] == {"memory_leak", "resource_limits"}
