# Local testing and operations

## Run the local application

1. Install dependencies and start PostgreSQL with the commands in the README.
2. Apply all Alembic migrations with `uv run alembic upgrade head`.
3. Set `OPENWA_ENABLED=true`, set a local-only `OPENWA_WEBHOOK_SECRET`, and keep `OPENWA_TRANSPORT_MODE=fake` in `.env`. This accepts signed local webhook fixtures while ensuring the outbound worker returns synthetic IDs and never contacts WhatsApp.
4. Start FastAPI and the workers in separate terminals. For a single-process local setup, set `RUN_WORKERS_IN_API=true`; FastAPI then starts all worker processes (conversation, outbound, CRM sync, exports, skill review, customer skill updates, and reviewer updates) and stops them on shutdown. Workers with disabled integrations or feature flags remain idle. This convenience is limited to development/test. The Bedrock and Zoho workers select deterministic fakes unless their integrations are explicitly enabled.
5. Use `X-Dev-User` and `X-Dev-Role` only while `APP_ENV=development` or `test`. To exercise automated dispatch end to end, change runtime controls through the supervisor API in this local fake transport environment; the persisted default remains `shadow` with the kill switch on.

Do not set `OPENWA_TRANSPORT_MODE=live` until an external open-wa instance, private networking, API key, webhook secret, test number, license, and session recovery owner have been approved. The app integration expects the open-wa send API and posts inbound events to `/webhooks/open-wa`; run that service separately. For inbound authentication, configure the same secret in `OPENWA_WEBHOOK_SECRET` and the OpenWA webhook's signing-secret field. The app verifies `X-OpenWA-Signature` as HMAC-SHA256 over the exact raw request body. The app also accepts `X-Webhook-Secret` for webhook senders configured with that custom header.

To show the first-contact property menu, set `OPENWA_INTERACTIVE_ENABLED=true` and apply an OpenWA license that includes interactive messages. Add `interactive.response` to the OpenWA webhook plugin's event list alongside `message.received`; the app validates the selected row against the original menu before processing it. Without the flag, greetings remain regular text. Without a compatible license or webhook event configuration, WhatsApp options cannot be sent or their selections received.

## Staging prerequisites

Staging and production require OIDC and the live open-wa transport mode. Before enabling any external integration, set up TLS, a least-privilege PostgreSQL role, encrypted database storage/backups, a secret manager, private networking, monitoring, and a private encrypted S3 export bucket with IAM-only access. Keep AI in `shadow` and the kill switch on until a supervisor authorizes a bounded evaluation.

Bedrock requires the AWS region, permitted model ID/access, and an API key configured as `AWS_BEARER_TOKEN_BEDROCK` (from `.env` locally or secret-manager injection outside local development). Zoho requires the organization’s data-center accounts domain, server-based OAuth credentials/scopes, and explicit module/unique-field/field API-name mappings. The app does not infer Zoho custom objects or overwrite CRM fields outside the configured map.

## Worker operations

Run each worker as a separate supervised process using its `python -m app.workers.*` entry point. Workers claim database jobs with expiring leases and bounded retries. Monitor pending/retry/dead-letter integration events, CRM `sync_status`, unresolved ambiguous WhatsApp sends, export failures, and skill-review run failures. An ambiguous WhatsApp result must be reconciled against the provider/session before an operator retries it. The customer skill update queue uses `python -m app.workers.skill_updates`; the separately gated reviewer self-update queue uses `python -m app.workers.reviewer_updates`. Their rollout, status endpoint, replay, and rollback procedures are in [skill-update operations](skill-update-operations.md).

Reply-path logs use event/message/conversation IDs and omit phone numbers, message text, credentials, and webhook signatures. Follow `openwa_message_stored` → `conversation_event_claimed` → `conversation_processing_started` → `conversation_model_completed` → `conversation_reply_enqueued` → `outbound_send_started` → `outbound_provider_accepted`. OpenWA privacy IDs without `senderPhone` are resolved through its contacts phone lookup; resolution failures return `503` for webhook retry and log `openwa_lid_resolution_failed`. A `conversation_reply_saved_as_draft` or `conversation_handover_created` means policy/rollout logic chose not to queue an automatic reply. `outbound_send_blocked` and `outbound_provider_error` include a machine-readable reason or provider error code. Worker startup logs identify whether the LLM and WhatsApp transports are fake or live.

Restarting workers is safe: committed inbound messages remain in PostgreSQL; application processing and CRM synchronization use durable integration events; outbound messages use a transactional outbox. Processing order is serialized per conversation where possible. Do not manually reset an ambiguous send to pending.

## Consent and handover

Exact inbound `STOP`, `STOP ALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, and `QUIT` commands immediately opt the contact out, open or retain a handover, and pause automation. All sends (including admin sends) are blocked while opted out because the human-response policy has not been approved. An admin may restore consent only by referencing an inbound `START` or `UNSTOP` command and recording a reason; this does not resume automation. Explicit hand-back is still required. Business approval is required before changing this conservative policy.

When a normal handover is created, the conversation worker queues one brief automated acknowledgement only when runtime controls allow automated sends (`active`, or an allowlisted `canary` contact with the kill switch off). The outbound worker validates this acknowledgement against its handover run and active handover record. No acknowledgement is sent for opted-out contacts or while automation is in `shadow`, `assisted`, or `off` mode.

## Exports and learning review

Exports default to de-identified JSONL, redact common phone/email/URL/name/address patterns, omit private admin notes, and are available only to supervisors. Redaction is pattern-based and should be treated as a best-effort safeguard, not a substitute for an approved privacy review. Local files are mode `0600` in a mode `0700` directory. Staging/production write to the configured private S3 prefix using server-side encryption; configure bucket lifecycle/IAM and backups consistently with the business retention policy. The default export TTL is 24 hours and must be approved for production.

Skill review currently aggregates de-identified operational reason codes and opens a proposal only after the configured number of distinct conversations (minimum three). It does not send raw transcripts to a reviewer model, edit Markdown skills, run generated code, or deploy changes. Proposal approval authorizes human code work only.

## Recovery and incident handling

- **Database restore:** Restore an encrypted backup to an isolated database, verify the migration version, and run `uv run alembic check`. Reconcile provider events and outbox state before reconnecting external sends.
- **open-wa session failure:** Keep the Python API available for durable intake where possible; do not expose QR/session controls publicly. Follow the external open-wa operator’s encrypted session backup and re-pair process, then verify session readiness with a test account before dispatch.
- **Bedrock outage:** Disable AI dispatch with the supervisor kill switch or set mode to `off`; inbound persistence, admin workflows, and CRM sync remain independent. Retry eligible processor events after recovery; do not log prompts or tokens for debugging.
- **Zoho outage:** Events remain in the CRM outbox with bounded retry/dead-letter states. Correct mappings/credentials and retry by contact from the admin API; verify source-ID upsert behavior in the sandbox first.
- **Credential incident:** Revoke/rotate the secret in its source manager and external provider, update the deployment injection, and review redacted access/audit events. Never paste credentials into issue text or logs.

## Checks run for the current implementation

The local implementation has been checked with Ruff, strict mypy, pytest, and a fresh Alembic upgrade/drift check using disposable SQLite. PostgreSQL container and live integration/device checks remain staging actions; the local environment did not provide Docker or provider credentials.
