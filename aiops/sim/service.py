"""One simulated service as a plain local process: ``/health`` and a Prometheus ``/metrics`` endpoint.

    python -m aiops.sim.service --name api --port 9102 --world var/active

It serves the current state of the active environment, so whatever incident the fault injector planted shows up
in the health check and in the latest metric values. A real Prometheus can scrape ``/metrics`` (see
``deploy/prometheus.yml``) and fire alerts through Alertmanager into the console's webhook.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def prometheus_text(world: Path, service: str) -> str:
    metrics: dict[str, dict[str, list[float]]] = json.loads((world / "metrics.json").read_text(encoding="utf-8"))
    out = []
    for name in sorted(metrics):
        if service in metrics[name]:
            out.append(f"# TYPE {name} gauge")
            out.append(f'{name}{{service="{service}"}} {metrics[name][service][-1]}')
    return "\n".join(out) + "\n"


def make_handler(name: str, world: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, ctype: str) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            if not (world / "meta.json").exists():
                self._send(503, json.dumps({"status": "unknown", "detail": "no active environment"}), "application/json")
                return
            if self.path.startswith("/health"):
                health: dict[str, Any] = json.loads((world / "health.json").read_text(encoding="utf-8")).get(name, {})
                code = 200 if health.get("status") == "ok" else 503
                self._send(code, json.dumps({"service": name, **health}), "application/json")
            elif self.path.startswith("/metrics"):
                self._send(200, prometheus_text(world, name), "text/plain; version=0.0.4")
            else:
                self._send(200, f"{name} (simulated)\n", "text/plain")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return None

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--world", default="var/active")
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    server = ThreadingHTTPServer((a.host, a.port), make_handler(a.name, Path(a.world)))
    print(f"{a.name} listening on http://{a.host}:{a.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
