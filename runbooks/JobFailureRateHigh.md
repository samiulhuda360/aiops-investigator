# Runbook: JobFailureRateHigh

Fires when more than 10% of worker jobs fail over 10 minutes.

1. Group worker ERROR logs: is it one job type or all of them? Is the error in our code or in a network call?
2. Check worker deploys, config and node image changes.
3. Check the dependencies the failing jobs call (database, SMTP, payment provider, DNS).
