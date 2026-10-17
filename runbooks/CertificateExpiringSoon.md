# Runbook: CertificateExpiringSoon

Fires when a served TLS certificate expires in under 14 days.

1. Find which certificate and domain. Check whether it is managed by certbot (renewal config) or installed by hand.
2. Check certbot-renew cron runs and their output.
3. Renew with certbot (or add the domain to certbot) and reload nginx. Confirm the new expiry date.
