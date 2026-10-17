# Evaluation results

24 injected incidents. Agent ran on 24.

| Metric | Rules: alert name | Rules: alert + log signatures | Agent |
|---|---|---|---|
| Root-cause category accuracy | 38% | 75% | 79% |
| Culprit accuracy (change / cron job / external) | 75% | 75% | 100% |
| Category and culprit both right | 33% | 54% | 79% |
| Evidence grounded (cited item exists) | n/a | n/a | 96% of 82 |
| Evidence precision (cited item supports the cause) | n/a | n/a | 88% |
| Time to report, median / max | <1 ms | <1 ms | 8.25 s / 13.32 s |
| Tool calls per incident (mean) | 0 | 1 | 9.42 |
| Model calls (total / mean) | 0 | 0 | 88 / 3.67 |
| Tokens per incident | 0 | 0 | 12,975 |
| Cost for all incidents | $0 | $0 | $0.0364 |
| Write actions attempted | 0 | 0 | 0 |

Model: `gemini-flash-lite-latest`. Cost uses $0.1/M input and $0.4/M output tokens. Time to report is the recorded model latency plus tool time, without the free-tier rate-limit pauses.

## Per incident

| Incident | Alert | True cause | Rules (alert+logs) | Agent | Agent culprit | Evidence supporting |
|---|---|---|---|---|---|---|
| s01-api-db-host-typo | HighErrorRate (api) | config_error | dns_resolution (wrong) | config_error (ok) | chg-9069 (ok) | 3/3 |
| s02-nginx-upstream-port | HighErrorRate (nginx) | config_error | config_error (ok) | config_error (ok) | chg-8416 (ok) | 2/3 |
| s03-worker-debug-logs | DiskSpaceLow (worker) | log_volume | log_volume (ok) | config_error (wrong) | chg-3943 (ok) | 3/3 |
| s04-nginx-logrotate-broken | DiskSpaceLow (nginx) | log_volume | log_volume (ok) | config_error (wrong) | chg-5977 (ok) | 3/4 |
| s05-worker-memory-leak | ContainerRestarting (worker) | memory_leak | memory_leak (ok) | memory_leak (ok) | chg-3479 (ok) | 4/4 |
| s06-api-memory-leak | HighMemoryUsage (api) | memory_leak | memory_leak (ok) | memory_leak (ok) | chg-6781 (ok) | 3/3 |
| s07-api-pool-exhausted | HighLatency (api) | connection_pool_exhausted | connection_pool_exhausted (ok) | connection_pool_exhausted (ok) | chg-4529 (ok) | 4/4 |
| s08-postgres-max-connections | HighErrorRate (api) | db_connection_limit | db_connection_limit (ok) | db_connection_limit (ok) | chg-2645 (ok) | 4/4 |
| s09-nginx-cert-expired | ProbeFailed (nginx) | certificate_expired | certificate_expired (ok) | certificate_expired (ok) | cron:certbot-renew (ok) | 3/3 |
| s10-cert-expiring | CertificateExpiringSoon (nginx) | certificate_expiring | certificate_expiring (ok) | certificate_expiring (ok) | none (ok) | 2/3 |
| s11-worker-dns-resolv | JobFailureRateHigh (worker) | dns_resolution | dns_resolution (ok) | dns_resolution (ok) | chg-7657 (ok) | 3/3 |
| s12-backup-cron-noisy | HighLatency (api) | noisy_neighbour | slow_query (wrong) | noisy_neighbour (ok) | chg-6055 (ok) | 4/5 |
| s13-invoices-cron-failed | CronJobFailed (worker) | cron_failure | cron_failure (ok) | code_regression (wrong) | chg-7506 (ok) | 2/3 |
| s14-missing-index | HighLatency (api) | slow_query | slow_query (ok) | code_regression (wrong) | chg-2856 (ok) | 3/4 |
| s15-payments-upstream | HighLatency (api) | upstream_outage | upstream_outage (ok) | upstream_outage (ok) | external:payments-provider (ok) | 2/2 |
| s16-db-password-rotated | QueueBacklogHigh (worker) | credentials_mismatch | credentials_mismatch (ok) | credentials_mismatch (ok) | chg-9155 (ok) | 3/3 |
| s17-api-cpu-limit | HighLatency (api) | resource_limits | slow_query (wrong) | resource_limits (ok) | chg-3125 (ok) | 3/4 |
| s18-worker-oom-limit | ContainerRestarting (worker) | resource_limits | memory_leak (wrong) | resource_limits (ok) | chg-6781 (ok) | 3/4 |
| s19-clock-skew | HighErrorRate (api) | clock_skew | clock_skew (ok) | clock_skew (ok) | chg-2694 (ok) | 4/4 |
| s20-nginx-fd-exhaustion | HighErrorRate (nginx) | fd_exhaustion | fd_exhaustion (ok) | config_error (wrong) | chg-3850 (ok) | 4/4 |
| s21-feature-flag | HighErrorRate (api) | code_regression | code_regression (ok) | code_regression (ok) | chg-6568 (ok) | 2/3 |
| s22-dependency-bump | JobFailureRateHigh (worker) | code_regression | code_regression (ok) | code_regression (ok) | chg-7083 (ok) | 3/3 |
| s23-queue-concurrency-typo | QueueBacklogHigh (worker) | config_error | resource_limits (wrong) | config_error (ok) | chg-1031 (ok) | 2/3 |
| s24-postgres-wal-slot | DiskSpaceLow (postgres) | wal_retention | log_volume (wrong) | wal_retention (ok) | chg-5126 (ok) | 3/3 |
