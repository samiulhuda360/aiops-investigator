"""The approval queue and its tamper-evident audit log.

A proposed fix waits here until a named person approves or rejects it. Approving records the decision and hands
the commands to the operator; nothing in this project runs them. Every event is appended to ``audit.jsonl`` with a
SHA-256 hash chained to the previous entry, so an edited or deleted line breaks ``verify()``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ApprovalError(ValueError):
    pass


@dataclass
class Store:
    root: Path

    @classmethod
    def default(cls) -> Store:
        return cls(Path(os.environ.get("AIOPS_STATE_DIR", "var/state")))

    @property
    def proposals_dir(self) -> Path:
        return self.root / "proposals"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    # ----- audit log -----
    def _audit(self, event: str, proposal_id: str, actor: str, note: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        entries = self.audit()
        prev = entries[-1]["hash"] if entries else "0" * 64
        entry: dict[str, Any] = {"seq": len(entries) + 1, "ts": _now(), "event": event, "proposal": proposal_id, "actor": actor, "note": note}
        if extra:
            entry.update(extra)
        entry["prev"] = prev
        entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(ln) for ln in self.audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def verify(self) -> tuple[bool, str]:
        prev = "0" * 64
        for e in self.audit():
            body = {k: v for k, v in e.items() if k != "hash"}
            if body.get("prev") != prev:
                return False, f"entry {e.get('seq')} does not follow the previous hash"
            if hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode()).hexdigest() != e["hash"]:
                return False, f"entry {e.get('seq')} was modified"
            prev = e["hash"]
        return True, f"{len(self.audit())} entries, chain intact"

    # ----- proposals -----
    def propose(self, alert: dict[str, Any], report: dict[str, Any], source: str, incident: str = "") -> dict[str, Any]:
        with _lock:
            self.proposals_dir.mkdir(parents=True, exist_ok=True)
            n = len(list(self.proposals_dir.glob("p-*.json"))) + 1
            pid = f"p-{n:04d}"
            prop = {
                "id": pid,
                "created": _now(),
                "status": "pending",
                "source": source,
                "incident": incident,
                "alert": alert,
                "report": report,
                "decided_by": "",
                "decided_at": "",
                "note": "",
            }
            self._write(prop)
            self._audit(
                "proposed",
                pid,
                f"aiops-investigator ({source})",
                extra={"category": report.get("root_cause", {}).get("category", ""), "culprit": report.get("root_cause", {}).get("culprit", "")},
            )
            return prop

    def _write(self, prop: dict[str, Any]) -> None:
        (self.proposals_dir / f"{prop['id']}.json").write_text(json.dumps(prop, indent=1) + "\n", encoding="utf-8")

    def get(self, pid: str) -> dict[str, Any]:
        path = self.proposals_dir / f"{pid}.json"
        if not path.exists():
            raise ApprovalError(f"no proposal {pid}")
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def list(self) -> list[dict[str, Any]]:
        if not self.proposals_dir.exists():
            return []
        props = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.proposals_dir.glob("p-*.json"))]
        return sorted(props, key=lambda p: (p["status"] != "pending", p["id"]), reverse=False)

    def decide(self, pid: str, approve: bool, actor: str, note: str = "") -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise ApprovalError("a decision needs the name of the person making it")
        if not approve and not note.strip():
            raise ApprovalError("a rejection needs a reason")
        with _lock:
            prop = self.get(pid)
            if prop["status"] != "pending":
                raise ApprovalError(f"{pid} is already {prop['status']}")
            prop.update(status="approved" if approve else "rejected", decided_by=actor, decided_at=_now(), note=note.strip())
            self._write(prop)
            self._audit(prop["status"], pid, actor, note.strip())
            return prop
