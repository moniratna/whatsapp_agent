# open-wa transport service

This separate Node.js service runs the linked WhatsApp Web session. It is not part of the Python dependency graph. The current pins are `@open-wa/wa-automate` 5.12.1, webhook plugin 5.10.0, and the Docker base image digest in `Dockerfile`. The runtime packages are installed from `package-lock.json`; Node.js 22.21.1 or newer is required. Review and update these pins through normal code review.

The included Easy API configuration subscribes only to inbound messages, acknowledgements, and session state events. Webhook delivery uses a shared `X-Webhook-Secret`, bounded retry, backpressure, and the plugin's SQLite durability journal in the persistent `/sessions` volume. The plugin's default queue is memory-only, and exhausted durable deliveries become dead letters without a built-in inspection/replay route. The Python inbox remains the system of record and deduplicates retries.

## Start locally

From `deploy/openwa`, install the pinned packages and build/run the image:

```sh
npm ci
docker compose up --build -d
```

Supply `WA_SESSION_ID`, `WA_API_KEY`, `OPENWA_WEBHOOK_SECRET`, and `OPENWA_WEBHOOK_URL` through the shell or deployment secret manager. For a local Docker Desktop setup, the webhook URL can target `http://host.docker.internal:8000/webhooks/open-wa`; use a private network address in other environments. The sample compose file binds the Easy API to loopback. Never publish `/health`, the dashboard, API explorer, QR pairing, or session controls to the public internet.

Check `/health` from the local machine and wait for both `connected` and `session.ready` before sending. The health route can include pairing information. Pairing a real WhatsApp device is a separate operator action and remains pending until a non-production number, device owner, and business acceptance of the unofficial WhatsApp Web integration risk have been approved.

The named volume stores both the login data and webhook delivery journal. Protect it as a credential, encrypt backups, restrict file access, and verify restore and re-pair procedures before production. The compose health check only verifies that the HTTP process answers; it does not mean the WhatsApp session is connected.

## Operations and known limits

- Interactive WhatsApp options stay disabled until the exact license and device behavior are confirmed. Keep equivalent text prompts.
- The webhook plugin has finite retry attempts. Monitor the open-wa logs and Python dead-letter events, and define an operator replay/reconciliation process.
- The plugin does not sign callbacks with HMAC. The configured shared secret and private network boundary are required. Rotate the webhook secret in both services together.
- Do not use the SSE event stream as the sole ingestion path; open-wa documents that it does not replay events missed while disconnected.
- Do not use the open-wa API to initiate a new chat without confirming the license entitlement and approved consent/outbound policy.
