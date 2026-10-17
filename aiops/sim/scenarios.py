"""The fault injector: 24 realistic incidents planted into a healthy background world.

Every scenario starts from the same kind of healthy shop (Nginx in front of an API, a background worker and
Postgres), with normal traffic, harmless warnings and two or three unrelated changes as red herrings. The scenario
then plants one fault: the change or event that caused it, the log lines it produces, the metric shapes it bends
and the alert that fires. It returns a ``Label`` with the true root cause, which only the evaluator reads.

Several alerts are deliberately ambiguous. ``HighLatency`` on the API has five different causes across the set, and
``ContainerRestarting`` on the worker is a memory leak in one scenario and a lowered memory limit in another, so an
alert-name lookup cannot score well by itself.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from aiops.sim.world import NOW, SERVICES, CronRun, Label, LogLine, World

GiB = 1024**3
MiB = 1024**2

CATEGORIES: dict[str, str] = {
    "config_error": "A deploy or config change shipped a wrong setting (typo, wrong host/port, wrong value).",
    "code_regression": "New code, a dependency upgrade or a feature flag introduced a bug.",
    "log_volume": "A disk is filling up because of log output or broken log rotation.",
    "memory_leak": "A process's memory grows without bound until it is killed.",
    "connection_pool_exhausted": "The application's DB connection pool is fully checked out; requests wait for a connection.",
    "db_connection_limit": "Postgres has hit max_connections; new clients are refused.",
    "certificate_expired": "A TLS certificate has expired and handshakes fail.",
    "certificate_expiring": "A TLS certificate will expire soon and is not being renewed.",
    "dns_resolution": "Name resolution is broken on a host (resolver config or DNS server).",
    "noisy_neighbour": "Another workload (batch job, backup, cron) is starving a shared resource.",
    "cron_failure": "A scheduled job failed.",
    "slow_query": "Database queries became slow (missing index, bad plan).",
    "upstream_outage": "A third-party dependency outside our control is slow or failing.",
    "credentials_mismatch": "A password, key or token was rotated or changed and a client still uses the old one.",
    "resource_limits": "CPU or memory limits/quotas are too low for the workload (throttling or OOM kills).",
    "clock_skew": "A host clock has drifted, breaking time-sensitive checks (tokens, TLS, signatures).",
    "fd_exhaustion": "A process ran out of file descriptors (Too many open files).",
    "wal_retention": "Postgres keeps WAL it cannot recycle (an inactive replication slot, archiving failure).",
}

AUTHORS = ["sarah.mills", "james.carter", "emma.wilson", "tom.baker", "claire.evans"]


# --------------------------------------------------------------------------- healthy background
def base_world(sid: str) -> World:
    rng = random.Random(f"aiops:{sid}")
    w = World(NOW, rng)
    # metrics, per service
    w.series("http_requests_per_second", "nginx", 120)
    w.series("http_error_ratio", "nginx", 0.002, 0.4)
    w.series("http_request_duration_p95_seconds", "nginx", 0.19, 0.12)
    w.series("process_open_fds", "nginx", 610, 0.08)
    w.series("node_filesystem_used_ratio", "nginx", 0.41, 0.003)
    w.series("ssl_certificate_expiry_days", "nginx", 61.0, 0.0)
    w.shape("ssl_certificate_expiry_days", "nginx", 360, lambda t, _v: 61.25 - t / 1440)

    w.series("http_requests_per_second", "api", 112)
    w.series("http_error_ratio", "api", 0.003, 0.4)
    w.series("http_request_duration_p95_seconds", "api", 0.22, 0.12)
    w.series("process_resident_memory_bytes", "api", 420 * MiB, 0.02)
    w.series("container_memory_limit_bytes", "api", 1 * GiB, 0.0)
    w.series("container_cpu_usage_cores", "api", 0.62, 0.1)
    w.series("container_cpu_throttled_ratio", "api", 0.01, 0.5)
    w.series("db_pool_connections_in_use", "api", 6, 0.3)
    w.series("db_pool_connections_max", "api", 20, 0.0)
    w.series("process_open_fds", "api", 180, 0.05)
    w.series("node_filesystem_used_ratio", "api", 0.38, 0.003)
    w.series("node_timex_offset_seconds", "api", 0.002, 0.5)
    w.series("upstream_payments_p95_seconds", "api", 0.41, 0.15)
    w.series("kube_pod_container_restarts_total", "api", 0, 0.0)

    w.series("queue_depth", "worker", 15, 0.4)
    w.series("jobs_failed_ratio", "worker", 0.004, 0.5)
    w.series("process_resident_memory_bytes", "worker", 610 * MiB, 0.02)
    w.series("container_memory_limit_bytes", "worker", 2 * GiB, 0.0)
    w.series("container_cpu_usage_cores", "worker", 1.1, 0.1)
    w.series("node_filesystem_used_ratio", "worker", 0.52, 0.003)
    w.series("log_bytes_written_per_second", "worker", 2100, 0.2)
    w.series("kube_pod_container_restarts_total", "worker", 0, 0.0)

    w.series("pg_stat_activity_count", "postgres", 38, 0.1)
    w.series("pg_max_connections", "postgres", 100, 0.0)
    w.series("node_cpu_iowait_ratio", "postgres", 0.03, 0.3)
    w.series("pg_wal_size_bytes", "postgres", 1.2 * GiB, 0.03)
    w.series("node_filesystem_used_ratio", "postgres", 0.47, 0.003)
    w.series("pg_slow_queries_per_minute", "postgres", 0.2, 1.0)

    _background_logs(w)
    _background_changes(w)
    _background_cron(w)
    for svc in SERVICES:
        w.set_health(svc, "ok", "all checks passing")
    return w


def _background_logs(w: World) -> None:
    r = w.rng
    paths = ["/api/orders", "/api/products", "/api/cart", "/api/checkout", "/api/account"]
    templates: dict[str, list[Callable[[], tuple[str, str]]]] = {
        "nginx": [
            lambda: (
                "INFO",
                f'10.0.{r.randint(1, 9)}.{r.randint(2, 250)} "GET {r.choice(paths)} HTTP/1.1" 200 {r.randint(300, 9000)} rt=0.{r.randint(40, 260):03d}',
            ),
            lambda: (
                "INFO",
                f'10.0.{r.randint(1, 9)}.{r.randint(2, 250)} "POST /api/checkout HTTP/1.1" 201 {r.randint(300, 900)} rt=0.{r.randint(90, 300):03d}',
            ),
        ],
        "api": [
            lambda: ("INFO", f"GET {r.choice(paths)} 200 {r.randint(18, 240)}ms request_id={r.randrange(16**8):08x}"),
            lambda: ("INFO", f"POST /api/checkout 201 {r.randint(90, 320)}ms request_id={r.randrange(16**8):08x}"),
            lambda: ("INFO", f"cache hit ratio 0.{r.randint(88, 97)} over last 60s"),
        ],
        "worker": [
            lambda: ("INFO", f"job send_email:{r.randint(40000, 99999)} done in 0.{r.randint(2, 9)}s"),
            lambda: ("INFO", f"job sync_inventory:{r.randint(40000, 99999)} done in {r.randint(1, 4)}.{r.randint(0, 9)}s"),
            lambda: ("INFO", f"job export_orders:{r.randint(40000, 99999)} done in {r.randint(1, 3)}.{r.randint(0, 9)}s"),
        ],
        "postgres": [
            lambda: (
                "INFO",
                f"LOG:  checkpoint complete: wrote {r.randint(900, 2600)} buffers ({r.randint(1, 3)}.{r.randint(0, 9)}%); 0 WAL file(s) added, 0 removed, {r.randint(2, 9)} recycled",
            ),
            lambda: ("INFO", 'LOG:  automatic vacuum of table "shop.public.orders": index scans: 1'),
            lambda: ("INFO", f"LOG:  connection authorized: user=api database=shop application_name=api-{r.randint(0, 3)}"),
        ],
    }
    benign_warns: dict[str, list[Callable[[], tuple[str, str]]]] = {
        "api": [
            lambda: ("WARN", f"slow request GET /api/reports/monthly took 1.{r.randint(2, 9)}s request_id={r.randrange(16**8):08x}"),
            lambda: ("WARN", "deprecated header X-Client-Version used by client ios-4.2"),
        ],
        "worker": [lambda: ("WARN", f"job send_email:{r.randint(40000, 99999)} retry 2/5: SMTP 421 try again later")],
        "nginx": [lambda: ("WARN", f"upstream response is buffered to a temporary file /var/cache/nginx/proxy_temp/{r.randint(1, 9)}/0{r.randint(10, 99)}")],
        "postgres": [lambda: ("WARN", "WARNING:  there is already a transaction in progress")],
    }
    for svc in SERVICES:
        t = w.at(360)
        while t < w.now:
            if r.random() < 0.06:
                level, msg = r.choice(benign_warns[svc])()
            else:
                level, msg = r.choice(templates[svc])()
            w.logs.append(LogLine(t, svc, level, msg, False))
            t += timedelta(seconds=r.uniform(25, 75))


_BENIGN_CHANGES = [
    (
        "api",
        "deploy",
        "api v2.13.4: update order confirmation email footer",
        "--- a/templates/email/footer.html\n+++ b/templates/email/footer.html\n-<p>Questions? Reply to this email.</p>\n+<p>Questions? Visit help.acme.test or reply to this email.</p>",
    ),
    (
        "nginx",
        "config",
        "nginx: enable gzip for application/json",
        "--- a/nginx/conf.d/gzip.conf\n+++ b/nginx/conf.d/gzip.conf\n gzip on;\n-gzip_types text/plain text/css;\n+gzip_types text/plain text/css application/json;",
    ),
    ("worker", "deploy", "worker v3.7.2: bump boto3 1.34.10 -> 1.34.11", "--- a/requirements.txt\n+++ b/requirements.txt\n-boto3==1.34.10\n+boto3==1.34.11"),
    (
        "postgres",
        "config",
        "postgres: log autovacuum runs longer than 250ms",
        "--- a/postgresql.conf\n+++ b/postgresql.conf\n-log_autovacuum_min_duration = -1\n+log_autovacuum_min_duration = 250ms",
    ),
    (
        "api",
        "deploy",
        "api v2.13.5: add alt text to product images",
        '--- a/api/serializers/product.py\n+++ b/api/serializers/product.py\n     image_url = fields.Str()\n+    image_alt = fields.Str(dump_default="")',
    ),
    (
        "worker",
        "config",
        "worker: send weekly digest at 08:00 instead of 07:00",
        '--- a/worker/schedule.yaml\n+++ b/worker/schedule.yaml\n weekly_digest:\n-  cron: "0 7 * * MON"\n+  cron: "0 8 * * MON"',
    ),
]


def _background_changes(w: World) -> None:
    picks = w.rng.sample(_BENIGN_CHANGES, 3)
    for svc, kind, summary, diff in picks:
        w.add_change(w.rng.uniform(30, 1300), svc, kind, w.rng.choice(AUTHORS), summary, diff, signal=False)


def _background_cron(w: World) -> None:
    jobs = [
        ("logrotate", "nginx", 0, 0, 3.1, "rotated 4 log files"),
        ("nightly-invoices", "worker", 1, 0, 412.0, "generated 1,284 invoices, 0 errors"),
        ("pg-backup", "postgres", 2, 0, 1830.0, "pg_dump completed: 18.4 GB written to s3://acme-backups/shop/"),
        ("certbot-renew", "nginx", 3, 0, 6.2, "Processing /etc/letsencrypt/renewal/shop.acme.test.conf\nCert not yet due for renewal"),
        ("certbot-renew", "nginx", 15, 0, 5.8, "Processing /etc/letsencrypt/renewal/shop.acme.test.conf\nCert not yet due for renewal"),
    ]
    day0 = NOW.replace(hour=0, minute=0, second=0)
    for job, host, hour, code, dur, out in jobs:
        for day in (-1, 0):
            ts = day0 + timedelta(days=day, hours=hour)
            if ts < NOW and ts > NOW - timedelta(hours=24):
                w.cron.append(CronRun(job, host, ts, code, dur, out, False))


def _replace_cron(w: World, job: str, exit_code: int, output: str, duration: float = 4.0) -> None:
    for run in w.cron:
        if run.job == job:
            run.exit_code, run.output, run.duration_s, run.signal = exit_code, output, duration, True


def ramp(a: float, b: float, minutes: float) -> Callable[[float, float], float]:
    return lambda t, _v: a + (b - a) * min(1.0, t / minutes)


def scale(factor: float) -> Callable[[float, float], float]:
    return lambda _t, v: v * factor


def to(value: float, rng: random.Random, noise: float = 0.05) -> Callable[[float, float], float]:
    return lambda _t, _v: value * (1 + rng.uniform(-noise, noise))


# --------------------------------------------------------------------------- the 24 incidents
def s01_api_db_host_typo(w: World) -> Label:
    cid = w.add_change(
        42,
        "api",
        "deploy",
        "james.carter",
        "api v2.14.0: move DB settings to env file",
        """
--- a/deploy/api.env
+++ b/deploy/api.env
-DATABASE_URL=postgresql://api@postgres:5432/shop
+DATABASE_URL=postgresql://api@postgress:5432/shop
 DB_POOL_SIZE=20
 LOG_LEVEL=INFO""",
    )
    w.log("api", 41.5, "INFO", "starting api v2.14.0 (git 9f1c2ab) with 4 workers")
    w.emit("api", 41, 0, 9, "ERROR", 'sqlalchemy.exc.OperationalError: could not translate host name "postgress" to address: Name or service not known')
    w.emit("api", 41, 0, 5, "ERROR", "GET /api/orders 500 {ms}ms request_id={rid}")
    w.shape("http_error_ratio", "api", 41, to(0.38, w.rng))
    w.shape("http_error_ratio", "nginx", 41, to(0.31, w.rng))
    w.shape("db_pool_connections_in_use", "api", 41, lambda _t, _v: 0)
    w.set_health("api", "degraded", "readiness: database check failed")
    w.set_alert("HighErrorRate", "api", "critical", 38, "High error rate on api", "5xx ratio on api is 37.9% over the last 5 minutes.")
    return Label(
        "",
        "",
        "config_error",
        [cid],
        "api",
        ["http_error_ratio@api", "http_error_ratio@nginx", "db_pool_connections_in_use@api"],
        [cid],
        [],
        ["api"],
        "Revert DATABASE_URL host to postgres (roll back to v2.13.x).",
    )


def s02_nginx_upstream_port(w: World) -> Label:
    cid = w.add_change(
        25,
        "nginx",
        "config",
        "emma.wilson",
        "nginx: point /api at the new api listener",
        """
--- a/nginx/conf.d/upstreams.conf
+++ b/nginx/conf.d/upstreams.conf
 upstream api_backend {
-    server api:8080 max_fails=3 fail_timeout=10s;
+    server api:8008 max_fails=3 fail_timeout=10s;
     keepalive 32;
 }""",
    )
    w.emit(
        "nginx",
        24.5,
        0,
        4,
        "ERROR",
        'connect() failed (111: Connection refused) while connecting to upstream, client: 10.0.4.21, server: shop.acme.test, request: "GET /api/orders HTTP/1.1", upstream: "http://10.0.2.14:8008/api/orders"',
    )
    w.emit("nginx", 24.5, 0, 6, "INFO", '10.0.4.21 "GET /api/orders HTTP/1.1" 502 157 rt=0.001')
    w.drop_logs("api", 24.5)
    w.emit("api", 24.5, 0, 30, "INFO", "GET /healthz 200 2ms (kubelet probe)", signal=False)
    w.shape("http_error_ratio", "nginx", 24, to(0.97, w.rng, 0.02))
    w.shape("http_requests_per_second", "api", 24, to(0.5, w.rng))
    w.set_health("nginx", "degraded", "upstream api_backend: 0/1 peers available")
    w.set_alert("HighErrorRate", "nginx", "critical", 22, "High error rate on nginx", "5xx ratio on nginx is 96.8% over the last 5 minutes.")
    return Label(
        "",
        "",
        "config_error",
        [cid],
        "nginx",
        ["http_error_ratio@nginx", "http_requests_per_second@api"],
        [cid],
        [],
        ["nginx"],
        "Change the upstream back to api:8080 and reload nginx.",
    )


def s03_worker_debug_logs(w: World) -> Label:
    cid = w.add_change(
        270,
        "worker",
        "config",
        "tom.baker",
        "worker: verbose logging for invoice sync investigation",
        """
--- a/deploy/worker.env
+++ b/deploy/worker.env
-LOG_LEVEL=INFO
+LOG_LEVEL=DEBUG
 WORKER_CONCURRENCY=8""",
    )
    w.emit("worker", 269, 0, 20, "DEBUG", "sync_inventory payload sku=AC-{job} warehouse=akl-1 body=<{ms} bytes> headers={{...}}")
    w.emit("worker", 60, 0, 600, "WARN", "disk usage on /var/log is above 85%")
    w.shape("log_bytes_written_per_second", "worker", 270, to(182000, w.rng, 0.1))
    w.shape("node_filesystem_used_ratio", "worker", 270, ramp(0.52, 0.93, 270))
    w.set_alert("DiskSpaceLow", "worker", "warning", 8, "Disk space low on worker", "Filesystem /var/log on worker is 92% full and filling.")
    return Label(
        "",
        "",
        "log_volume",
        [cid],
        "worker",
        ["log_bytes_written_per_second@worker", "node_filesystem_used_ratio@worker"],
        [cid],
        [],
        [],
        "Set LOG_LEVEL back to INFO and truncate the oversized debug logs.",
    )


def s04_nginx_logrotate_broken(w: World) -> Label:
    cid = w.add_change(
        1560,
        "nginx",
        "config",
        "claire.evans",
        "logrotate: compress nginx logs",
        """
--- a/logrotate.d/nginx
+++ b/logrotate.d/nginx
 /var/log/nginx/*.log {
     daily
     rotate 14
+    compresss
     missingok
 }""",
    )
    _replace_cron(
        w, "logrotate", 1, "error: /etc/logrotate.d/nginx:4 unknown option 'compresss' -- ignoring line\nerror: found error in /var/log/nginx/*.log , skipping"
    )
    w.shape("node_filesystem_used_ratio", "nginx", 360, ramp(0.79, 0.91, 360))
    w.set_alert("DiskSpaceLow", "nginx", "warning", 6, "Disk space low on nginx", "Filesystem /var/log on nginx is 91% full and filling.")
    return Label(
        "",
        "",
        "log_volume",
        ["cron:logrotate", cid],
        "nginx",
        ["node_filesystem_used_ratio@nginx"],
        [cid],
        ["logrotate"],
        [],
        "Fix the typo (compress), run logrotate -f, confirm the next run exits 0.",
    )


def s05_worker_memory_leak(w: World) -> Label:
    cid = w.add_change(
        300,
        "worker",
        "deploy",
        "sarah.mills",
        "worker v3.9.0: cache customer records in-process for faster invoice runs",
        """
--- a/worker/invoices.py
+++ b/worker/invoices.py
+_customer_cache: dict[int, Customer] = {}
+
 def load_customer(customer_id: int) -> Customer:
-    return db.get(Customer, customer_id)
+    if customer_id not in _customer_cache:
+        _customer_cache[customer_id] = db.get(Customer, customer_id)
+    return _customer_cache[customer_id]""",
    )
    restart_at = (1950 - 610) / 5.8  # minutes after the deploy when it reaches the limit

    def rss(t: float, _v: float) -> float:
        since = t if t < restart_at else t - restart_at
        return (610 + 5.8 * since) * MiB

    w.shape("process_resident_memory_bytes", "worker", 300, rss)
    w.shape("kube_pod_container_restarts_total", "worker", 300, lambda t, _v: 0 if t < restart_at else 1)
    ago = 300 - restart_at
    w.log("worker", ago, "ERROR", "Worker exited with signal 9 (OOMKilled), restarting")
    w.log("worker", ago - 0.5, "INFO", "booting worker v3.9.0 pid 7 (concurrency=8)")
    for i, m in enumerate(range(290, 0, -30)):
        w.log("worker", m, "WARN", f"invoice customer cache holds {(i + 1) * 21873} entries")
    w.set_health("worker", "degraded", "container restarted 1 time in the last 2h (OOMKilled)")
    w.set_alert("ContainerRestarting", "worker", "warning", 60, "Container restarting: worker", "worker restarted 1 time(s) in the last 2 hours.")
    return Label(
        "",
        "",
        "memory_leak",
        [cid],
        "worker",
        ["process_resident_memory_bytes@worker", "kube_pod_container_restarts_total@worker"],
        [cid],
        [],
        ["worker"],
        "Roll back to v3.8.x or bound the cache (LRU with max size).",
    )


def s06_api_memory_leak(w: World) -> Label:
    cid = w.add_change(
        340,
        "api",
        "deploy",
        "james.carter",
        "api v2.13.0: reuse headless renderers for invoice PDFs",
        """
--- a/api/pdf.py
+++ b/api/pdf.py
+_renderers: list[Renderer] = []
+
 def render_invoice(order: Order) -> bytes:
-    with Renderer() as r:
-        return r.render(order)
+    r = Renderer()
+    _renderers.append(r)  # reuse later
+    return r.render(order)""",
    )
    w.shape("process_resident_memory_bytes", "api", 340, lambda t, _v: (420 + 1.62 * t) * MiB)
    for i, m in enumerate(range(330, 0, -40)):
        w.log("api", m, "WARN", f"pdf renderer pool holds {(i + 1) * 47} renderers (expected <= 8)")
    w.set_alert("HighMemoryUsage", "api", "warning", 12, "High memory usage on api", "api memory is at 93% of its container limit.")
    return Label(
        "", "", "memory_leak", [cid], "api", ["process_resident_memory_bytes@api"], [cid], [], [], "Roll back to v2.12.x or close renderers after use."
    )


def s07_api_pool_exhausted(w: World) -> Label:
    cid = w.add_change(
        95,
        "api",
        "deploy",
        "emma.wilson",
        "api v2.14.2: stream monthly report export",
        """
--- a/api/reports.py
+++ b/api/reports.py
 def export_monthly(month: str) -> Iterator[bytes]:
-    rows = session.execute(query(month)).all()
-    yield from to_csv(rows)
+    with session.begin():  # held open while the client downloads
+        for row in session.execute(query(month)).yield_per(500):
+            yield to_csv_row(row)""",
    )
    w.emit("api", 52, 0, 10, "ERROR", "sqlalchemy.exc.TimeoutError: QueuePool limit of size 20 overflow 0 reached, connection timed out, timeout 30.00")
    w.emit("api", 80, 0, 240, "WARN", "slow request GET /api/reports/export?month=2026-08 took {ms}.2s request_id={rid}")
    w.shape("db_pool_connections_in_use", "api", 80, ramp(6, 20, 30))
    w.shape("http_request_duration_p95_seconds", "api", 52, to(8.6, w.rng))
    w.shape("pg_stat_activity_count", "postgres", 80, ramp(38, 52, 30))
    w.set_alert("HighLatency", "api", "critical", 45, "High latency on api", "p95 latency on api is 8.6s over the last 5 minutes.")
    return Label(
        "",
        "",
        "connection_pool_exhausted",
        [cid],
        "api",
        ["db_pool_connections_in_use@api", "http_request_duration_p95_seconds@api"],
        [cid],
        [],
        [],
        "Roll back the export change; buffer the export outside the transaction.",
    )


def s08_postgres_max_connections(w: World) -> Label:
    cid = w.add_change(
        70,
        "worker",
        "config",
        "tom.baker",
        "worker: raise concurrency for the sale backlog",
        """
--- a/deploy/worker.env
+++ b/deploy/worker.env
-WORKER_CONCURRENCY=8
+WORKER_CONCURRENCY=64
 DB_POOL_SIZE=2""",
    )
    msg = 'psycopg2.OperationalError: connection to server at "postgres" (10.0.2.20), port 5432 failed: FATAL:  sorry, too many clients already'
    w.emit("api", 66, 0, 12, "ERROR", msg)
    w.emit("worker", 66, 0, 15, "ERROR", msg)
    w.emit("postgres", 66, 0, 8, "ERROR", "FATAL:  sorry, too many clients already")
    w.log("worker", 69, "INFO", "celery@worker-0 ready. concurrency: 64 (prefork)")
    w.shape("pg_stat_activity_count", "postgres", 69, ramp(38, 100, 3))
    w.shape("http_error_ratio", "api", 66, to(0.12, w.rng))
    w.set_alert("HighErrorRate", "api", "critical", 60, "High error rate on api", "5xx ratio on api is 12.1% over the last 5 minutes.")
    return Label(
        "",
        "",
        "db_connection_limit",
        [cid],
        "postgres",
        ["pg_stat_activity_count@postgres", "http_error_ratio@api"],
        [cid],
        [],
        [],
        "Set WORKER_CONCURRENCY back to 8 (or add PgBouncer) and restart the worker.",
    )


def s09_nginx_cert_expired(w: World) -> Label:
    cid = w.add_change(
        2300,
        "nginx",
        "infra",
        "claire.evans",
        "dns: move acme.test zone to new provider",
        """
--- a/dns/acme.test.zone
+++ b/dns/acme.test.zone
-_acme-challenge.shop  CNAME  shop.acme-validation.test.
 shop                  A      203.0.113.10""",
    )
    out = (
        "Processing /etc/letsencrypt/renewal/shop.acme.test.conf\nChallenge failed for domain shop.acme.test\n"
        "DNS problem: NXDOMAIN looking up TXT for _acme-challenge.shop.acme.test"
    )
    _replace_cron(w, "certbot-renew", 1, out, 9.4)
    w.shape("ssl_certificate_expiry_days", "nginx", 360, lambda t, _v: 0.233 - t / 1440)
    w.emit(
        "nginx",
        25,
        0,
        6,
        "ERROR",
        "SSL_do_handshake() failed (SSL: error:0A000415:SSL routines::sslv3 alert certificate expired:SSL alert number 45) while SSL handshaking, client: 10.0.{n}.17",
    )
    w.shape("http_requests_per_second", "nginx", 25, scale(0.28))
    w.set_alert("ProbeFailed", "nginx", "critical", 20, "Synthetic probe failing", "probe_success == 0 for https://shop.acme.test/ from 3 locations.")
    return Label(
        "",
        "",
        "certificate_expired",
        ["cron:certbot-renew", cid],
        "nginx",
        ["ssl_certificate_expiry_days@nginx", "http_requests_per_second@nginx"],
        [cid],
        ["certbot-renew"],
        [],
        "Restore the _acme-challenge CNAME, run certbot renew, reload nginx.",
    )


def s10_cert_expiring(w: World) -> Label:
    w.series("ssl_certificate_expiry_days", "nginx", 6.0, 0.0)
    w.shape("ssl_certificate_expiry_days", "nginx", 360, lambda t, _v: 6.85 - t / 1440)
    w.log("nginx", 300, "INFO", "certificate /etc/nginx/certs/api.acme.test.pem loaded (manual install, not managed by certbot)")
    w.set_alert("CertificateExpiringSoon", "nginx", "warning", 30, "TLS certificate expiring", "Certificate for api.acme.test expires in 6.6 days.")
    return Label(
        "",
        "",
        "certificate_expiring",
        ["cron:certbot-renew", "none"],
        "nginx",
        ["ssl_certificate_expiry_days@nginx"],
        [],
        ["certbot-renew"],
        [],
        "Add api.acme.test to certbot (certbot certonly ... -d api.acme.test) and reload nginx.",
    )


def s11_worker_dns_resolv(w: World) -> Label:
    cid = w.add_change(
        35,
        "worker",
        "infra",
        "ops-bot",
        "node image refresh 2026.09.2 for worker node pool",
        """
--- a/images/worker-node/etc/resolv.conf
+++ b/images/worker-node/etc/resolv.conf
-nameserver 10.0.0.2
+nameserver 10.0.0.53
 search svc.cluster.local""",
    )
    w.emit("worker", 33, 0, 12, "ERROR", "httpx.ConnectError: [Errno -3] Temporary failure in name resolution (host: api.payments-provider.test)")
    w.emit("worker", 33, 0, 20, "ERROR", "smtplib.SMTPConnectError: [Errno -3] Temporary failure in name resolution (host: smtp.mailer.test)")
    w.shape("jobs_failed_ratio", "worker", 33, to(0.61, w.rng))
    w.set_alert("JobFailureRateHigh", "worker", "critical", 28, "Job failure rate high on worker", "61% of worker jobs failed over the last 10 minutes.")
    return Label(
        "", "", "dns_resolution", [cid], "worker", ["jobs_failed_ratio@worker"], [cid], [], [], "Roll the worker node image back (nameserver 10.0.0.2)."
    )


def s12_backup_cron_noisy(w: World) -> Label:
    cid = w.add_change(
        1240,
        "postgres",
        "config",
        "claire.evans",
        "crontab: move pg-backup to 09:45 so it finishes before the EU close",
        """
--- a/cron/postgres.crontab
+++ b/cron/postgres.crontab
-0 2 * * *   /opt/backup/pg-backup.sh
+45 9 * * *  /opt/backup/pg-backup.sh""",
    )
    w.cron = [r for r in w.cron if r.job != "pg-backup"]
    w.cron.append(CronRun("pg-backup", "postgres", w.at(45), -1, 0.0, "running (pid 31877): pg_dump --jobs=4 shop", True))
    w.emit("postgres", 44, 0, 90, "INFO", "pg_dump: dumping contents of table public.order_items")
    w.emit(
        "postgres",
        42,
        0,
        20,
        "WARN",
        "LOG:  duration: {ms}.448 ms  statement: SELECT o.* FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.id = $1",
    )
    w.shape("node_cpu_iowait_ratio", "postgres", 45, to(0.62, w.rng))
    w.shape("pg_slow_queries_per_minute", "postgres", 45, to(14, w.rng, 0.2))
    w.shape("http_request_duration_p95_seconds", "api", 43, to(2.9, w.rng))
    w.set_alert("HighLatency", "api", "warning", 35, "High latency on api", "p95 latency on api is 2.9s over the last 5 minutes.")
    return Label(
        "",
        "",
        "noisy_neighbour",
        ["cron:pg-backup", cid],
        "postgres",
        ["node_cpu_iowait_ratio@postgres", "http_request_duration_p95_seconds@api"],
        [cid],
        ["pg-backup"],
        [],
        "Move pg-backup back to 02:00 (or run it against a replica) after this run finishes.",
    )


def s13_invoices_cron_failed(w: World) -> Label:
    cid = w.add_change(
        1200,
        "worker",
        "deploy",
        "sarah.mills",
        "worker v3.8.4: new tax fields on invoice model",
        """
--- a/worker/invoices.py
+++ b/worker/invoices.py
 def invoice_lines(order: Order) -> list[Line]:
-    region = order.customer.region
+    region = order.customer.extra["tax_region"]""",
    )
    out = (
        'Traceback (most recent call last):\n  File "/app/worker/invoices.py", line 61, in invoice_lines\n'
        "    region = order.customer.extra[\"tax_region\"]\nKeyError: 'tax_region'\n0 of 1,291 invoices generated"
    )
    _replace_cron(w, "nightly-invoices", 2, out, 3.2)
    w.set_alert("CronJobFailed", "worker", "warning", 5, "Cron job failed", "Job nightly-invoices exited with a non-zero status.")
    return Label(
        "",
        "",
        "cron_failure",
        ["cron:nightly-invoices", cid],
        "worker",
        [],
        [cid],
        ["nightly-invoices"],
        [],
        "Fall back to customer.region when tax_region is missing, then rerun nightly-invoices.",
    )


def s14_missing_index(w: World) -> Label:
    cid = w.add_change(
        80,
        "api",
        "deploy",
        "james.carter",
        "api v2.14.1: migration 0042 rebuild orders indexes",
        """
--- a/migrations/0042_orders_indexes.py
+++ b/migrations/0042_orders_indexes.py
 def upgrade() -> None:
-    op.create_index("ix_orders_customer_id", "orders", ["customer_id", "created_at"])
+    op.drop_index("ix_orders_customer_id", "orders")
+    op.create_index("ix_orders_created_at", "orders", ["created_at"])""",
    )
    w.emit(
        "postgres", 78, 0, 15, "WARN", "LOG:  duration: {ms}.220 ms  statement: SELECT * FROM orders WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 20"
    )
    w.shape("pg_slow_queries_per_minute", "postgres", 78, to(41, w.rng, 0.2))
    w.shape("node_cpu_iowait_ratio", "postgres", 78, to(0.11, w.rng))
    w.shape("http_request_duration_p95_seconds", "api", 78, to(4.8, w.rng))
    w.set_alert("HighLatency", "api", "critical", 70, "High latency on api", "p95 latency on api is 4.8s over the last 5 minutes.")
    return Label(
        "",
        "",
        "slow_query",
        [cid],
        "postgres",
        ["pg_slow_queries_per_minute@postgres", "http_request_duration_p95_seconds@api"],
        [cid],
        [],
        [],
        "Recreate ix_orders_customer_id CONCURRENTLY.",
    )


def s15_payments_upstream(w: World) -> Label:
    w.add_change(
        30,
        "api",
        "deploy",
        "emma.wilson",
        "api v2.14.3: copy change on the order confirmation page",
        """
--- a/api/templates/confirmation.html
+++ b/api/templates/confirmation.html
-<h1>Thanks for your order!</h1>
+<h1>Thank you, your order is confirmed</h1>""",
        signal=False,
    )
    w.emit(
        "api", 40, 0, 8, "ERROR", "payment provider timeout: POST https://api.payments-provider.test/v2/charges exceeded 10.0s (attempt 3/3) request_id={rid}"
    )
    w.emit("api", 40, 0, 15, "INFO", "POST /api/checkout 504 {ms}ms request_id={rid}")
    w.shape("upstream_payments_p95_seconds", "api", 40, to(10.0, w.rng, 0.01))
    w.shape("http_request_duration_p95_seconds", "api", 40, to(9.7, w.rng))
    w.set_alert("HighLatency", "api", "critical", 32, "High latency on api", "p95 latency on api is 9.7s over the last 5 minutes.")
    return Label(
        "",
        "",
        "upstream_outage",
        ["external:payments-provider"],
        "api",
        ["upstream_payments_p95_seconds@api", "http_request_duration_p95_seconds@api"],
        [],
        [],
        [],
        "Check the provider status page; enable the queued-checkout fallback; no rollback needed.",
    )


def s16_db_password_rotated(w: World) -> Label:
    cid = w.add_change(
        55,
        "postgres",
        "infra",
        "vault-rotator",
        "rotate postgres password for role worker (90-day policy)",
        """
--- vault: database/creds/worker
+++ vault: database/creds/worker
-password_version: 7
+password_version: 8
 rotation_period: 2160h""",
    )
    w.emit(
        "worker",
        50,
        0,
        20,
        "ERROR",
        'psycopg2.OperationalError: connection to server at "postgres" (10.0.2.20), port 5432 failed: FATAL:  password authentication failed for user "worker"',
    )
    w.emit("postgres", 50, 0, 20, "ERROR", 'FATAL:  password authentication failed for user "worker"')
    w.emit("postgres", 50, 0, 60, "INFO", 'DETAIL:  Connection matched pg_hba.conf line 98: "host shop worker 10.0.0.0/16 scram-sha-256"')
    w.shape("queue_depth", "worker", 50, ramp(15, 3900, 50))
    w.shape("jobs_failed_ratio", "worker", 50, to(0.0, w.rng))
    w.shape("container_cpu_usage_cores", "worker", 50, to(0.05, w.rng))
    w.set_alert("QueueBacklogHigh", "worker", "critical", 30, "Queue backlog high", "Worker queue depth is 3,900 and rising.")
    return Label(
        "",
        "",
        "credentials_mismatch",
        [cid],
        "worker",
        ["queue_depth@worker"],
        [cid],
        [],
        [],
        "Restart the worker so it reads the new secret (or sync the rotated password).",
    )


def s17_api_cpu_limit(w: World) -> Label:
    cid = w.add_change(
        65,
        "api",
        "infra",
        "tom.baker",
        "k8s: right-size api requests/limits after cost review",
        """
--- a/k8s/api/deployment.yaml
+++ b/k8s/api/deployment.yaml
         resources:
           limits:
-            cpu: "2"
+            cpu: "500m"
             memory: 1Gi""",
    )
    w.shape("container_cpu_throttled_ratio", "api", 63, to(0.71, w.rng))
    w.shape("container_cpu_usage_cores", "api", 63, to(0.5, w.rng, 0.01))
    w.shape("http_request_duration_p95_seconds", "api", 63, to(3.1, w.rng))
    w.emit("api", 62, 0, 45, "WARN", "event loop blocked for {ms}ms (gunicorn worker 3)")
    w.set_alert("HighLatency", "api", "warning", 55, "High latency on api", "p95 latency on api is 3.1s over the last 5 minutes.")
    return Label(
        "",
        "",
        "resource_limits",
        [cid],
        "api",
        ["container_cpu_throttled_ratio@api", "container_cpu_usage_cores@api"],
        [cid],
        [],
        [],
        "Restore the CPU limit to 2 cores.",
    )


def s18_worker_oom_limit(w: World) -> Label:
    cid = w.add_change(
        40,
        "worker",
        "infra",
        "tom.baker",
        "k8s: lower worker memory limit after cost review",
        """
--- a/k8s/worker/deployment.yaml
+++ b/k8s/worker/deployment.yaml
           limits:
             cpu: "2"
-            memory: 2Gi
+            memory: 512Mi""",
    )
    w.shape("container_memory_limit_bytes", "worker", 39, lambda _t, _v: 512 * MiB)
    w.shape("kube_pod_container_restarts_total", "worker", 39, lambda t, _v: float(int(t // 6) + 1))
    w.shape("process_resident_memory_bytes", "worker", 39, lambda t, _v: (280 + 40 * (t % 6)) * MiB)
    for m in range(39, 0, -6):
        w.log("worker", m, "ERROR", "Worker exited with signal 9 (OOMKilled), restarting")
        w.log("worker", m - 0.3, "INFO", "booting worker v3.8.5 pid 7 (concurrency=8)")
    w.set_health("worker", "degraded", "CrashLoopBackOff: OOMKilled 7 times in 40m")
    w.set_alert("ContainerRestarting", "worker", "critical", 30, "Container restarting: worker", "worker restarted 7 time(s) in the last 40 minutes.")
    return Label(
        "",
        "",
        "resource_limits",
        [cid],
        "worker",
        ["container_memory_limit_bytes@worker", "kube_pod_container_restarts_total@worker"],
        [cid],
        [],
        ["worker"],
        "Restore the 2Gi memory limit.",
    )


def s19_clock_skew(w: World) -> Label:
    cid = w.add_change(
        185,
        "api",
        "infra",
        "ops-bot",
        "node image refresh 2026.09.1: distroless base for api nodes",
        """
--- a/images/api-node/packages.txt
+++ b/images/api-node/packages.txt
 ca-certificates
-chrony
 tzdata""",
    )
    w.shape("node_timex_offset_seconds", "api", 180, lambda t, _v: -0.02 * t)
    w.emit("api", 70, 0, 14, "WARN", "jwt.exceptions.ImmatureSignatureError: The token is not yet valid (iat) request_id={rid}")
    w.emit("api", 70, 0, 20, "INFO", "GET /api/account 401 {ms}ms request_id={rid}")
    w.shape("http_error_ratio", "api", 70, ramp(0.01, 0.19, 70))
    w.set_alert("HighErrorRate", "api", "warning", 15, "High error rate on api", "4xx/5xx ratio on api is 19% over the last 5 minutes.")
    return Label(
        "",
        "",
        "clock_skew",
        [cid],
        "api",
        ["node_timex_offset_seconds@api", "http_error_ratio@api"],
        [cid],
        [],
        [],
        "Reinstall/enable chrony on the api nodes and step the clock.",
    )


def s20_nginx_fd_exhaustion(w: World) -> Label:
    cid = w.add_change(
        125,
        "nginx",
        "config",
        "emma.wilson",
        "nginx: upstream keepalive for websockets",
        """
--- a/nginx/nginx.conf
+++ b/nginx/nginx.conf
-worker_rlimit_nofile 65535;
 events {
-    worker_connections 16384;
+    worker_connections 4096;
 }
+proxy_http_version 1.1;
+proxy_set_header Connection "upgrade";""",
    )
    w.shape("process_open_fds", "nginx", 125, ramp(610, 1024, 70))
    w.emit("nginx", 55, 0, 9, "ERROR", "[alert] 2817#2817: *91{job} socket() failed (24: Too many open files) while connecting to upstream")
    w.emit("nginx", 55, 0, 25, "ERROR", "[crit] 2817#2817: accept4() failed (24: Too many open files)")
    w.shape("http_error_ratio", "nginx", 55, to(0.22, w.rng))
    w.set_alert("HighErrorRate", "nginx", "critical", 48, "High error rate on nginx", "5xx ratio on nginx is 22% over the last 5 minutes.")
    return Label(
        "",
        "",
        "fd_exhaustion",
        [cid],
        "nginx",
        ["process_open_fds@nginx", "http_error_ratio@nginx"],
        [cid],
        [],
        [],
        "Restore worker_rlimit_nofile 65535 and reload nginx.",
    )


def s21_feature_flag(w: World) -> Label:
    cid = w.add_change(
        18,
        "api",
        "config",
        "sarah.mills",
        "flags: new_checkout to 100% of users",
        """
--- a/flags/production.yaml
+++ b/flags/production.yaml
 new_checkout:
-  rollout_percent: 5
+  rollout_percent: 100""",
    )
    err = "TypeError: unsupported operand type(s) for +: 'decimal.Decimal' and 'float' (checkout/pricing.py:88, flag new_checkout)"
    w.emit("api", 350, 19, 1500, "ERROR", err)
    w.emit("api", 17, 0, 7, "ERROR", err)
    w.emit("api", 17, 0, 9, "INFO", "POST /api/checkout 500 {ms}ms request_id={rid}")
    w.shape("http_error_ratio", "api", 17, to(0.14, w.rng))
    w.set_alert("HighErrorRate", "api", "critical", 12, "High error rate on api", "5xx ratio on api is 14% over the last 5 minutes.")
    return Label("", "", "code_regression", [cid], "api", ["http_error_ratio@api"], [cid], [], [], "Set new_checkout rollout back to 5% (or 0).")


def s22_dependency_bump(w: World) -> Label:
    cid = w.add_change(
        50,
        "worker",
        "deploy",
        "james.carter",
        "worker v3.9.1: bump orjson 3.10.7 -> 4.0.0",
        """
--- a/requirements.txt
+++ b/requirements.txt
-orjson==3.10.7
+orjson==4.0.0""",
    )
    w.emit("worker", 48, 0, 11, "ERROR", "job export_orders:{job} failed: AttributeError: module 'orjson' has no attribute 'OPT_NAIVE_UTC'")
    w.shape("jobs_failed_ratio", "worker", 48, to(0.35, w.rng))
    w.set_alert("JobFailureRateHigh", "worker", "critical", 40, "Job failure rate high on worker", "35% of worker jobs failed over the last 10 minutes.")
    return Label("", "", "code_regression", [cid], "worker", ["jobs_failed_ratio@worker"], [cid], [], [], "Pin orjson back to 3.10.7 and redeploy.")


def s23_queue_concurrency_typo(w: World) -> Label:
    cid = w.add_change(
        100,
        "worker",
        "config",
        "tom.baker",
        "worker: lower prefetch to stop long jobs hogging slots",
        """
--- a/deploy/worker.env
+++ b/deploy/worker.env
-WORKER_CONCURRENCY=16
-WORKER_PREFETCH=4
+WORKER_CONCURRENCY=1
+WORKER_PREFETCH=1""",
    )
    w.log("worker", 99, "INFO", "celery@worker-0 ready. concurrency: 1 (prefork), prefetch: 1")
    w.shape("queue_depth", "worker", 99, ramp(15, 4800, 99))
    w.shape("container_cpu_usage_cores", "worker", 99, to(0.15, w.rng))
    w.set_alert("QueueBacklogHigh", "worker", "critical", 40, "Queue backlog high", "Worker queue depth is 4,800 and rising.")
    return Label(
        "",
        "",
        "config_error",
        [cid],
        "worker",
        ["queue_depth@worker", "container_cpu_usage_cores@worker"],
        [cid],
        [],
        [],
        "Set WORKER_CONCURRENCY back to 16 (keep prefetch 1) and restart the worker.",
    )


def s24_postgres_wal_slot(w: World) -> Label:
    cid = w.add_change(
        300,
        "postgres",
        "infra",
        "claire.evans",
        "create logical replication slot analytics_cdc for the reporting pipeline",
        """
--- a/db/ops/2026-09-14-analytics-cdc.sql
+++ b/db/ops/2026-09-14-analytics-cdc.sql
+SELECT pg_create_logical_replication_slot('analytics_cdc', 'pgoutput');
+-- consumer: analytics-cdc (deploy pending)""",
    )
    w.shape("pg_wal_size_bytes", "postgres", 300, lambda t, _v: (1.2 + 0.125 * t) * GiB)
    w.shape("node_filesystem_used_ratio", "postgres", 300, ramp(0.47, 0.91, 300))
    for m in range(290, 0, -30):
        retained = round(0.125 * (300 - m), 1)
        w.log("postgres", m, "INFO", f"pg_exporter: replication slot analytics_cdc active=false retained_wal={retained}GB")
    w.emit("postgres", 290, 0, 300, "INFO", "LOG:  checkpoint complete: wrote 2311 buffers (1.8%); 0 WAL file(s) added, 0 removed, 0 recycled")
    w.set_alert("DiskSpaceLow", "postgres", "critical", 10, "Disk space low on postgres", "Filesystem /var/lib/postgresql on postgres is 91% full and filling.")
    return Label(
        "",
        "",
        "wal_retention",
        [cid],
        "postgres",
        ["pg_wal_size_bytes@postgres", "node_filesystem_used_ratio@postgres"],
        [cid],
        [],
        [],
        "Drop the inactive slot (or start its consumer) so WAL can be recycled.",
    )


SCENARIOS: dict[str, Callable[[World], Label]] = {
    "s01-api-db-host-typo": s01_api_db_host_typo,
    "s02-nginx-upstream-port": s02_nginx_upstream_port,
    "s03-worker-debug-logs": s03_worker_debug_logs,
    "s04-nginx-logrotate-broken": s04_nginx_logrotate_broken,
    "s05-worker-memory-leak": s05_worker_memory_leak,
    "s06-api-memory-leak": s06_api_memory_leak,
    "s07-api-pool-exhausted": s07_api_pool_exhausted,
    "s08-postgres-max-connections": s08_postgres_max_connections,
    "s09-nginx-cert-expired": s09_nginx_cert_expired,
    "s10-cert-expiring": s10_cert_expiring,
    "s11-worker-dns-resolv": s11_worker_dns_resolv,
    "s12-backup-cron-noisy": s12_backup_cron_noisy,
    "s13-invoices-cron-failed": s13_invoices_cron_failed,
    "s14-missing-index": s14_missing_index,
    "s15-payments-upstream": s15_payments_upstream,
    "s16-db-password-rotated": s16_db_password_rotated,
    "s17-api-cpu-limit": s17_api_cpu_limit,
    "s18-worker-oom-limit": s18_worker_oom_limit,
    "s19-clock-skew": s19_clock_skew,
    "s20-nginx-fd-exhaustion": s20_nginx_fd_exhaustion,
    "s21-feature-flag": s21_feature_flag,
    "s22-dependency-bump": s22_dependency_bump,
    "s23-queue-concurrency-typo": s23_queue_concurrency_typo,
    "s24-postgres-wal-slot": s24_postgres_wal_slot,
}


def build(sid: str) -> tuple[World, Label]:
    """Build one incident world. Deterministic: the same id always gives byte-identical files."""
    w = base_world(sid)
    label = SCENARIOS[sid](w)
    label.id = sid
    label.title = (SCENARIOS[sid].__name__.split("_", 1)[1]).replace("_", " ")
    return w, label


def build_all(out: Path) -> list[Label]:
    labels = []
    for sid in SCENARIOS:
        w, label = build(sid)
        w.save(out / sid)
        labels.append(label)
    return labels
