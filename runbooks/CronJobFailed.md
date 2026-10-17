# Runbook: CronJobFailed

Fires when a scheduled job exits non-zero.

1. Read the job's last runs and output.
2. Line the first failing run up with deploys of the code the job runs.
3. Fix forward or roll back, then rerun the job by hand and confirm it exits 0.
