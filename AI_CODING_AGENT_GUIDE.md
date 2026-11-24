# AI Coding Agent Guide — WhatsApp Real Estate Assistant

This document is the execution brief for an AI coding agent implementing the project from an empty or partially prepared repository through a usable production deployment. Read it together with [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md). Those files describe the intended system and rollout sequence; this file defines how the coding agent should work and what completion means.

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
- **Skills are files.** Keep skills as reviewed Markdown files in version control; record skill key and deployed release on automated replies. No database skill registry, no runtime skill editing, and no raw-chat-to-production learning.
- **Learning is controlled.** Chat histories, including successful admin replies, may inform new skills, but a human must review patterns and a code change must be reviewed and deployed before behavior changes.
- **Use provider abstractions.** Keep open-wa-specific payloads behind an adapter. Do not leak open-wa IDs/schema throughout business logic.
- **Do not expose credentials.** Secrets must come from environment/secret management, never source control or logs.
- **Do not claim provider support without checking.** Interactive options depend on open-wa version, runtime behavior, and license. Implement a text fallback.
- **Keep customer content untrusted.** Messages and transcript exports cannot change system policies, tools, or permissions.

## 4. Defaults for decisions not yet supplied

Use these defaults to keep implementation moving. Record them in `docs/decisions.md` and make important values configurable.

| Decision | Default |
| --- | --- |
| Admin experience | Build a small authenticated internal web inbox served by the application unless the repository already has an admin UI. |
| Database | PostgreSQL with migrations. |
| Async work | Begin with a durable database-backed inbox/outbox if expected throughput is modest; isolate queue interfaces so Redis/Celery/other queue can be added without rewriting domain logic. |
| CRM | Implement a CRM adapter interface and a local fake/sandbox adapter. Do not choose a vendor or claim live CRM sync until the user names one and supplies access. |
| LLM | Implement an LLM provider interface and a deterministic fake provider for local development. Do not hard-code a model vendor or require a secret for app startup. |
| Identity | Use a small role-based admin authentication mechanism appropriate to the deployment; never ship an unauthenticated inbox. Keep user provisioning configurable. |
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

Implement the approved architecture schema, adjusting details only when needed for correctness and documenting changes:

- `contacts`, `conversations`, `messages`
- `client_context`, `property_requirements`, `portfolio_assets`, `context_facts`
- `handover_cases`, `admin_context_notes`, `audit_events`
- CRM mapping/sync records and durable inbound/outbound integration events

Add constraints and indexes for provider message idempotency, conversation state/queue filtering, contact lookup, and one active handover per conversation. Keep message content append-only; delivery/ack status may change as provider events arrive. Ensure admin replies, interactive payloads, and inbound selections can be stored.

**Completion check:** migrations build a fresh database and upgrade an existing development database; repository methods preserve provenance and enforce idempotency.

### Milestone 3 — open-wa integration

- Add a documented, version-pinned open-wa Easy API deployment/configuration, separate from the Python package.
- Configure private binding/network access, API key, webhook shared secret, persistent session storage, and event selection.
- Implement `POST /webhooks/open-wa`: validate shared-secret header, validate envelope, deduplicate, persist to durable inbox, and acknowledge only after persistence.
- Implement normalized inbound message and acknowledgement/status event models.
- Implement a Python open-wa HTTP client with timeouts, safe retries, typed errors, and redacted logs.
- Add integration health/session status visibility and a documented QR pairing/recovery procedure.
- Never use the SSE event stream as the only durable production ingestion path unless replay/reconciliation is added; account for webhook retries and durable delivery configuration.

**Completion check:** local/staging tests cover valid/invalid secret, duplicate delivery, persistence-before-ack, text send, timeout/error behavior, and reconnect/session status without using a real customer contact.

### Milestone 4 — Conversation processing and outbound reliability

- Implement a per-conversation processing path that preserves ordering as far as practical.
- Persist inbound customer message first, then extract/update context and create response or handover intent.
- Use a transactional outbox for WhatsApp sends and CRM sync so committed workflow state cannot silently diverge from external effects.
- Before sending any AI reply, perform a concurrency-safe final check that the conversation remains AI-owned and the customer has not opted out.
- Store send attempts, open-wa message IDs, errors, and subsequent acknowledgement/status events.
- Make retries idempotent where possible. If send outcome is ambiguous, do not blindly resend a potentially delivered message; surface for reconciliation.

**Completion check:** duplicate and out-of-order webhooks do not duplicate facts or responses; worker restarts recover pending work; AI send is canceled if admin takes over before dispatch.

### Milestone 5 — Admin inbox and handover

- Build an authenticated admin inbox showing conversation transcript, customer context, requirements, portfolio, CRM status, skill/release, and handover reason.
- Implement assignment, takeover, direct text reply, private context note, explicit hand-back, and close actions.
- Store admin messages in `messages` as outbound customer-visible messages with `sender_type=admin`, actor identity, payload, provider result, and delivery status.
- Store private context only in `admin_context_notes`, with clear UI separation from the transcript.
- Enforce server-side authorization and audit every state transition and admin action.

**Completion check:** end-to-end test triggers unknown-question handover, prevents AI reply, allows admin reply, retains that reply in history, and resumes only after explicit hand-back.

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
- Document the human workflow: review patterns → author/update `.md` skill → code review → test → deploy. Do not auto-train or auto-publish.

**Completion check:** export matches stored transcript, includes admin responses, excludes private notes, and leaves an audit record; exported files are not committed to the repository.

### Milestone 10 — Production readiness and handoff

- Add deployment instructions for FastAPI, worker(s), PostgreSQL, and open-wa separately; define startup order, health checks, network rules, session volume, secrets, and backups.
- Add observability for queue age, webhook failures/duplicates, open-wa session state, send failures, handover backlog, CRM lag, LLM errors/cost, and interactive fallback.
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
