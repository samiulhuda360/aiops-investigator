# Runbook: HighLatency

Fires when p95 request latency on a service stays above 2s for 5 minutes.

1. Is the service waiting on something? Check DB pool usage, Postgres slow queries and iowait, and upstream latency.
2. Is the service starved? Check CPU usage and throttling against its limits.
3. Is something else running? Check cron runs and batch jobs that share the database or host.
4. Line up the start of the latency rise with recent changes and scheduled jobs.
5. Latency fixes during an incident: roll back the change, move the batch job, or restore the resource limit.
