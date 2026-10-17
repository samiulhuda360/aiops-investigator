import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops.approvals import ApprovalError, Store
from tests.conftest import GOOD_REPORT


def test_approval_flow_and_audit_chain(tmp_path: Path) -> None:
    store = Store(tmp_path)
    p = store.propose({"labels": {"alertname": "HighLatency", "service": "api"}}, GOOD_REPORT, "agent (replay)")
    assert p["status"] == "pending"
    with pytest.raises(ApprovalError):
        store.decide(p["id"], True, "")  # a decision needs a name
    with pytest.raises(ApprovalError):
        store.decide(p["id"], False, "Sarah")  # a rejection needs a reason
    store.decide(p["id"], True, "Sarah", "after the backup finishes")
    with pytest.raises(ApprovalError):
        store.decide(p["id"], False, "James", "changed my mind")  # decisions are final
    assert [e["event"] for e in store.audit()] == ["proposed", "approved"]
    assert store.verify()[0]


def test_tampering_breaks_the_chain(tmp_path: Path) -> None:
    store = Store(tmp_path)
    p = store.propose({"labels": {}}, GOOD_REPORT, "rules")
    store.decide(p["id"], False, "Emma", "not convinced")
    lines = store.audit_path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["actor"] = "someone else"
    store.audit_path.write_text(lines[0] + "\n" + json.dumps(entry, sort_keys=True) + "\n")
    ok, msg = store.verify()
    assert not ok and "modified" in msg


def test_webhook_to_approval_page(tmp_path: Path, worlds: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = tmp_path / "active"
    shutil.copytree(worlds / "s12-backup-cron-noisy", world)
    monkeypatch.setenv("AIOPS_WORLD", str(world))
    monkeypatch.setenv("AIOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AIOPS_TRACE_FILE", str(tmp_path / "traces.jsonl"))
    monkeypatch.setenv("AIOPS_OFFLINE", "1")
    from aiops.web import app

    alert = json.loads((world / "alert.json").read_text())
    with TestClient(app) as client:
        assert client.post("/alertmanager", json={"version": "4"}).status_code == 400
        r = client.post("/alertmanager", json={"version": "4", "status": "firing", "alerts": [alert, {**alert, "status": "resolved"}]})
        assert r.json() == {"accepted": 1, "ignored": 1}
        props = client.get("/api/proposals").json()
        assert len(props) == 1 and props[0]["status"] == "pending"
        page = client.get("/").text
        assert "HighLatency" in page and "Approve" in page
        r = client.post(f"/proposals/{props[0]['id']}/decide", data={"action": "approve", "actor": "Sarah", "note": ""}, follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/api/proposals").json()[0]["status"] == "approved"
        audit = client.get("/api/audit").json()
        assert audit["chain_ok"] and [e["event"] for e in audit["entries"]] == ["proposed", "approved"]
        assert "chain intact" in client.get("/audit").text
