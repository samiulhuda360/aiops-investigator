# Runbook: ContainerRestarting

Fires when a container restarts more than once in a window.

1. Find the exit reason in logs and health (OOMKilled, crash, failed probe).
2. For OOMKilled: compare memory usage with the memory limit over time. Did usage grow, or did the limit drop?
3. Check deploys and infrastructure changes for the service.
4. Roll back whichever side changed (the code or the limit).
