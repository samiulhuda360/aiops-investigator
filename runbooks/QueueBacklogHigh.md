# Runbook: QueueBacklogHigh

Fires when the worker queue depth stays above 1,000 and is rising.

1. Is the worker consuming at all? Check CPU usage, job completion logs and errors.
2. Check worker concurrency settings and recent config changes.
3. Check whether the worker can reach its dependencies (database credentials, network).
4. Fix the consumer first; then let the backlog drain. Do not purge the queue.
