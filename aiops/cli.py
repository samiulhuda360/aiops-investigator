"""Command line: build and run the simulated environment, inject incidents, investigate, approve.

aiops demo [scenario]
aiops env list | build | up | down
aiops inject <scenario> [--send]
aiops investigate [--scenario ID | --world DIR] [--rules] [--json] [--no-queue]
aiops approvals list | show ID | approve ID --by NAME [--note ...] | reject ID --by NAME --note ...
aiops audit [--verify]
aiops serve [--port 8080]
aiops mcp [--world DIR]
aiops trace [--file var/traces.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from aiops.approvals import ApprovalError, Store
from aiops.report import IncidentReport, render_markdown
from aiops.sim.scenarios import SCENARIOS, build
from aiops.telemetry import setup_tracing, shutdown

VAR = Path(os.environ.get("AIOPS_VAR", "var"))
ACTIVE = VAR / "active"
ENV_FILE = VAR / "env.json"
PORTS = {"nginx": 9101, "api": 9102, "worker": 9103, "postgres": 9104}


def _scenario_world(sid: str) -> Path:
    if sid not in SCENARIOS:
        raise SystemExit(f"unknown scenario {sid!r}; see `aiops env list`")
    out = VAR / "worlds" / sid
    world, _ = build(sid)
    world.save(out)
    return out


def cmd_env(a: argparse.Namespace) -> int:
    if a.action == "list":
        for sid in SCENARIOS:
            w, label = build(sid)
            print(f"{sid:30} {w.alert['labels']['alertname']:24} {w.alert['labels']['service']:9} true cause: {label.category}")
        return 0
    if a.action == "build":
        for sid in SCENARIOS:
            _scenario_world(sid)
        print(f"built {len(SCENARIOS)} environments under {VAR / 'worlds'}")
        return 0
    if a.action == "up":
        if ENV_FILE.exists():
            print("environment already up; run `aiops env down` first")
            return 1
        VAR.mkdir(parents=True, exist_ok=True)
        procs: dict[str, int] = {}
        logs = VAR / "proc-logs"
        logs.mkdir(exist_ok=True)
        for name, port in PORTS.items():
            fh = (logs / f"{name}.out").open("w")
            p = subprocess.Popen(
                [sys.executable, "-m", "aiops.sim.service", "--name", name, "--port", str(port), "--world", str(ACTIVE)], stdout=fh, stderr=subprocess.STDOUT
            )
            procs[name] = p.pid
        if a.console:
            fh = (logs / "console.out").open("w")
            p = subprocess.Popen([sys.executable, "-m", "uvicorn", "aiops.web:app", "--port", str(a.port)], stdout=fh, stderr=subprocess.STDOUT)
            procs["console"] = p.pid
        ENV_FILE.write_text(
            json.dumps(
                {
                    "pids": procs,
                    "health_urls": {n: f"http://127.0.0.1:{p}" for n, p in PORTS.items()},
                    "console": f"http://127.0.0.1:{a.port}" if a.console else "",
                },
                indent=1,
            )
        )
        for name, pid in procs.items():
            print(f"started {name:9} pid {pid}" + (f"  http://127.0.0.1:{PORTS[name]}/metrics" if name in PORTS else f"  http://127.0.0.1:{a.port}/"))
        return 0
    if a.action == "down":
        if not ENV_FILE.exists():
            print("environment is not up")
            return 0
        for name, pid in json.loads(ENV_FILE.read_text())["pids"].items():
            try:
                os.kill(int(pid), signal.SIGTERM)
                print(f"stopped {name} (pid {pid})")
            except OSError:
                print(f"{name} (pid {pid}) was not running")
        ENV_FILE.unlink()
        return 0
    return 2


def cmd_inject(a: argparse.Namespace) -> int:
    src = _scenario_world(a.scenario)
    if ACTIVE.exists():
        shutil.rmtree(ACTIVE)
    shutil.copytree(src, ACTIVE)
    meta = json.loads((ACTIVE / "meta.json").read_text())
    meta["scenario"] = a.scenario
    (ACTIVE / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    alert = json.loads((ACTIVE / "alert.json").read_text())
    print(f"injected {a.scenario}: {alert['labels']['alertname']} on {alert['labels']['service']} is now firing")
    if a.send:
        console = json.loads(ENV_FILE.read_text()).get("console") if ENV_FILE.exists() else ""
        url = (a.url or console or "http://127.0.0.1:8080").rstrip("/") + "/alertmanager"
        payload = {
            "version": "4",
            "groupKey": f'{{}}:{{alertname="{alert["labels"]["alertname"]}"}}',
            "status": "firing",
            "receiver": "aiops-investigator",
            "groupLabels": {"alertname": alert["labels"]["alertname"]},
            "commonLabels": alert["labels"],
            "commonAnnotations": alert["annotations"],
            "externalURL": "http://alertmanager:9093",
            "alerts": [alert],
        }
        r = httpx.post(url, json=payload, timeout=10)
        print(f"sent Alertmanager webhook to {url}: {r.status_code} {r.text}")
    return 0


def cmd_investigate(a: argparse.Namespace) -> int:
    from aiops.pipeline import handle_alert, rules_report
    from aiops.tools import Environment

    world = Path(a.world) if a.world else (_scenario_world(a.scenario) if a.scenario else ACTIVE)
    health = json.loads(ENV_FILE.read_text())["health_urls"] if ENV_FILE.exists() and world.resolve() == ACTIVE.resolve() else {}
    env = Environment(world, health_urls=health)
    setup_tracing()
    if a.rules:
        report: IncidentReport = rules_report(env)
        source, note = "rules", ""
        if not a.no_queue:
            Store.default().propose(env.alert(), json.loads(report.model_dump_json()), source, a.scenario or "")
    else:
        out = handle_alert(env, Store.default(), max_steps=a.max_steps, incident=a.scenario or "", queue=not a.no_queue)
        report, source, note = out.report, out.source, out.note
        if out.investigation:
            i = out.investigation
            print(
                f"[{source}] {i.model_calls} model calls, {len(i.tool_calls)} read-only tool calls, "
                f"{i.prompt_tokens + i.completion_tokens:,} tokens, {i.time_to_report_s:.1f}s to report, write attempts: {i.write_attempts}"
            )
            for t in i.tool_calls:
                print(f"  step {t.step}: {t.name}({', '.join(f'{k}={v!r}' for k, v in t.args.items())})")
            print()
    shutdown()
    if note:
        print(f"note: {note}\n")
    print(report.model_dump_json(indent=1) if a.json else render_markdown(report, env.alert()))
    return 0


def cmd_approvals(a: argparse.Namespace) -> int:
    store = Store.default()
    try:
        if a.action == "list":
            for p in store.list():
                rc = p["report"]["root_cause"]
                print(f"{p['id']}  {p['status']:9} {p['alert']['labels'].get('alertname', ''):22} {rc['category']:26} {rc['culprit']:22} {p['source']}")
            return 0
        if not a.id:
            raise ApprovalError("give a proposal id")
        if a.action == "show":
            p = store.get(a.id)
            print(render_markdown(IncidentReport.model_validate(p["report"]), p["alert"]))
            print(f"\nstatus: {p['status']}" + (f" by {p['decided_by']} at {p['decided_at']}" if p["decided_by"] else ""))
            return 0
        p = store.decide(a.id, a.action == "approve", a.by or "", a.note or "")
        print(f"{p['id']} {p['status']} by {p['decided_by']}. Recorded in {store.audit_path}.")
        if p["status"] == "approved":
            print("Commands for the operator to run (aiops-investigator does not run them):")
            for c in p["report"]["proposed_fix"].get("commands", []):
                print(f"  $ {c}")
        return 0
    except ApprovalError as exc:
        print(f"error: {exc}")
        return 1


def cmd_audit(a: argparse.Namespace) -> int:
    store = Store.default()
    for e in store.audit():
        print(f"{e['seq']:>3} {e['ts']} {e['event']:9} {e['proposal']} by {e['actor']}  {e.get('note') or e.get('category', '')}  {e['hash'][:12]}")
    ok, msg = store.verify()
    print(("chain OK: " if ok else "CHAIN BROKEN: ") + msg)
    return 0 if ok else 1


def cmd_trace(a: argparse.Namespace) -> int:
    spans = [json.loads(ln) for ln in Path(a.file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    roots = [s for s in spans if s["name"] == "investigate"]
    if not roots:
        print("no investigations traced yet")
        return 1
    root = roots[-1] if not a.incident else next(s for s in reversed(roots) if s["attributes"].get("incident.id") == a.incident)
    tree = [s for s in spans if s["trace_id"] == root["trace_id"]]
    kids: dict[str | None, list[dict[str, Any]]] = {}
    for s in tree:
        kids.setdefault(s["parent_id"], []).append(s)
    t0 = root["start_ns"]

    def show(s: dict[str, Any], depth: int) -> None:
        dur = (s["end_ns"] - s["start_ns"]) / 1e6
        at = s["attributes"]
        extra = ""
        if s["name"] == "llm.chat":
            extra = f"in={at.get('gen_ai.usage.input_tokens')} out={at.get('gen_ai.usage.output_tokens')} cache_hit={at.get('llm.cache_hit')} recorded={at.get('llm.recorded_latency_s')}s"
        elif s["name"].startswith("tool "):
            extra = f"{at.get('tool.args', '')} -> {at.get('tool.result_chars')} chars read_only={at.get('tool.read_only')}"
        elif s["name"] == "investigate":
            extra = f"{at.get('alert.name')} on {at.get('alert.service')} -> {at.get('report.category')} / {at.get('report.culprit')}"
        print(f"{'  ' * depth}{s['name']:<{34 - 2 * depth}} +{(s['start_ns'] - t0) / 1e6:8.1f}ms {dur:8.1f}ms  {extra}")
        for c in sorted(kids.get(s["span_id"], []), key=lambda x: x["start_ns"]):
            show(c, depth + 1)

    print(f"trace {root['trace_id']}")
    show(root, 0)
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    """Inject one incident, investigate it (replayed from the cache when no key is set) and queue the fix."""
    print(f"== 1. inject {a.scenario}")
    cmd_inject(argparse.Namespace(scenario=a.scenario, send=False, url=""))
    print("\n== 2. investigate with read-only tools")
    cmd_investigate(argparse.Namespace(world=str(ACTIVE), scenario=a.scenario, rules=False, json=False, no_queue=False, max_steps=5))
    print("\n== 3. the approval queue")
    cmd_approvals(argparse.Namespace(action="list", id="", by="", note=""))
    print("\nNext: `aiops serve` and open http://127.0.0.1:8080 to approve or reject, or `aiops approvals approve <id> --by <name>`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aiops", description="An AI on-call engineer with read-only tools.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("env", help="the simulated environment")
    e.add_argument("action", choices=["list", "build", "up", "down"])
    e.add_argument("--console", action="store_true", help="with up: also start the web console")
    e.add_argument("--port", type=int, default=8080)
    i = sub.add_parser("inject", help="plant an incident in the running environment")
    i.add_argument("scenario")
    i.add_argument("--send", action="store_true", help="also send the alert to the console's Alertmanager webhook")
    i.add_argument("--url", default="")
    v = sub.add_parser("investigate", help="investigate the active (or a given) incident")
    v.add_argument("--scenario", default="")
    v.add_argument("--world", default="")
    v.add_argument("--rules", action="store_true", help="use the rules baseline only")
    v.add_argument("--json", action="store_true")
    v.add_argument("--no-queue", action="store_true", help="do not add the fix to the approval queue")
    v.add_argument("--max-steps", type=int, default=5)
    p = sub.add_parser("approvals", help="the approval queue")
    p.add_argument("action", choices=["list", "show", "approve", "reject"])
    p.add_argument("id", nargs="?", default="")
    p.add_argument("--by", default="")
    p.add_argument("--note", default="")
    sub.add_parser("audit", help="print and verify the audit log")
    s = sub.add_parser("serve", help="run the web console (webhook + approvals)")
    s.add_argument("--port", type=int, default=8080)
    m = sub.add_parser("mcp", help="run the read-only tools as an MCP server on stdio")
    m.add_argument("--world", default=str(ACTIVE))
    t = sub.add_parser("trace", help="print the span tree of the last investigation")
    t.add_argument("--file", default=os.environ.get("AIOPS_TRACE_FILE", "var/traces.jsonl"))
    t.add_argument("--incident", default="")
    d = sub.add_parser("demo", help="inject an incident, investigate it and queue the fix (works without a key)")
    d.add_argument("scenario", nargs="?", default="s12-backup-cron-noisy")
    a = ap.parse_args(argv)
    if a.cmd == "demo":
        return cmd_demo(a)
    if a.cmd == "env":
        return cmd_env(a)
    if a.cmd == "inject":
        return cmd_inject(a)
    if a.cmd == "investigate":
        return cmd_investigate(a)
    if a.cmd == "approvals":
        return cmd_approvals(a)
    if a.cmd == "audit":
        return cmd_audit(a)
    if a.cmd == "trace":
        return cmd_trace(a)
    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("aiops.web:app", host="127.0.0.1", port=a.port)
        return 0
    if a.cmd == "mcp":
        os.environ["AIOPS_WORLD"] = a.world
        from aiops.mcp_server import main as mcp_main

        mcp_main()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
