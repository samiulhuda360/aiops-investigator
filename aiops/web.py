"""The console: an Alertmanager-compatible webhook plus the approval page.

    uvicorn aiops.web:app --port 8080      (or: aiops serve)

POST /alertmanager    Alertmanager webhook (version 4). Each firing alert is investigated in the background.
GET  /                the approval queue: reports, evidence, proposed fixes, approve / reject
GET  /audit           the audit log, with its hash chain checked
GET  /api/proposals   JSON
"""

from __future__ import annotations

import html
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from aiops.approvals import ApprovalError, Store
from aiops.pipeline import handle_alert
from aiops.telemetry import setup_tracing
from aiops.tools import Environment


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_tracing()
    yield


app = FastAPI(title="aiops-investigator console", lifespan=_lifespan)
_work_lock = threading.Lock()


def _store() -> Store:
    return Store.default()


def _world() -> Path:
    return Path(os.environ.get("AIOPS_WORLD", "var/active"))


def _health_urls() -> dict[str, str]:
    p = Path(os.environ.get("AIOPS_STATE_DIR", "var/state")).parent / "env.json"
    if p.exists():
        data: dict[str, str] = json.loads(p.read_text(encoding="utf-8")).get("health_urls", {})
        return data
    return {}


def _investigate(alert: dict[str, Any]) -> None:
    with _work_lock:
        world = _world()
        env = Environment(world, health_urls=_health_urls())
        current = env.alert()
        same = all(current.get("labels", {}).get(k) == alert.get("labels", {}).get(k) for k in ("alertname", "service"))
        if not same:  # an alert from a real Prometheus: investigate it as received
            (world / "alert.json").write_text(json.dumps(alert, indent=1) + "\n", encoding="utf-8")
            env = Environment(world, health_urls=_health_urls())
        meta = json.loads((world / "meta.json").read_text(encoding="utf-8"))
        handle_alert(env, _store(), incident=meta.get("scenario", ""))


@app.post("/alertmanager")
def alertmanager(payload: dict[str, Any], background: BackgroundTasks) -> JSONResponse:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise HTTPException(400, "not an Alertmanager webhook payload (missing alerts[])")
    firing = [a for a in alerts if a.get("status", payload.get("status")) == "firing"]
    for a in firing:
        background.add_task(_investigate, a)
    return JSONResponse({"accepted": len(firing), "ignored": len(alerts) - len(firing)})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/proposals")
def api_proposals() -> list[dict[str, Any]]:
    return _store().list()


@app.get("/api/audit")
def api_audit() -> dict[str, Any]:
    ok, msg = _store().verify()
    return {"chain_ok": ok, "message": msg, "entries": _store().audit()}


@app.post("/proposals/{pid}/decide")
def decide(pid: str, action: str = Form(...), actor: str = Form(""), note: str = Form("")) -> RedirectResponse:
    try:
        _store().decide(pid, action == "approve", actor, note)
    except ApprovalError as exc:
        return RedirectResponse(f"/?p={pid}&error={html.escape(str(exc))}", status_code=303)
    return RedirectResponse(f"/?p={pid}", status_code=303)


# --------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f5f7f6;--card:#fff;--ink:#13302c;--muted:#5f7471;--line:#d9e2df;--teal:#2f6f68;--teal-soft:#e6f2ef;
--amber:#9a6a12;--amber-soft:#fbf1dc;--red:#9b2c2c;--red-soft:#fbe7e7;--green:#2b6a3f;--green-soft:#e3f2e7;--code:#0f1f1d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:var(--teal);color:#fff;padding:14px 24px;display:flex;align-items:center;gap:18px}
header b{font-size:17px}header a{color:#d6ece8;text-decoration:none;font-size:14px}header a.on{color:#fff;text-decoration:underline}
main{max-width:1180px;margin:0 auto;padding:20px 16px;display:grid;grid-template-columns:330px 1fr;gap:18px}
@media (max-width:860px){main{grid-template-columns:1fr}}
.list a{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px;color:inherit;text-decoration:none}
.list a.sel{border-color:var(--teal);box-shadow:0 0 0 2px var(--teal-soft)}
.pill{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;font-weight:600}
.pending{background:var(--amber-soft);color:var(--amber)}.approved{background:var(--green-soft);color:var(--green)}.rejected{background:var(--red-soft);color:var(--red)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:14px}
h2{margin:0 0 4px;font-size:20px}h3{margin:16px 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.muted{color:var(--muted);font-size:13px}.cause{font-size:16px}.tag{background:var(--teal-soft);color:var(--teal);padding:2px 8px;border-radius:6px;font-family:ui-monospace,Consolas,monospace;font-size:13px}
.bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden;width:180px;display:inline-block;vertical-align:middle}.bar i{display:block;height:100%;background:var(--teal)}
ul.ev{list-style:none;padding:0;margin:0}ul.ev li{border-left:3px solid var(--teal);padding:4px 10px;margin:6px 0;background:#fafcfb}
code,pre{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:12.5px}
pre{background:var(--code);color:#e3efec;padding:10px 12px;border-radius:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;margin:6px 0}
.kind{display:inline-block;min-width:54px;font-size:11px;font-weight:700;color:var(--teal);text-transform:uppercase}
form.decide{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
input[type=text]{border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;min-width:160px;flex:1}
button{border:0;border-radius:8px;padding:9px 16px;font:inherit;font-weight:600;cursor:pointer}
.approve{background:var(--teal);color:#fff}.reject{background:#fff;color:var(--red);border:1px solid #e4b9b9}
.err{background:var(--red-soft);color:var(--red);padding:8px 12px;border-radius:8px;margin-bottom:10px}
.safe{background:var(--teal-soft);border-radius:8px;padding:8px 12px;font-size:13px;color:var(--teal)}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.wide{grid-column:1/-1}
"""


def _e(s: Any) -> str:
    return html.escape(str(s))


def page(title: str, body: str, tab: str) -> HTMLResponse:
    nav = "".join(f'<a href="{href}" class="{"on" if tab == name else ""}">{name}</a>' for name, href in (("Approvals", "/"), ("Audit log", "/audit")))
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body><header><b>aiops-investigator</b>{nav}"
        f"<span class=muted style='margin-left:auto;color:#d6ece8'>read-only agent · fixes need a person</span></header>{body}</body></html>"
    )


def _detail(p: dict[str, Any], error: str) -> str:
    r, a = p["report"], p["alert"]
    lb = a.get("labels", {})
    rc = r["root_cause"]
    conf = float(r.get("confidence", 0))
    ev = "".join(
        f"<li><span class=kind>{_e(x['kind'])}</span> <span class=muted>{_e(x.get('service', ''))}</span><br><code>{_e(x['ref'])}</code>"
        + (f"<div class=muted>{_e(x.get('observation', ''))}</div>" if x.get("observation") else "")
        + "</li>"
        for x in r.get("evidence", [])
    )
    fx = r["proposed_fix"]
    cmds = "\n".join("$ " + c for c in fx.get("commands", []))
    rb = "\n".join("$ " + c for c in fx.get("rollback", []))
    out = [
        f"<div class=card>{f'<div class=err>{_e(error)}</div>' if error else ''}",
        f"<div class=muted>{_e(p['id'])} · {_e(p['created'])} · source: {_e(p['source'])}</div>",
        f"<h2>{_e(lb.get('alertname', 'Alert'))} on {_e(lb.get('service', '?'))} <span class='pill {_e(p['status'])}'>{_e(p['status'])}</span></h2>",
        f"<div class=muted>{_e(a.get('annotations', {}).get('description', ''))}</div>",
        "<h3>What broke</h3>",
        f"<div>{_e(r['what_broke'])}</div>",
        "<h3>Most likely cause</h3>",
        f"<div class=cause><span class=tag>{_e(rc['category'])}</span> culprit <span class=tag>{_e(rc['culprit'])}</span> "
        f"&nbsp;confidence <span class=bar><i style='width:{conf * 100:.0f}%'></i></span> {conf:.0%}</div>",
        f"<p>{_e(rc['explanation'])}</p>",
        "<h3>Evidence</h3>",
        f"<ul class=ev>{ev}</ul></div>",
        f"<div class=card><h3 style='margin-top:0'>Proposed fix · risk {_e(fx.get('risk', ''))}</h3><div>{_e(fx['summary'])}</div>",
        f"<pre>{_e(cmds)}</pre>" if cmds else "",
        f"<h3>Config diff</h3><pre>{_e(fx['config_diff'])}</pre>" if fx.get("config_diff") else "",
        f"<h3>Rollback</h3><pre>{_e(rb)}</pre>" if rb else "",
    ]
    if p["status"] == "pending":
        out.append(
            f"<form class=decide method=post action='/proposals/{_e(p['id'])}/decide'>"
            "<input type=text name=actor placeholder='Your name' required><input type=text name=note placeholder='Note (required to reject)'>"
            "<button class=approve name=action value=approve>Approve</button><button class=reject name=action value=reject>Reject</button></form>"
            "<p class=safe>Approving records your decision in the audit log and hands these commands to the operator. The agent never runs them.</p>"
        )
    else:
        out.append(
            f"<p class=safe>{_e(p['status'].capitalize())} by <b>{_e(p['decided_by'])}</b> at {_e(p['decided_at'])}"
            + (f": {_e(p['note'])}" if p.get("note") else "")
            + "</p>"
        )
    out.append("</div>")
    return "".join(out)


@app.get("/", response_class=HTMLResponse)
def index(p: str = "", error: str = "") -> HTMLResponse:
    props = _store().list()
    if not props:
        body = "<main><div class='card wide'><h2>No proposals yet</h2><p class=muted>Send an alert to POST /alertmanager, or run <code>aiops inject &lt;scenario&gt;</code>.</p></div></main>"
        return page("Approvals", body, "Approvals")
    sel = next((x for x in props if x["id"] == p), props[0])
    items = "".join(
        f"<a href='/?p={_e(x['id'])}' class='{'sel' if x['id'] == sel['id'] else ''}'><span class='pill {_e(x['status'])}'>{_e(x['status'])}</span> "
        f"<b>{_e(x['alert'].get('labels', {}).get('alertname', ''))}</b> · {_e(x['alert'].get('labels', {}).get('service', ''))}"
        f"<div class=muted>{_e(x['id'])} · {_e(x['report']['root_cause']['category'])}</div></a>"
        for x in props
    )
    return page("Approvals", f"<main><div class=list>{items}</div><div>{_detail(sel, error)}</div></main>", "Approvals")


@app.get("/audit", response_class=HTMLResponse)
def audit_page() -> HTMLResponse:
    store = _store()
    ok, msg = store.verify()
    rows = "".join(
        f"<tr><td>{e['seq']}</td><td>{_e(e['ts'])}</td><td><span class='pill {_e(e['event']) if e['event'] != 'proposed' else 'pending'}'>{_e(e['event'])}</span></td>"
        f"<td>{_e(e['proposal'])}</td><td>{_e(e['actor'])}</td><td>{_e(e.get('note') or e.get('category', ''))}</td><td><code>{_e(e['hash'][:12])}</code></td></tr>"
        for e in reversed(store.audit())
    )
    badge = f"<span class='pill {'approved' if ok else 'rejected'}'>{'chain intact' if ok else 'chain broken'}</span> <span class=muted>{_e(msg)}</span>"
    body = (
        f"<main><div class='card wide'><h2>Audit log</h2><p>{badge}</p><p class=muted>Append-only. Each entry stores the SHA-256 of the previous one, so editing or deleting a line breaks the chain.</p>"
        f"<table><tr><th>#</th><th>Time (UTC)</th><th>Event</th><th>Proposal</th><th>By</th><th>Note / cause</th><th>Hash</th></tr>{rows}</table></div></main>"
    )
    return page("Audit log", body, "Audit log")
