# Runbook: DiskSpaceLow

Fires when a filesystem is over 90% full, or predicted to fill within 4 hours.

1. Find what is growing: logs (log bytes written, log level), database files (WAL size, replication slots), caches.
2. Check log rotation cron runs and their exit codes.
3. Check recent config changes that increase output (log level, debug flags) or keep data (slots, retention).
4. Free space safely: rotate or compress logs, fix the source of growth. Never delete database files by hand.
