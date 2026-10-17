"""Evaluate the agent and the rules baseline on the 24 injected incidents.

    python eval/run_eval.py            # replay: uses cached model replies only (no key, no network)
    python eval/run_eval.py --live     # record: calls the model for anything not cached (needs AI_API_KEY)
    python eval/run_eval.py --check    # replay and compare with the committed eval/results.json (used in CI)

Writes eval/results.json (per incident) and eval/RESULTS.md (the summary table).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiops.agent import investigate  # noqa: E402
from aiops.llm import CacheMiss, CallCapReached, ChatClient, RateLimited  # noqa: E402
from aiops.rules import classify  # noqa: E402
from aiops.scoring import culprit_ok, score_evidence  # noqa: E402
from aiops.sim.scenarios import SCENARIOS, build  # noqa: E402
from aiops.telemetry import setup_tracing, shutdown  # noqa: E402
from aiops.tools import Environment  # noqa: E402

PRICE_IN = float(os.environ.get("AIOPS_PRICE_IN_PER_M", "0.10"))
PRICE_OUT = float(os.environ.get("AIOPS_PRICE_OUT_PER_M", "0.40"))


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call the model on cache misses (needs AI_API_KEY)")
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--max-steps", type=int, default=5)
    ap.add_argument("--worlds", default=str(ROOT / "var" / "worlds"))
    ap.add_argument("--out", default=str(ROOT / "eval"))
    ap.add_argument("--check", action="store_true", help="replay and fail if the results differ from the committed results.json")
    args = ap.parse_args()

    setup_tracing(Path(os.environ.get("AIOPS_TRACE_FILE", str(ROOT / "var" / "eval-traces.jsonl"))))
    client = ChatClient(offline=not args.live)
    ids = [s for s in SCENARIOS if not args.only or s in args.only.split(",")]
    rows: list[dict[str, Any]] = []
    stopped = ""
    for sid in ids:
        world, label = build(sid)
        wdir = Path(args.worlds) / sid
        world.save(wdir)
        env = Environment(wdir)
        row: dict[str, Any] = {
            "id": sid,
            "alert": env.alert()["labels"]["alertname"],
            "service": env.alert()["labels"]["service"],
            "label": {"category": label.category, "culprits": label.culprits},
        }
        for tier, use_logs in (("rules_alert", False), ("rules_logs", True)):
            t0 = time.perf_counter()
            v = classify(env, use_logs=use_logs)
            row[tier] = {
                "category": v.category,
                "culprit": v.culprit,
                "category_ok": v.category == label.category,
                "culprit_ok": culprit_ok(v.culprit, label),
                "ms": round(1000 * (time.perf_counter() - t0), 2),
            }
        try:
            inv = investigate(env, client, max_steps=args.max_steps, incident_id=sid)
        except CacheMiss:
            row["agent"] = {"status": "not_cached"}
            rows.append(row)
            continue
        except (RateLimited, CallCapReached) as exc:
            stopped = f"stopped at {sid}: {exc}"
            print(stopped)
            break
        a: dict[str, Any] = {
            "status": "ok" if inv.report else "no_report",
            "model": inv.model,
            "model_calls": inv.model_calls,
            "cache_hits": inv.cache_hits,
            "tool_calls": len(inv.tool_calls),
            "tools_used": [t.name for t in inv.tool_calls],
            "write_attempts": inv.write_attempts,
            "invalid_reports": inv.invalid_reports,
            "prompt_tokens": inv.prompt_tokens,
            "completion_tokens": inv.completion_tokens,
            "time_to_report_s": round(inv.time_to_report_s, 2),
            "model_latency_s": round(inv.model_latency_s, 2),
            "tool_time_s": round(inv.tool_time_s, 3),
        }
        if inv.report:
            ev = score_evidence(world, label, inv.report)
            a.update(
                {
                    "category": inv.report.root_cause.category,
                    "culprit": inv.report.root_cause.culprit,
                    "confidence": inv.report.confidence,
                    "category_ok": inv.report.root_cause.category == label.category,
                    "culprit_ok": culprit_ok(inv.report.root_cause.culprit, label),
                    "evidence_total": ev.total,
                    "evidence_grounded": ev.grounded,
                    "evidence_supporting": ev.supporting,
                    "report": inv.report.model_dump(),
                }
            )
        row["agent"] = a
        rows.append(row)
        print(
            f"{sid:32} rules={row['rules_logs']['category']:<26} agent={a.get('category', a['status']):<26} "
            f"{'OK ' if a.get('category_ok') else 'XX '} calls={a['model_calls']} tools={a['tool_calls']}"
        )
    shutdown()
    summary = summarise(rows, stopped)
    out = Path(args.out)
    if args.check:
        committed = json.loads((out / "results.json").read_text(encoding="utf-8"))
        same = _stable(committed["incidents"]) == _stable(json.loads(json.dumps(rows)))
        print("replay matches the committed results" if same else "replay DIFFERS from the committed results")
        print(markdown(summary, rows).split("## Per incident")[0])
        return 0 if same else 1
    (out / "results.json").write_text(json.dumps({"summary": summary, "incidents": rows}, indent=1) + "\n", encoding="utf-8")
    (out / "RESULTS.md").write_text(markdown(summary, rows), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


WALL_CLOCK_KEYS = {"ms", "time_to_report_s", "tool_time_s", "cache_hits"}


def _stable(obj: Any) -> Any:
    """Drop the fields measured with this machine's clock, which differ run to run."""
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in obj.items() if k not in WALL_CLOCK_KEYS}
    if isinstance(obj, list):
        return [_stable(v) for v in obj]
    return obj


def summarise(rows: list[dict[str, Any]], stopped: str) -> dict[str, Any]:
    n = len(rows)
    ran = [r for r in rows if r["agent"].get("status") in ("ok", "no_report")]
    reported = [r for r in ran if r["agent"]["status"] == "ok"]
    s: dict[str, Any] = {"incidents": n, "agent_ran": len(ran), "agent_reported": len(reported), "stopped": stopped}
    for tier in ("rules_alert", "rules_logs"):
        s[tier] = {
            "category_accuracy": sum(r[tier]["category_ok"] for r in rows) / n if n else 0,
            "culprit_accuracy": sum(r[tier]["culprit_ok"] for r in rows) / n if n else 0,
            "both_accuracy": sum(r[tier]["category_ok"] and r[tier]["culprit_ok"] for r in rows) / n if n else 0,
        }
    if ran:
        ag = [r["agent"] for r in ran]
        ev_total = sum(a.get("evidence_total", 0) for a in ag)
        times = [a["time_to_report_s"] for a in ag]
        tin, tout = sum(a["prompt_tokens"] for a in ag), sum(a["completion_tokens"] for a in ag)
        s["agent"] = {
            "category_accuracy": sum(a.get("category_ok", False) for a in ag) / len(ran),
            "culprit_accuracy": sum(a.get("culprit_ok", False) for a in ag) / len(ran),
            "both_accuracy": sum(a.get("category_ok", False) and a.get("culprit_ok", False) for a in ag) / len(ran),
            "evidence_items": ev_total,
            "evidence_grounded": sum(a.get("evidence_grounded", 0) for a in ag) / ev_total if ev_total else 0,
            "evidence_precision": sum(a.get("evidence_supporting", 0) for a in ag) / ev_total if ev_total else 0,
            "time_to_report_median_s": round(statistics.median(times), 2),
            "time_to_report_max_s": round(max(times), 2),
            "tool_calls_mean": round(statistics.mean(a["tool_calls"] for a in ag), 2),
            "model_calls_total": sum(a["model_calls"] for a in ag),
            "model_calls_mean": round(statistics.mean(a["model_calls"] for a in ag), 2),
            "prompt_tokens": tin,
            "completion_tokens": tout,
            "tokens_per_incident": round((tin + tout) / len(ran)),
            "cost_usd_total": round(tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT, 4),
            "price_per_m_tokens": {"input": PRICE_IN, "output": PRICE_OUT},
            "write_actions_attempted": sum(a["write_attempts"] for a in ag),
            "invalid_reports": sum(a["invalid_reports"] for a in ag),
            "no_report": len(ran) - len(reported),
            "model": next((a["model"] for a in ag if a.get("model")), ""),
        }
        # the rules baseline on the same incidents the agent ran, for a like-for-like comparison
        s["rules_logs_on_agent_set"] = sum(r["rules_logs"]["category_ok"] for r in ran) / len(ran)
    return s


def mark(ok: bool) -> str:
    return "ok" if ok else "wrong"


def markdown(s: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    n = s["incidents"]
    lines = ["# Evaluation results", "", f"{n} injected incidents. Agent ran on {s['agent_ran']}." + (f" {s['stopped']}" if s["stopped"] else ""), ""]
    lines += ["| Metric | Rules: alert name | Rules: alert + log signatures | Agent |", "|---|---|---|---|"]
    a = s.get("agent", {})

    def f(x: float) -> str:
        return f"{100 * x:.0f}%"

    lines.append(
        f"| Root-cause category accuracy | {f(s['rules_alert']['category_accuracy'])} | {f(s['rules_logs']['category_accuracy'])} | {f(a.get('category_accuracy', 0))} |"
    )
    lines.append(
        f"| Culprit accuracy (change / cron job / external) | {f(s['rules_alert']['culprit_accuracy'])} | {f(s['rules_logs']['culprit_accuracy'])} | {f(a.get('culprit_accuracy', 0))} |"
    )
    lines.append(
        f"| Category and culprit both right | {f(s['rules_alert']['both_accuracy'])} | {f(s['rules_logs']['both_accuracy'])} | {f(a.get('both_accuracy', 0))} |"
    )
    if a:
        lines += [
            f"| Evidence grounded (cited item exists) | n/a | n/a | {f(a['evidence_grounded'])} of {a['evidence_items']} |",
            f"| Evidence precision (cited item supports the cause) | n/a | n/a | {f(a['evidence_precision'])} |",
            f"| Time to report, median / max | <1 ms | <1 ms | {a['time_to_report_median_s']} s / {a['time_to_report_max_s']} s |",
            f"| Tool calls per incident (mean) | 0 | 1 | {a['tool_calls_mean']} |",
            f"| Model calls (total / mean) | 0 | 0 | {a['model_calls_total']} / {a['model_calls_mean']} |",
            f"| Tokens per incident | 0 | 0 | {a['tokens_per_incident']:,} |",
            f"| Cost for all incidents | $0 | $0 | ${a['cost_usd_total']} |",
            f"| Write actions attempted | 0 | 0 | {a['write_actions_attempted']} |",
            "",
            f"Model: `{a['model']}`. Cost uses ${a['price_per_m_tokens']['input']}/M input and ${a['price_per_m_tokens']['output']}/M output tokens. "
            "Time to report is the recorded model latency plus tool time, without the free-tier rate-limit pauses.",
        ]
    lines += [
        "",
        "## Per incident",
        "",
        "| Incident | Alert | True cause | Rules (alert+logs) | Agent | Agent culprit | Evidence supporting |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ag = r["agent"]
        agent_cell = f"{ag['category']} ({mark(ag['category_ok'])})" if "category" in ag else ag.get("status", "")
        culprit_cell = f"{ag['culprit']} ({mark(ag['culprit_ok'])})" if "culprit" in ag else ""
        ev = f"{ag.get('evidence_supporting', 0)}/{ag.get('evidence_total', 0)}" if "category" in ag else ""
        lines.append(
            f"| {r['id']} | {r['alert']} ({r['service']}) | {r['label']['category']} | {r['rules_logs']['category']} ({mark(r['rules_logs']['category_ok'])}) | {agent_cell} | {culprit_cell} | {ev} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
