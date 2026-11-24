# Implementation decisions and remaining prerequisites

## Defaults implemented

- Python 3.11+ with `uv`, FastAPI, SQLAlchemy 2, PostgreSQL as the runtime database, and Alembic for every schema change.
- Database-backed integration inbox/outbox and leased workers for conversation processing, WhatsApp sends, CRM sync, exports, and skill reviews.
- open-wa is an external, separately operated service per the user's direction. This app keeps a typed HTTP client, authenticated webhook boundary, normalization, deduplication, and local fake transport. No open-wa instance is required on the Python app host.
- Amazon Bedrock Runtime Converse is the only live LLM adapter. The local deterministic fake makes the flow testable without credentials. `boto3>=1.39` is locked for bearer-token API key support.
- Zoho CRM API v8 is application-to-CRM only. The real adapter refreshes OAuth tokens and uses explicit module, unique-key, and field mappings. Local fake CRM uses synthetic object names and never calls Zoho.
- Version-controlled Markdown skills define buyer, seller, portfolio, general, unknown, and reviewer behavior. Skill review produces a human proposal only; no code or skill file is edited at runtime.
- OIDC is required for staging/production. Development auth headers work only in local/test environments.
- Runtime defaults to `shadow` with the kill switch on. Interactive messages are disabled and use a stable text fallback until the external open-wa license/runtime is verified.
- Candidate facts are restricted to allowlisted structured fields, require model confidence of at least 0.90, retain source text/message and provenance, and cannot override admin-verified values. A decision below 0.65 confidence hands over.
- Exact inbound STOP commands atomically set `opted_out`, open/retain handover, and pause processing. Until the business human-response policy is approved, all sends (including admin sends) are blocked after opt-out. Restoring consent requires an inbound `START` or `UNSTOP` record and an audited admin correction; AI still requires explicit hand-back.
- Transcript exports are audited, de-identified by default, exclude private notes, and expire after 24 hours by default. Local artifacts are private files; staging/production readiness requires a private S3 bucket with server-side encryption and IAM access.
- The skill-review baseline uses de-identified aggregate handover reason codes and requires at least three distinct conversations. It does not send raw chat history to a reviewer model. This is a deliberately narrow first review path; expanding the reviewer to inspect de-identified transcript cohorts should be a separately reviewed change.
- UAE inventory ingestion and property matching remain out of scope.

## Items requiring external credentials or owner decisions

| Item | Current state | Needed before live use |
| --- | --- | --- |
| WhatsApp number and device owner | Not supplied | Choose a non-production test number and accountable device owner. |
| External open-wa service | App-side adapter is ready; user will operate it separately | Confirm the service URL, pinned release/license, business risk, private network access, session persistence, and re-pair owner. |
| Zoho organization and data center | Not supplied | Provide sandbox organization, OAuth consent/scopes, Accounts/API domains, and validated module/field API names. |
| Zoho ownership/conflict rules | App writes only configured values | Approve field ownership and human-verified CRM precedence in sandbox. |
| Bedrock region/model/API key | Not supplied | Provision bearer-token API key, grant model access, test Converse in selected region, and set expiry/rotation alerts. |
| Production identity | Not supplied | Configure OIDC issuer/client and map claims to `admin`, `supervisor`, and `read_only`; enforce provider MFA. |
| Hosting/secrets/database operations | Not supplied | Choose staging/production hosts, TLS, DB roles, secret manager, encrypted backups, alerting, worker supervision, and restore owner. |
| Language and UAE vocabulary | Not supplied | Approve supported languages, location/property-type vocabulary, and completeness rules before marking matcher-ready. |
| Consent and retention | Conservative local policy implemented; business policy unresolved | Approve customer response after opt-out, retention/deletion periods, backup deletion behavior, and export/reviewer legal basis. |

The app is ready for local integration testing. Live provider activation remains disabled until its required credentials, sandbox checks, and policy decisions are present. Milestone 10 production handoff is intentionally out of scope for the current testing push.

## Implementation record

### Foundation and persistence

- Initialized the uv package, typed settings, structured request logging, liveness/readiness endpoints, local PostgreSQL Compose service, and Alembic migrations.
- The schema includes contacts, conversations, messages, delivery/send history, context, requirements, portfolio, provenance, handovers/private notes, admin identities/audit, rollout controls, CRM links, inbox/outbox, agent runs/drafts, exports, and skill-review records/proposals.
- Later migrations add asset CRM mappings, export/reviewer worker leases, and the automated-message to agent-run foreign key. Fresh upgrade and Alembic drift checks passed on disposable SQLite. PostgreSQL itself was not available in this local environment.

### App-side WhatsApp boundary and processing

- Webhook shared-secret authentication, bounded payload validation, dedupe, durable persistence-before-ack, media metadata retention, and acknowledgement projection are implemented.
- Inbound message and processing event are committed atomically. Conversation processing uses bounded transcript/context plus conversation-scoped private notes, reviewed skills, validated LLM decisions, provenance-backed facts, handover, drafts, or transactional outbound intent.
- The app rechecks ownership and opt-out immediately before dispatch. Ambiguous sends are not blindly retried. Local transport mode produces synthetic provider IDs only.
- Per user direction, open-wa deployment is not required on this host. The optional `deploy/openwa` reference is not part of the Python runtime.

### Admin, CRM, exports, and review

- Role-gated admin operations include queue/transcript/context, direct replies, takeover/handback, private notes, profile/requirement/portfolio corrections, consent correction, rollout/canary control, and CRM sync status/retry.
- Zoho's sandbox/production object mapping is intentionally configuration-driven; no module/field names are assumed.
- Export workers create private, expiring JSONL artifacts. Supervisor API endpoints queue/read/download exports with audits.
- Manual skill-review jobs create aggregate-only proposals after the distinct-conversation threshold. Proposal decisions require a supervisor and reason and never modify Markdown files.

### Dependency security

- The pinned open-wa transport dependency tree originally reported eight high findings. A pinned `image-size@2.0.3` override was added for the advisory affecting versions through 2.0.2; `npm audit --omit=dev` then reported zero vulnerabilities. Review the override when updating open-wa dependencies.
