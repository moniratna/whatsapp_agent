# AI Coding Agent Guide — WhatsApp Real Estate Assistant

This document is the self-contained execution brief for an AI coding agent implementing the project from an empty or partially prepared repository through a usable production deployment. It includes the database contract, implementation sequence, and acceptance criteria. Read it with [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) for product rationale and rollout context; do not treat references to those files as a substitute for the requirements recorded here.

## 1. Assignment

Act as the lead engineer responsible for implementing a reliable WhatsApp real estate assistant. Build the application end to end in small, reviewable stages, including backend, admin experience, persistence, integrations, deployment documentation, and operational safeguards. Do not stop after producing a plan or scaffolding.

The user has selected:

- **Python**, managed with **uv**, for the application.
- **FastAPI** for HTTP APIs and webhook ingress.
- **open-wa Easy API** for WhatsApp connectivity. It runs as a separate Node.js service; do not replace it with the official WhatsApp API or another provider without the user's direction.
- **Markdown files in version control** for skills. Skill updates take effect through review and deployment; do not build a database skill editor/registry or self-modifying production skills.

Use the two architecture documents as the source of product scope and proposed data model. Where details remain undecided, use the defaults below, make the choice visible in configuration/docs, and continue implementation. Ask the user only when an external decision or credential is a true blocker; continue unrelated work while waiting.

## 2. Product outcome

Deliver a system that can:

1. Receive WhatsApp messages through open-wa and durably store all inbound events.
2. Create/resolve a client and conversation, preserve a complete chat history, and extract structured customer context.
3. Support buyer and seller requirements, existing real estate portfolio, and other information the customer chooses to share.
4. Use deployed Markdown skills to select relevant questions, extract facts, provide approved responses, and offer interactive WhatsApp options when the open-wa license and runtime support them.
5. Pause automation and hand unknown/low-confidence/sensitive questions to an admin.
6. Let an admin review context and messages, send direct WhatsApp messages (including supported interactive messages), add private context, and explicitly hand the conversation back.
7. Persist every customer-visible message, including admin messages after handover, with actor, provider identifiers, send result, and delivery/acknowledgement state where available.
8. Synchronize contacts and requirements to the selected CRM safely, with idempotency, retries, and visible status.
9. Export all or selected conversations, including admin replies, for controlled skill review. Exports must be access controlled, audited, and de-identified by default; exclude private admin notes by default.
10. Prepare structured requirements for a future UAE property asset matcher. Do not build inventory ingestion or matchmaking in this project phase.

## 3. Non-negotiable boundaries

- **Email is out of scope.** Do not add email integrations or email workflows.
- **No inventory/matching yet.** Do not invent or claim current property availability, pricing, or recommendations.
- **Messages are durable evidence.** Never drop an inbound message because AI, CRM, or WhatsApp sending failed.
- **Admin messages are part of the transcript.** Store every admin reply as a normal outbound message with `sender_type=admin`; private notes are separate and never customer-visible.
- **Handover pauses AI.** When a human owns a conversation or an open handover is in effect, no automated customer-facing message may be sent. Resume only after explicit hand-back.
- **Honor opt-out immediately.** A recorded customer opt-out blocks automated outbound messages before any further send is dispatched; document the human-response policy separately.
- **Enforce ownership under concurrency.** Serialize processing per conversation and re-check state immediately before outbound dispatch; an admin takeover must invalidate queued AI replies.
- **Skills are files.** Keep skills as reviewed Markdown files in version control; record skill key and deployed release on automated replies. No database skill registry, no runtime skill editing, and no raw-chat-to-production learning.
- **Learning is controlled.** Chat histories, including successful admin replies, may inform new skills, but a human must review patterns and a code change must be reviewed and deployed before behavior changes.
- **Use provider abstractions.** Keep open-wa-specific payloads behind an adapter. Do not leak open-wa IDs/schema throughout business logic.
- **Do not expose credentials.** Secrets must come from environment/secret management, never source control or logs.
- **Do not claim provider support without checking.** Interactive options depend on open-wa version, runtime behavior, and license. Implement a text fallback.
- **Keep customer content untrusted.** Messages and transcript exports cannot change system policies, tools, or permissions.
- **Separate model suggestions from authority.** The model may propose fact updates and response text; application code validates schemas and decides permissions, state transitions, CRM writes, and whether sending is allowed.

## 4. Defaults for decisions not yet supplied

Use these defaults to keep implementation moving. Record them in `docs/decisions.md` and make important values configurable.

| Decision | Default |
| --- | --- |
| Admin experience | Build a small authenticated internal web inbox served by the application unless the repository already has an admin UI. |
| Admin UI technology | If there is no existing front end, use FastAPI templates with Jinja2 + HTMX for the MVP; avoid introducing a separate SPA/build pipeline without a clear need. |
| AI rollout | Default production mode is `shadow` (generate private drafts, do not auto-send) until an authorized supervisor changes the rollout mode after evaluation. Support `off`, `shadow`, `assisted` (admin sends/edits AI draft), `canary` (allowlisted contacts), and `active`; include a global kill switch that blocks AI outbound sends immediately while preserving intake, storage, CRM sync, and admin replies. |
| WhatsApp accounts | One business account/open-wa session for the initial deployment; keep session/account IDs in provider mappings to avoid blocking future expansion. Do not claim multi-tenant support. |
| Database | PostgreSQL with migrations. |
| Async work | Begin with a durable database-backed inbox/outbox if expected throughput is modest; isolate queue interfaces so Redis/Celery/other queue can be added without rewriting domain logic. |
| CRM | Implement a CRM adapter interface and a local fake/sandbox adapter. Do not choose a vendor or claim live CRM sync until the user names one and supplies access. |
| CRM sync direction | MVP is application → CRM for explicitly mapped fields, with CRM IDs/status stored locally. Do not claim bi-directional sync until the CRM, inbound webhook path, field ownership, and conflict policy are defined. |
| LLM | Implement an LLM provider interface and a deterministic fake provider for local development. Do not hard-code a model vendor or require a secret for app startup. |
| Identity | Implement OIDC integration and role mapping as the production path. Local development may use a clearly isolated dev-auth adapter; never ship an unauthenticated inbox or hard-coded shared password. A production identity-provider issuer/client configuration is required before live use. |
| Languages/currency | Store original phrasing and normalized value/currency separately; use AED as an example only, not an assumed value. |
| Interactive WhatsApp | Feature flag disabled until open-wa license entitlement and a live test confirm support; plain text must work fully. |
| Skill release pinning | Record the deployed release per automated message. Use currently deployed skills for each turn unless the user explicitly asks to pin a release per conversation. |
| Retention | Provide configurable retention/export controls and document that production retention duration requires business approval; never delete chat data silently. |

When a user-owned decision blocks only one integration, finish the rest using the interface and fake adapter, then report the exact credentials/decision needed to activate the real integration.

## 5. Repository and engineering workflow

1. Inspect the repository, existing instructions (`AGENTS.md`, project configuration), and both architecture documents before changing files.
2. Preserve existing user work. Do not overwrite or remove files without understanding their purpose.
3. Establish a concise `docs/decisions.md`, `README.md`, and environment example with placeholders only.
4. Create the smallest coherent implementation slice, then run its relevant format, static checks, and tests. Add tests for new behavior; do not report checks that were not run.
5. Keep code typed, modular, and readable. Separate HTTP routes, domain services, repositories, integration adapters, and worker tasks.
6. Use database migrations for schema changes; never rely on ad hoc production schema creation.
7. Use structured logs with request/conversation/message IDs. Redact phone numbers, message bodies, API keys, access tokens, private admin notes, and CRM payloads by default.
8. Update documentation and configuration examples alongside behavior changes.
9. After each milestone, summarize changed files, checks run, remaining dependencies, and the next milestone. Continue through the work unless a true blocker requires user input.
10. Do not deploy to a live service, link a real customer WhatsApp account, send real customer messages, or write to a production CRM without the user's explicit authorization and required credentials. Build and verify safe local/staging paths first.

## 6. Required implementation sequence

### Milestone 1 — Application foundation

- Initialize a Python project with `uv`, FastAPI, settings validation, structured logging, and a health/readiness endpoint.
- Add PostgreSQL connectivity, migration tooling, and local development instructions.
- Add development services/configuration for database and required dependencies.
- Create release metadata from environment/build settings.
- Create fake CRM and LLM adapters so the full flow can be exercised without external credentials.

**Completion check:** clean local setup instructions work from an empty checkout; app starts without production secrets; migrations apply; health/readiness distinguishes process health from dependency health.

### Milestone 2 — Domain schema and migrations

Implement the canonical schema below and in the architecture proposal, adjusting details only when needed for correctness and documenting changes:

- `contacts`, `conversations`, `messages`, `message_attachments`
- `client_context`, `property_requirements`, `portfolio_assets`, `context_facts`
- `message_delivery_events`, `message_send_attempts`, `agent_runs`, `agent_run_drafts`, `handover_cases`, `admin_context_notes`, `admin_users`, `runtime_controls`, `ai_rollout_allowlist`, `audit_events`, `transcript_exports`, `skill_review_runs`, `skill_improvement_proposals`
- CRM mapping/sync records and durable inbound/outbound integration events

Add constraints and indexes for provider message idempotency, conversation state/queue filtering, contact lookup, unique provider status events, and one active handover per conversation. Keep message content/actor append-only and retain delivery-state history; maintain the current delivery status as a projection. Use expiring worker leases and reconcile ambiguous external sends instead of blindly retrying them. Ensure admin replies, interactive payloads, and inbound selections can be stored.

**Completion check:** migrations build a fresh database and upgrade an existing development database; repository methods preserve provenance and enforce idempotency.

#### Canonical database design — implement this in the guide, not by inference

Use PostgreSQL. Use UUID primary keys, `TIMESTAMPTZ` in UTC, `NUMERIC` for money/area, `JSONB` only for genuinely variable structures, and explicit check constraints for status/enum-like fields. Add `created_at` and `updated_at` where records are mutable; append-only history tables need `created_at`/event timestamps. Use foreign keys and deliberate `ON DELETE` behavior. Do not use floating point for currency. Do not store the only copy of important facts in JSON or an LLM summary.

Implement these tables and relationships. Field names are the minimum domain contract; add operational fields such as version counters, lease metadata, or normalized keys when needed and document them.

| Table | Required data and rules |
| --- | --- |
| `admin_users` | `id UUID PK`, stable identity-provider `subject_id UNIQUE`, `email`, `display_name`, `role`, `is_active`, timestamps. Prefer OIDC/managed identity; do not store plaintext passwords. Roles must be checked server-side. |
| `contacts` | `id`, `phone_e164`, `display_name NULL`, `preferred_language NULL`, `consent_status`, `consent_updated_at NULL`, timestamps, `retention_state`. One initial open-wa business account is assumed; make phone uniqueness scoped by provider account/session if multi-account is later enabled. Protect phone and other PII with encrypted storage-at-rest and strict access. |
| `conversations` | `id`, `contact_id FK`, `channel` (`whatsapp`), `provider_account_id/session_id`, `provider_thread_id`, `state` (`active_agent`,`admin_owned`,`closed`), `active_intent NULL`, `assigned_admin_id FK NULL`, `last_inbound_at NULL`, `last_outbound_at NULL`, `closed_at NULL`, `version` for optimistic concurrency, timestamps. A client may have multiple conversations. |
| `messages` | `id`, `conversation_id FK`, `provider_message_id NULL`, `direction` (`inbound`,`outbound`), `sender_type` (`customer`,`automated_agent`,`admin`,`system`), `sender_admin_id FK NULL`, `message_type`, `body_text NULL`, `interactive_payload JSONB NULL`, `reply_to_message_id FK NULL`, `agent_run_id FK NULL`, `provider_timestamp NULL`, `delivery_status NULL`, `skill_key NULL`, `application_release NULL`, `created_at`. Include every inbound message and every customer-visible outbound message, including admin replies and selections. Message actor/body are immutable; only delivery projection and retention redaction fields may change. Never put internal admin notes here. |
| `message_attachments` | `id`, `message_id FK`, `provider_media_id NULL`, `storage_key NULL`, `mime_type`, `filename NULL`, `byte_size NULL`, `sha256 NULL`, `scan_status`, `created_at`, `expires_at NULL`. Store binaries only in private object storage, never a public web directory or PostgreSQL blob by default. Keep metadata even if content cannot be retrieved; verify file type/size, malware-scan before admin download/model use, and apply retention/deletion. |
| `message_delivery_events` | `id`, `message_id FK`, `provider_event_id`, `status`, `occurred_at`, `received_at`, `error_code NULL`, minimal `metadata_json`. Append status transitions; unique provider event key per session/account. `messages.delivery_status` is the latest projection. |
| `message_send_attempts` | `id`, `message_id FK`, `attempt_number`, `status` (`started`,`accepted`,`rejected`,`ambiguous`,`failed`), `request_id`, `started_at`, `completed_at NULL`, `provider_message_id NULL`, `error_code NULL`, redacted `response_metadata NULL`. Do not duplicate message body. An ambiguous timeout must be reconciled before resend. |
| `agent_runs` | `id`, `conversation_id FK`, `trigger_message_id FK`, `response_message_id FK NULL`, `skill_key`, `application_release`, `model_provider`, `model_name`, `rollout_mode`, `status` (`started`,`completed`,`handed_over`,`failed`,`cancelled`), `decision_type`, `handover_reason NULL`, `known_fact_ids_json`, `requested_field_keys_json`, `decision_context_hash`, `input_tokens/output_tokens NULL`, `estimated_cost NULL`, `latency_ms NULL`, `error_code NULL`, `created_at`, `completed_at NULL`. Store operational metadata, fact IDs and field IDs available/requested at decision time, and validated structured result references only; never store hidden chain-of-thought or duplicate full prompts/transcripts. |
| `agent_run_drafts` | `id`, `agent_run_id FK UNIQUE`, `draft_text`, `draft_payload_json NULL`, `status` (`pending_admin`,`sent`,`discarded`,`expired`), `expires_at`, `created_at`, `resolved_at NULL`. Private model output for shadow/assisted evaluation, never part of the customer transcript unless an admin explicitly sends it. Restrict access and apply short retention. |
| `runtime_controls` | Singleton `id`, `ai_mode` (`off`,`shadow`,`assisted`,`canary`,`active`), `kill_switch BOOLEAN` (true means block automated outbound sends), `changed_by_admin_id FK NULL`, `reason`, `version`, `updated_at`. Every change also creates an audit event. Default mode is `shadow`; kill switch defaults true in production until explicitly enabled. |
| `ai_rollout_allowlist` | `id`, `contact_id FK UNIQUE`, `enabled_by_admin_id FK`, `reason`, `created_at`, `expires_at NULL`. Used only for `canary`; never inferred from customer content. |
| `client_context` | `id`, `contact_id FK UNIQUE`, `purpose_notes NULL`, `other_shared_context JSONB NOT NULL DEFAULT '{}'`, timestamps. Keep stable/queryable fields in dedicated columns or child tables, not in the flexible JSON field. |
| `property_requirements` | `id`, `contact_id FK`, `conversation_id FK NULL`, `transaction_type` (`buy`,`sell`), `property_type NULL`, `locations JSONB NOT NULL DEFAULT '[]'`, `budget_min/max NUMERIC NULL`, `currency CHAR(3) NULL`, `bedrooms_min/max SMALLINT NULL`, `size_min/max NUMERIC NULL`, `size_unit NULL`, `timing_from/to DATE NULL`, `timing_text NULL`, `purpose NULL`, `preferences JSONB NOT NULL DEFAULT '{}'`, `notes NULL`, `status` (`active`,`paused`,`fulfilled`,`withdrawn`,`superseded`), `match_readiness` (`incomplete`,`ready`,`needs_review`), timestamps. One row per distinct buy or sell requirement; do not overwrite one need with another. |
| `portfolio_assets` | `id`, `contact_id FK`, `property_type NULL`, `location JSONB NULL`, `ownership_status NULL`, `bedrooms NULL`, `size/value NUMERIC NULL`, `size_unit/value_currency NULL`, `acquisition_date NULL`, `portfolio_notes NULL`, `status` (`current`,`sold`,`unknown`), timestamps. One row per customer-shared asset, separate from desired requirements. |
| `context_facts` | `id`, `contact_id FK`, `entity_type` (`client_context`,`requirement`,`portfolio_asset`), `entity_id`, `field_key`, `value_json`, `confidence NUMERIC NULL`, `source_type` (`customer_message`,`admin`,`agent_extraction`), `source_message_id FK NULL`, `created_by_admin_id FK NULL`, `valid_from`, `superseded_at NULL`, `created_at`. Append each accepted fact/correction and supersede the old fact; preserve original phrasing separately where needed. Enforce confidence in `[0,1]`; validate that `entity_id` exists and belongs to `contact_id` in the repository/service because this polymorphic reference cannot use a normal single-column FK. |
| `handover_cases` | `id`, `conversation_id FK`, `status` (`open`,`assigned`,`admin_owned`,`resolved`,`handed_back`), `reason_code`, `reason_summary NULL`, `trigger_message_id FK NULL`, `assigned_admin_id FK NULL`, `opened_at`, `resolved_at NULL`, `handback_at NULL`, timestamps. Enforce only one active case per conversation with a partial unique index. |
| `admin_context_notes` | `id`, `conversation_id FK`, `handover_case_id FK`, `admin_id FK`, `note_text`, `created_at`, `superseded_at NULL`. Private-only; excluded from messages and transcript exports by default. Record create/read/supersede in audit events. |
| `crm_links` | `id`, `contact_id FK`, optional `requirement_id FK`, `crm_system`, `external_contact_id NULL`, `external_record_id NULL`, `sync_status`, `last_synced_at NULL`, `last_error_code NULL`, timestamps. Unique external IDs and internal-to-CRM mappings in the correct CRM/account scope. Do not assume one contact maps to only one opportunity. |
| `integration_events` | Durable inbox/outbox: `id`, `integration`, `direction` (`inbound`,`outbound`), `event_type`, `external_event_id NULL`, `aggregate_type/id`, `payload_json`, `status` (`pending`,`processing`,`succeeded`,`retry_wait`,`dead_letter`,`ambiguous`), `attempt_count`, `next_attempt_at NULL`, `lease_owner/lease_expires_at NULL`, `last_error_code NULL`, timestamps, `processed_at NULL`. Unique inbound `(integration, external_event_id)` when ID exists; workers claim with expiring leases. Payloads must be minimal, access-restricted, redacted where possible, and covered by retention policy. |
| `audit_events` | `id`, `actor_type`, `actor_id NULL`, `action`, `entity_type`, `entity_id`, `reason NULL`, `before_json/after_json NULL` (redacted/minimal), `request_id NULL`, `created_at`. Append-only. Record admin send/takeover/hand-back, profile corrections, consent changes, CRM retries, exports, and access to private notes. |
| `transcript_exports` | `id`, `requested_by_admin_id FK`, `filters_json`, `status` (`queued`,`running`,`ready`,`failed`,`expired`), `deidentified BOOLEAN`, `row_count NULL`, `artifact_uri NULL`, `checksum NULL`, `error_code NULL`, `requested_at`, `ready_at NULL`, `expires_at NULL`. Store artifacts in private object storage, not a public/static directory; audit download and delete expired artifacts. |
| `skill_review_runs` | `id`, `mode` (`scheduled`,`manual`), `requested_by_admin_id FK NULL`, `cohort_filters_json`, `reviewer_skill_key` (must be `skill_reviewer`), `application_release`, `status` (`queued`,`running`,`completed`,`failed`,`cancelled`), `conversation_count`, `message_count`, `proposal_count`, `result_artifact_uri NULL`, `error_code NULL`, `started_at NULL`, `completed_at NULL`, `created_at`. Store de-identified cohort references and aggregate counts; do not duplicate full transcripts in this table. |
| `skill_improvement_proposals` | `id`, `skill_review_run_id FK`, `affected_skill_key`, `fingerprint` (stable hash for duplicate detection), `title`, `issue_category`, `summary`, `frequency_count`, `distinct_conversation_count`, `cohort_size`, `severity`, `evidence_refs_json` (pseudonymous internal references), `proposed_diff` (Markdown patch/text), `regression_tests_json` or private artifact URI, `validation_status`, `status` (`pending_review`,`approved`,`rejected`,`implemented`,`deployed`,`superseded`), `reviewed_by_admin_id FK NULL`, `decision_reason NULL`, `reviewed_at NULL`, `code_change_ref NULL`, `deployed_release NULL`, timestamps. Unique `(skill_review_run_id, affected_skill_key, fingerprint)`. A proposal is not a live skill version. Approval only authorizes a human to prepare a code change; Git review/CI/deployment remain mandatory. |

#### Database constraints, indexes, and storage rules

1. Enforce foreign keys and check constraints for all status fields, `transaction_type`, `direction`, `sender_type`, confidence, valid currency format, nonnegative amounts/areas, and ordered min/max ranges. Require `sender_admin_id` for `sender_type=admin`; require it to be null for other sender types. Require `agent_run_id` only for automated messages. Use explicit deletion behavior; do not cascade-delete a contact into its transcript without the approved deletion workflow.
2. Add unique constraints for `(provider_account_id, provider_message_id)` when provider IDs exist; `(integration, external_event_id)` for inbound event dedupe; `(message_id, provider_event_id)` for status event dedupe; `(message_id, attempt_number)` for send attempts; one active fact per `(entity_type, entity_id, field_key)` where `superseded_at IS NULL`; one active handover per conversation; and `(skill_review_run_id, affected_skill_key, fingerprint)` for proposal dedupe. Use partial unique indexes where nullable IDs/statuses require them.
3. Add indexes for `conversations(contact_id, updated_at DESC)`, `conversations(state, updated_at)`, active handover queue `(status, assigned_admin_id, opened_at)`, messages `(conversation_id, created_at, id)`, partial unique provider message ID `(provider_account_id, provider_message_id)`, client requirement/asset lookups `(contact_id, status)`, facts `(entity_type, entity_id, field_key, superseded_at)`, agent runs `(conversation_id, created_at DESC)`, review jobs `(status, created_at)` and pending proposals `(status, created_at)`, and worker polling `(status, next_attempt_at, lease_expires_at)`.
4. For inbound processing: validate event → insert inbox event with unique idempotency key → insert message and update conversation in one transaction → commit → return 2xx. A duplicate must return success without reapplying facts or generating a second reply.
5. For outbound processing: in one transaction create the message intent and outbox item; worker claims a leased item; record send attempt; call open-wa outside the DB transaction; record result/status. Never hold a DB transaction open during network/LLM calls. Re-check conversation ownership and opt-out before send.
6. Use a transaction or optimistic version check when changing conversation state, merging facts, claiming work, and opening/closing handover. Preserve per-conversation event order with a lock or serialized queue key.
7. For every accepted extraction or admin correction, atomically append a `context_facts` row, supersede the prior active fact for the same entity/field, and update the owning current-state column (`property_requirements`, `portfolio_assets`, or `client_context`). The structured domain row is the queryable current projection; `context_facts` is its provenance/history ledger. Never update the projection without recording its source, and never treat an unaccepted model suggestion as a fact.
8. Do not store open-wa API keys, CRM tokens, LLM keys, or webhook secrets in any table. Do not persist full provider payloads by default; if replay/debug requires a raw payload, store it in a restricted, short-retention event area and redact it.
9. Store the database in encrypted storage, require TLS across network boundaries, use a least-privilege application DB role, and use a separate migration role. Do not put production data in development fixtures or committed test files.
10. Create all schema changes as versioned migrations. Test fresh install and upgrade paths, use backward-compatible expand/migrate/contract changes for deployed environments, and never edit an already-applied migration.
11. Define retention and deletion before production. Deletion/anonymization must cover messages, structured facts, CRM mappings where supported, export artifacts, logs, and backups according to policy. Keep only minimum legally/operationally permitted audit evidence; implement deletion as a controlled job with an auditable result.

#### Migration order and representative stored records

Create migrations in dependency order: (1) admin users/contacts, (2) conversations, (3) messages, attachments, delivery/send-attempt history, (4) context/requirements/portfolio/facts, (5) handovers/private notes/audit, (6) CRM links and inbox/outbox, (7) agent runs/drafts/transcript exports/runtime controls/allowlist, (8) skill review runs/proposals, then (9) constraints/indexes that require the completed tables. Keep each migration small and reversible where safe; document data backfills and destructive changes.

To avoid a circular foreign-key insert for `agent_runs` and automated messages, insert the run as `started`, create the response message with that `agent_run_id`, then update the run's `response_message_id` and terminal status in the same transaction that records the response intent. For failed/handover runs, `response_message_id` can remain null or reference a safe acknowledgement message.

Examples below illustrate storage intent; use real UUIDs and timestamps in the application:

```json
{
  "messages": {
    "direction": "inbound",
    "sender_type": "customer",
    "message_type": "text",
    "body_text": "Looking for a 2 bedroom apartment in Dubai Marina around AED 2m",
    "provider_message_id": "open-wa-message-id",
    "delivery_status": null
  },
  "property_requirements": {
    "transaction_type": "buy",
    "property_type": "apartment",
    "locations": [{"label": "Dubai Marina", "normalized_id": null, "source_text": "Dubai Marina"}],
    "budget_min": null,
    "budget_max": 2000000,
    "currency": "AED",
    "bedrooms_min": 2,
    "bedrooms_max": 2,
    "status": "active",
    "match_readiness": "incomplete"
  },
  "context_facts": {
    "entity_type": "requirement",
    "field_key": "budget_max",
    "value_json": {"amount": 2000000, "currency": "AED", "original_text": "around AED 2m"},
    "confidence": 0.98,
    "source_type": "customer_message",
    "source_message_id": "6c2b8ef0-b1df-4aae-8db9-1caecab7fbd2"
  }
}
```

When an admin sends a reply, insert an outbound `messages` row with `sender_type=admin` and `sender_admin_id`, a `message_send_attempts` row, and an audit event. If open-wa later reports acknowledgement changes, append `message_delivery_events` and update only the current `messages.delivery_status` projection. For an interactive response, store stable option IDs and display labels in `interactive_payload`; when a customer selects an option, store a separate inbound message/event linked to the interactive message ID. Never rewrite the original outbound message to pretend it was the customer's selection.

For transcript reads, use cursor pagination by `(created_at, id)` and authorize access to the contact/conversation. Do not load an unbounded transcript into an admin page or model context. The model receives a bounded, relevant context assembled from recent messages and structured facts; the transcript remains fully stored for authorized audit/export.

The coding agent must update `ARCHITECTURE.md` if an implementation choice changes this contract and must explain why. Do not leave schema behavior as an undocumented guess.

### Milestone 3 — open-wa integration

- Add a documented, version-pinned open-wa Easy API deployment/configuration, separate from the Python package.
- Configure private binding/network access, API key, webhook shared secret, persistent session storage, and event selection.
- Implement `POST /webhooks/open-wa`: validate shared-secret header, validate envelope, deduplicate, persist to durable inbox, and acknowledge only after persistence.
- Implement normalized inbound message and acknowledgement/status event models.
- Persist inbound media as message plus `message_attachments` metadata; store files only in private object storage after size/type checks and malware scanning. If media retrieval is unsupported, retain the event metadata and let the assistant/admin respond safely instead of dropping the message.
- Implement a Python open-wa HTTP client with timeouts, safe retries, typed errors, and redacted logs.
- Add integration health/session status visibility and a documented QR pairing/recovery procedure.
- Never use the SSE event stream as the only durable production ingestion path unless replay/reconciliation is added; account for webhook retries and durable delivery configuration.

**Completion check:** local/staging tests cover valid/invalid secret, duplicate delivery, persistence-before-ack, text send, timeout/error behavior, and reconnect/session status without using a real customer contact.

### Milestone 4 — Conversation processing and outbound reliability

- Implement a per-conversation processing path that preserves ordering as far as practical.
- Persist inbound customer message first, then extract/update context and create response or handover intent.
- Use a transactional outbox for WhatsApp sends and CRM sync so committed workflow state cannot silently diverge from external effects.
- Before sending any AI reply, perform a concurrency-safe final check that the conversation remains AI-owned and the customer has not opted out.
- Enforce runtime rollout controls: `off` generates nothing; `shadow` stores an expiring private draft without sending; `assisted` exposes a draft for explicit admin send; `canary` may send only for supervisor-allowlisted contacts; `active` allows eligible conversations under all normal guardrails. The kill switch blocks AI sends in every mode.
- Store send attempts, open-wa message IDs, errors, and subsequent acknowledgement/status events.
- Make retries idempotent where possible. If send outcome is ambiguous, do not blindly resend a potentially delivered message; surface for reconciliation.

**Completion check:** duplicate and out-of-order webhooks do not duplicate facts or responses; worker restarts recover pending work; AI send is canceled if admin takes over before dispatch.

### Milestone 5 — Admin inbox and handover

- Build an authenticated admin inbox showing conversation transcript, customer context, requirements, portfolio, CRM status, skill/release, and handover reason.
- Implement the operator workflow and UI contract described in “Human interface requirements” below; do not deliver only backend endpoints or a generic chat screen.
- Implement assignment, takeover, direct text reply, private context note, explicit hand-back, and close actions.
- Store admin messages in `messages` as outbound customer-visible messages with `sender_type=admin`, actor identity, payload, provider result, and delivery status.
- Store private context only in `admin_context_notes`, with clear UI separation from the transcript.
- Enforce server-side authorization and audit every state transition and admin action.

**Completion check:** end-to-end test triggers unknown-question handover, prevents AI reply, allows admin reply, retains that reply in history, and resumes only after explicit hand-back.

#### Human interface requirements

Build a responsive internal admin application (desktop-first, usable on tablet) with these views:

1. **Inbox/queues:** tabs or filters for `Open handovers`, `Assigned to me`, `Unassigned`, `AI active`, and `Closed`; search by customer name/phone with appropriate access; sort by latest inbound message and age of open handover; show unread/new-message indicator, assignee, intent, handover reason, last activity, and CRM sync warning. Do not put full message content in notification previews/logs.
2. **Conversation workspace:** three clearly separated areas: (a) chronological customer-visible transcript; (b) client context with buy requirements, sell requirements, portfolio, and fact provenance/correction controls; (c) ownership/handover/CRM/skill status. Paginate older transcript items by cursor and maintain scroll position on new events.
3. **Transcript rendering:** visibly label customer, automated agent, and each admin sender; show timestamps, interactive cards/options and selected responses, media placeholders/secure previews, and send/ack/failure status. Escape all customer-controlled text. Clearly mark open-wa delivery as accepted/sent/delivered only when that status is actually known.
4. **Admin composer:** support text in MVP and feature-gated supported interactive messages. Show whether automation is paused, who owns the conversation, recipient, and any applicable send restrictions before submit. Disable duplicate submits while in flight; use a client-generated idempotency key; display queued/sent/failed/ambiguous status and a safe retry/reconcile action.
5. **Private context:** provide a visually distinct internal note composer, separate from the WhatsApp composer. Require explicit action to add a private note; display a persistent “internal only” label. Private notes must never enter the outbound message route or transcript-export payload by default.
6. **Ownership actions:** allow authorized admin to claim/assign, take over, resolve, and explicitly hand back. Confirm hand-back, show the open handover reason and private context that will be included in the next AI turn, and record actor/time. A successful admin send does not imply hand-back.
7. **Context correction:** allow an authorized admin to correct extracted fields while showing original customer wording, source message, current normalized value, confidence, and edit history. Require a reason or source for corrections; save the corrected projection and fact ledger atomically.
8. **Operational errors:** show clear nontechnical states for open-wa disconnected, send ambiguous/failed, CRM sync failed, and worker backlog. Provide retry/reconcile only when safe; never silently mark a failed action complete.
9. **Skill proposal review:** provide a supervisor-only queue showing pending proposals, cohort/frequency, affected skill/release, redacted evidence, proposed diff, generated tests/results, risk notes, and approve/reject with required reason. Clearly state that approval requests a code change and does not publish a live skill.
10. **AI drafts and rollout control:** in shadow mode, keep generated replies private and label them as drafts that were not sent. In assisted mode, an admin must edit/approve/send the draft; sent content is stored as an admin-authored message. Show current rollout mode and kill-switch state to supervisors, record who changed it and when, require a reason, and make disabled state visible to operators. Do not let a model or skill file change rollout mode.

**Human interface authorization and behavior:**

- Authenticate every page/API request with a managed identity/OIDC provider or an equivalent secure mechanism. Do not implement a shared hard-coded password. Disable inactive users and enforce server-side role checks on every action.
- Minimum roles: `admin` (conversation operations and profile correction), `supervisor` (admin plus assignment/export/override permissions), and `read_only` (view only, no notes/replies/exports unless explicitly granted). Keep role mapping configurable.
- Require CSRF protection for cookie-authenticated state changes, secure/HTTP-only/SameSite cookies, session expiry, and audit logs for sign-in and privileged actions. MFA should be enabled through the identity provider where available.
- Default to least-privilege queue/contact access. Every transcript, context, private note, and export request must be authorized server-side; hiding a UI control is not authorization.
- Make handovers operationally visible: show queue age and assignment state, define configurable response-time targets, and surface overdue/unassigned cases in the inbox. Do not silently auto-resume AI or auto-close a handover when a target is missed.
- Use short polling with cursor/version updates for MVP inbox freshness unless a reliable push channel is already present. If WebSocket/SSE is added, it must re-check authorization, handle reconnects, and never become the source of truth; PostgreSQL remains authoritative.
- If two admins open the same conversation, show current owner and use optimistic version checks. Reject stale takeover/hand-back/profile edits with a refresh prompt rather than overwriting newer work.

**Completion check:** browser-level flow covers login, queue filtering, claim/takeover, private note, direct reply, delivery state, profile correction, transcript pagination, and explicit hand-back. A read-only role cannot mutate via direct API calls. Admin replies appear in the same chronological transcript as customer and automated messages.

### Milestone 6 — Structured real estate context and CRM adapter

- Support multiple simultaneous buy and sell requirements and multiple portfolio assets per contact.
- Track type, locations, budget/expected price, currency, timing, purpose, property size/bedrooms, optional preferences, free-text context, and status/readiness as described in `ARCHITECTURE.md`.
- Store provenance, confidence, source message/admin, and fact history. Preserve original customer wording alongside normalized fields.
- Add CRM adapter interface, field mapping configuration, fake CRM, and sync status UI/API.
- When a CRM is selected, add a concrete adapter and sandbox integration without coupling conversation logic to CRM vendor payloads.
- Retry sync safely; define conflicts so low-confidence extraction does not overwrite verified human data.

**Completion check:** buyer, seller, portfolio, and mixed-intent examples map to distinct records; corrections retain history; CRM failures are visible and retryable without duplicate CRM records.

### Milestone 7 — Markdown skills and assistant behavior

- Add a Markdown skill loader with schema/header validation and stable skill keys.
- Create initial buyer, seller, portfolio, general questions, and unknown/handover skills.
- Skills define purpose, triggers, required/optional fields, question order, structured options, free-text fallback, allowed claims, and handover rules.
- Implement LLM provider interface, deterministic fake provider, extraction schema validation, confidence/ambiguity handling, and bounded context construction.
- Store `skill_key` and application release on automated messages.
- Add offline evaluation fixtures based on synthetic or redacted conversations; do not commit unredacted customer transcripts into source control.

**Completion check:** skill files load deterministically; malformed files fail safely; extraction does not lose facts; unknown/low-confidence/sensitive cases hand over; untrusted message content cannot alter system instructions.

#### Skill file format and runtime contract

Skills are plain Markdown files in `skills/`, loaded by the application from the deployed release. They contain human-authored instructions and declarative metadata only; never execute Python/JavaScript embedded in a skill or allow a skill to grant tools/permissions. Add a parser/validator for frontmatter and fail startup or disable the invalid skill with a clear operator error rather than silently interpreting malformed content.

Use a consistent frontmatter contract such as:

```yaml
---
key: buyer_intake
title: Buyer intake
version: 1
intent: [buy]
priority: 100
required_fields: [property_type, locations, budget, timing, purpose]
optional_fields: [bedrooms, size, preferences]
handover_on: [unknown_question, low_confidence, sensitive_topic, customer_requests_human]
---
```

The remainder is Markdown instructions containing: scope and exclusions; allowed claims and sources; field definitions/normalization; question strategy; interactive prompts with stable option IDs and free-text fallback; handling for ambiguity, contradictions, correction, opt-out, and unsupported requests; and a few synthetic/redacted examples. Keep option IDs stable across label translations. Do not put customer-specific facts or private admin context in shared files.

Implement the skill runtime in this order for every customer message:

1. Load active skill definitions for the deployed release and validate them at startup. Record the release/commit identifier.
2. Classify intent against a constrained set (`buy`, `sell`, `portfolio`, `general`, `unknown`, `mixed`) and select skills by explicit intent/triggers/priority. If mixed, compose the relevant skills in a deterministic order; do not ask the model to invent a new skill or path.
3. Build a bounded context from relevant current profile projections, active facts, recent transcript turns, unanswered required fields, open handover/private notes (only if authorized), customer consent, and the selected skill text.
4. Ask the model for a validated structured proposal: intent, candidate fact patches with source message and confidence, unresolved conflicts/missing fields, selected skill key, response text or stable interactive-option key, and handover decision/reason. Reject unknown keys, invalid IDs, malformed values, and out-of-range confidence.
5. Application policy validates the proposal. It decides whether facts are accepted, whether a correction/clarification is needed, whether the conversation is eligible for automation, and whether the response is safe to send. The model cannot choose a recipient, invoke arbitrary tools, alter ownership, or write to CRM directly.
6. In one transaction, persist the `agent_runs` decision, accepted `context_facts` and current projections, handover state if applicable, and outbound message/outbox intent. Preserve the inbound message as the trigger/source.
7. Apply rollout mode: `shadow` writes a short-lived private `agent_run_drafts` record; `assisted` displays a draft requiring admin send; `canary` sends only for allowlisted contacts; `active` sends only when all guardrails pass; `off` does not call the model. `kill_switch=true` blocks automated sends in every mode.
8. Dispatch through the outbox only after a final state/opt-out/rollout check; store skill key, application release, model identifier, and outcome metadata. If generation fails or a proposal is invalid, send a configured safe acknowledgement only if policy permits; otherwise hand over.

**Skill routing rules:** explicit customer intent beats inferred intent; customer corrections supersede prior facts with provenance; low-confidence candidate facts are not written to the current profile; an unanswered required field may prompt a single concise follow-up; never ask again for a known field unless it is contradictory or stale. Skills should be composable, but avoid two skills asking competing questions in one turn. Unknown questions route to the human handover policy.

**Skill tests and release control:**

- Store test fixtures as synthetic or de-identified cases in version control, never raw identifiable transcripts.
- Validate frontmatter keys, field IDs, interactive option IDs, and skill key uniqueness in CI/startup.
- Test each skill for normal completion, free-text response to an option prompt, ambiguity, contradiction, unsupported claim, prompt injection, opt-out, and handover.
- Compare the candidate skill release against the prior deployed release on a fixed evaluation set; report schema validity, required-field capture, unnecessary repeat questions, handover correctness, unsupported claims, and interactive fallback behavior.
- Pin `application_release` and `skill_key` on every automated response. Rollback is a code rollback/redeployment; never edit live skill text in the database.

**Completion check:** adding a new Markdown skill and deploying it changes behavior without a DB skill-table migration; malformed/duplicate skills cannot be activated; each reply is traceable to the skill and release; an unknown or unsafe case reliably yields a handover rather than an invented answer.

#### Offline skill reviewer contract (`skills/skill_reviewer.md`)

Add `skills/skill_reviewer.md` as a separate analysis skill. It is never selected for customer conversations and cannot send WhatsApp messages. The background worker supplies it with a bounded, de-identified dataset and the deployed skill/test versions to review. The reviewer should identify recurring behavior defects such as asking for a budget already clearly captured, ignoring a customer correction, repeating a field question, or failing to hand over an unsupported question.

**Run policy:** support both scheduled and supervisor-triggered runs. Start with a configurable weekly cohort of the prior seven days and a default threshold of at least three distinct conversations from three distinct contacts before proposing a recurring fix. Below threshold, record a monitor-only finding or no proposal. Do not create a scheduled job that repeatedly re-processes the full corpus by default. Make period, cohort, threshold, cost/time limits, and enablement configurable. Use a deterministic cohort snapshot so a run can be reproduced.

**Data passed to reviewer:**

- De-identify phone numbers, names, emails, and irrelevant exact addresses before model input; use stable pseudonyms only within one review run where cross-message linking is needed.
- Include only messages/context needed to judge a behavior: relevant customer turns, automated replies, admin corrections/replies, the structured facts known at each turn, skill key/release, and outcome metadata.
- Exclude private admin context notes, credentials, raw webhook payloads, CRM secrets, and unrelated customer/portfolio information.
- Preserve source references as access-controlled opaque IDs so a human reviewer can inspect the original conversation if authorized. Never expose raw identifiers in the proposal's default view.
- Apply the same retention and deletion rules as transcript exports; do not add the de-identified batch to general model training or persist prompts/transcripts in provider logs.

**Reviewer output:** require schema-validated JSON containing (a) issue category/title, (b) concise explanation, (c) affected skill key and release, (d) frequency and distinct-contact denominator, (e) opaque evidence references, (f) root-cause hypothesis clearly labeled as a hypothesis, (g) proposed unified diff or precise Markdown edits, (h) regression cases with input/context/expected behavior, (i) risk/side effects, and (j) confidence. No executable arbitrary code, no direct filesystem/repository tools, no credentials, no personal data in the generated patch/tests.

**Validation and approval:**

1. Validate output against a strict schema; reject missing evidence, unsupported skill keys, unsafe content, malformed patches/tests, or out-of-cohort references.
2. Verify thresholds from the selected cohort in application code; do not trust model-reported frequency counts.
3. Run proposed regression cases only in an isolated fixed test harness with no network, secrets, production database, WhatsApp, or CRM access. Tests must be declarative JSON cases interpreted by trusted test code; never execute/import generated scripts. Apply resource/time limits.
4. Store the run and proposal records, reviewer release, test results, and failure reason. Deduplicate repeated proposals using the fingerprint.
5. Show a supervisor the issue, distinct conversation count/cohort denominator, redacted evidence links, before/after skill diff, test cases/results, risk notes, and affected release.
6. Supervisor must explicitly approve or reject with a reason. Approval records consent to prepare a code change only; a human developer applies the change, reviews it in version control, runs normal CI, and deploys. Only then mark the proposal `deployed` with the release ID. Rejection never suppresses future recurrence permanently; a future run may resurface it with new evidence.
7. The reviewer must have read-only access to the sanitized analysis input and write access only to its run/proposal records. It must not update customer profiles, messages, CRM, skill files, branches, pull requests, or deployments.

**Example expected proposal:** “Buyer intake asked for budget again after the customer stated AED 2m.” Evidence should demonstrate the budget fact existed before the repeated question in at least the configured number of independent conversations. The draft should propose a rule/test that asserts known requirement fields are omitted from missing-field questions, while asking for clarification if currency/range is genuinely ambiguous.

To establish this evidence, use each `agent_runs.known_fact_ids_json` and `requested_field_keys_json` with fact validity timestamps and the triggering message order. Admin replies/corrections are useful signals, not automatic proof that the admin was correct. Prefer explicit customer corrections, confirmed fact provenance, repeated pattern across independent contacts, and human reviewer verification; label uncertain cases as inconclusive rather than a skill defect.

**Completion check:** a scheduled and manual review run process only the frozen eligible cohort; PII/private notes do not reach the reviewer; repeated failures produce a deduplicated proposal and runnable regression cases; sub-threshold anecdotes do not become proposed universal rules; no proposal changes a live skill without explicit human approval plus code review, CI, and deployment.

### Milestone 8 — Interactive WhatsApp options

- First verify the open-wa license and exact installed version support for intended interactive types.
- Add stable option identifiers to skill definitions and provider mapping behind the open-wa adapter.
- Persist the outbound interactive message and provider message ID. Normalize selected option callbacks, including the originating interactive message ID where supplied.
- Continue to accept ordinary typed replies and provide a text fallback.
- If licensing/provider support is unavailable, leave the feature disabled and complete all required flows with text prompts.

**Completion check:** automated and admin interactive sends are stored; selection events update the correct field; unsupported sends fall back safely; no option ID depends on a translated/display label.

### Milestone 9 — Transcript export and controlled learning loop

- Implement role-protected, audited transcript export for all conversations or filters (date range, handover reason, skill, status, etc.).
- Include customer, automated-agent, and admin-sent messages, including interactive content and selected options.
- Exclude internal admin notes by default. Provide de-identified exports by default and require explicit elevated access for identifiable data.
- Record export requester, filters, time, row count, and output reference; apply export retention/deletion policy.
- Implement `skill_reviewer.md` as a separate offline skill, scheduled/manual job, frozen de-identified cohorts, minimum recurrence threshold, strict structured output validation, isolated test generation/execution, run/proposal persistence, and proposal deduplication as defined above.
- Add supervisor endpoints/UI to inspect evidence, diff, tests, and risk, then approve/reject with reason. Approval creates a human work item only.
- Document the human workflow: review patterns → reviewer draft → supervisor approval → human applies Markdown/test change → code review/CI → deploy → mark proposal deployed. Do not auto-train, auto-edit the repository, auto-merge, or auto-publish.

**Completion check:** export matches stored transcript, includes admin responses, excludes private notes, and leaves an audit record; exported files are not committed to the repository.

**Skill-review completion check:** scheduled/manual runs are bounded, repeatable, de-identified, thresholded, and audited; proposals include evidence, a safe diff, and regression cases; supervisor approval cannot bypass repository review/CI/deployment.

### Milestone 10 — Production readiness and handoff

- Add deployment instructions for FastAPI, worker(s), PostgreSQL, and open-wa separately; define startup order, health checks, network rules, session volume, secrets, and backups.
- Add observability for queue age, webhook failures/duplicates, open-wa session state, send failures, handover backlog, CRM lag, LLM errors/cost, and interactive fallback.
- Add security protections for admin sessions (MFA where supported, CSRF protection, role checks), escape untrusted transcript content in the UI, and redact sensitive fields in logs.
- Define deletion/anonymization and retention behavior across database records, transcript exports, CRM mappings, logs, and backups; document export access and expiry.
- Establish recovery objectives and exercise database backup restore, open-wa session recovery/re-pair, and integration dead-letter replay in staging.
- Document recovery: open-wa re-pair, webhook replay/reconciliation, database restore, queue recovery, CRM retry, and admin override.
- Review security, data retention, consent/opt-out, access roles, and transcript-export permissions.
- Run full automated checks and end-to-end staging scenarios. Clearly distinguish simulated integrations from verified live integrations.
- Provide a final operator runbook, user/admin guide, environment variable reference, and known limitations.

**Completion check:** staging walkthrough passes; all critical failures are observable and recoverable; no unresolved security-critical issues; live credentials/integrations remain explicitly identified as pending if not supplied.

## 7. API and UI behavior requirements

Implement API shapes consistent with `ARCHITECTURE.md`, including:

- open-wa webhook ingestion.
- Conversation list/detail with transcript and context.
- Admin reply and interactive reply actions.
- Handover creation, assignment, takeover, private context, and hand-back.
- Context, requirement, and portfolio corrections.
- CRM sync status/retry.
- Audited transcript export.
- Scheduled/manual skill review runs and a human approval queue for generated proposals.
- Supervisor-only rollout mode and kill-switch controls, canary allowlist, and explicit admin-send action for assisted drafts.

All mutations require authentication/authorization, request correlation, validation, and idempotency where external side effects can repeat. Never expose open-wa API credentials in browser code. The UI must visibly distinguish customer messages, assistant messages, admin-sent messages, and private admin notes.

## 8. Required quality checks

The coding agent should add and run appropriate checks, at minimum:

- Formatting and linting for Python and any deployment/config code.
- Static/type checks for Python.
- Unit tests for schema validation, state transitions, skills, extraction merge/provenance, authorization, and open-wa payload normalization.
- Integration tests for database migrations/repositories, inbox/outbox retry/idempotency, CRM fake adapter, and open-wa HTTP client using a stub server.
- End-to-end test covering customer message → structured extraction → assistant response → unknown handover → admin reply → explicit hand-back → assistant resumes.
- Interactive message test where licensed functionality is represented by a stub; actual device test is a separate staging check.
- Security checks for webhook shared-secret validation, admin authorization, private-note exclusion, export controls, and secrets redaction.

Never state “tested” unless the relevant command ran and passed. If an environment limitation blocks a test, report the command and exact limitation and continue with other checks.

## 9. Definition of done

The project is complete when:

- A documented local environment can be started with the Python application, PostgreSQL, worker, and open-wa service.
- Inbound WhatsApp events are authenticated, persisted once, and recoverable after transient failure.
- Every customer-visible message, including every admin reply, appears in the correct conversation history.
- Buyer/seller requirements, current portfolio, and additional context are structured with provenance and suitable for a future UAE asset matcher.
- An admin can take over, reply, add private context, and explicitly hand back without accidental AI messages during ownership.
- CRM sync works through a real adapter when credentials exist, and safely through a fake adapter otherwise.
- Markdown skills control behavior and skill/release traceability is stored; skill changes require review and deployment.
- Interactive options work only when the chosen open-wa license/runtime supports them; text fallback remains complete.
- Authorized full/filtered transcript exports include admin messages, omit private notes by default, and are audited.
- Migrations, automated checks, deployment guide, environment reference, and recovery runbook are present.
- Remaining external dependencies (CRM selection/credentials, open-wa license, real WhatsApp account, LLM credentials, deployment target) are listed precisely and are not misrepresented as completed.

## 10. Final report format

At completion, report:

1. What was implemented, grouped by milestone.
2. Important architecture decisions and any deviations from the two design documents.
3. Commands/checks run and their results.
4. How to start the local/staging system.
5. Which integrations are live versus mocked.
6. Required user actions or credentials still needed.
7. Known limitations and operational risks, especially open-wa session/licensing status.
