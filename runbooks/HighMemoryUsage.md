# Runbook: HighMemoryUsage

Fires when a container uses more than 90% of its memory limit for 10 minutes.

1. Compare the memory curve with the limit. Steady growth since a point in time suggests a leak; a lowered limit
   suggests a sizing change.
2. Find the change that lines up with the start of growth.
3. Look for warnings about caches, pools or buffers that keep growing.
4. Mitigate with a rollback; a restart only buys time.
