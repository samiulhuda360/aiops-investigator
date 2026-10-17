# Runbook: HighErrorRate

Fires when the 5xx (or 4xx+5xx) ratio on a service stays above 5% for 5 minutes.

1. Check health of the alerting service and the services it depends on (nginx -> api -> postgres; worker -> postgres).
2. Group the service's ERROR logs for the alert window. Look for the first new error pattern and when it started.
3. List changes in the last few hours across all services, not only the alerting one. Line up change times with the
   first error.
4. Check dependency metrics: database connections, upstream latency, open file descriptors.
5. Prefer the smallest reversible fix (roll back the change, revert the flag) over a forward fix during an incident.
