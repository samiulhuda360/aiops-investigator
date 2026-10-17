# Runbook: ProbeFailed

Fires when the external synthetic probe of the public site fails from several locations.

1. Check nginx health and error logs: connection, upstream and TLS handshake errors.
2. Check the certificate expiry metric and certbot runs.
3. Check DNS and network changes.
4. Check the upstream services behind nginx.
