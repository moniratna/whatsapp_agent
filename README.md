# WhatsApp Real Estate Assistant

FastAPI application for durable WhatsApp intake, real estate context, human handover, controlled AI assistance, and asynchronous CRM synchronization. This repository integrates with an open-wa Easy API instance operated separately; it does not require open-wa to run on the application host. Live WhatsApp, Bedrock, and Zoho use remain disabled until credentials, policy, and staging checks are complete.

## Local setup

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). Docker Compose is used only for local PostgreSQL.

```sh
uv sync
cp .env.example .env
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

The API listens on `http://127.0.0.1:8000`; interactive documentation is at `/docs`. By default, run database-backed workers in separate terminals:

```sh
uv run python -m app.workers.conversation
uv run python -m app.workers.outbound
uv run python -m app.workers.crm_sync
uv run python -m app.workers.exports
uv run python -m app.workers.skill_reviews
uv run python -m app.workers.skill_updates
```

For a single-process local setup, set `RUN_WORKERS_IN_API=true` in `.env` and start only `uv run uvicorn app.main:app --reload`. FastAPI starts the conversation, outbound, CRM sync, exports, skill review, customer skill update, and reviewer update workers, then stops them on shutdown. Workers with disabled integrations or feature flags remain idle. This option is restricted to development/test; use separately supervised worker processes outside local development.

With the example settings, the conversation worker uses a deterministic fake LLM, the outbound worker uses a local transport fake (synthetic provider IDs only), and the CRM worker uses an in-memory fake with synthetic object mappings. No external messages or CRM records are created. These fakes are allowed only in `development` and `test`; staging and production require configured integrations.

To exercise webhook intake locally, set `OPENWA_ENABLED=true`, set `OPENWA_WEBHOOK_SECRET`, and leave `OPENWA_TRANSPORT_MODE=fake`. The server accepts either an `X-Webhook-Secret` header with that value or an `X-OpenWA-Signature` HMAC generated with that value; the outbound worker still cannot contact WhatsApp. For a local-only automated-send walkthrough, change the runtime control through the supervisor API while the fake transport is selected. Do not use live transport settings for this walkthrough.

`GET /health/live` reports process health. `GET /health/ready` checks the database and configured integration prerequisites without returning secret values. Use `uv run alembic revision --autogenerate -m "description"` to create migrations and `uv run alembic upgrade head` to apply them.

## Local admin API

Development authentication is deliberately isolated to `APP_ENV=development` or `test`. Supply `X-Dev-User` and `X-Dev-Role` headers (`admin`, `supervisor`, or `read_only`) when calling `/api/*`. Production and staging require a verified OIDC bearer token with the configured role claim. No shared password or unauthenticated admin route is provided.

The admin API covers conversation queues and paginated transcripts, context and provenance, requirements, portfolio assets, handover, direct replies, private notes, explicit hand-back, consent correction, CRM status/retry, transcript exports, skill review proposals, and supervisor rollout controls. Private notes and unsent drafts remain separate from the customer transcript.

## Integrations and safety

- **open-wa:** Configure the private URL, API key, webhook secret, and `OPENWA_ENABLED=true` only for an externally operated instance. The webhook accepts either `X-Webhook-Secret` or OpenWA's `X-OpenWA-Signature` HMAC-SHA256 over the exact raw request body, using `OPENWA_WEBHOOK_SECRET`. It accepts both legacy `payload.message` and newer `data` event envelopes. FastAPI durably stores webhook messages before acknowledgement. Keep the open-wa API and pairing/health endpoints private.
- **Bedrock:** The adapter uses Bedrock Runtime `Converse`, the configured model and region, and the `AWS_BEARER_TOKEN_BEDROCK` bearer key. The deterministic fake is used locally. No provider credentials are stored in the database or logs.
- **Zoho:** The API v8 adapter refreshes OAuth access tokens and uses explicit module, unique-field, and field mappings. Configure `ZOHO_MODULE_MAPPING_JSON` and `ZOHO_FIELD_MAPPING_JSON` from the target organization’s CRM metadata; no custom module or field is assumed. The fake is local only.
- **AI rollout:** Runtime controls begin in `shadow` with the kill switch on. Shadow/assisted behavior creates short-lived private drafts. Automated sends require an authorized rollout change and pass a final ownership, consent, handover, and kill-switch check.
- **Opt-out:** Exact `STOP`, `STOP ALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, and `QUIT` messages take effect during intake. All outbound sends remain blocked while opted out. Restoring consent requires a recorded `START` or `UNSTOP` message and an audited admin action; automation still requires explicit hand-back.
- **Exports:** De-identified JSONL is the default, private notes are excluded, and creation/downloads are audited. Common phone, email, URL, introduced-name, and address patterns are redacted; local files use restrictive permissions. Staging/production require a private S3 bucket with server-side encryption and IAM access controls. Exports expire after the configured retention period.
- **Skills:** Reviewed Markdown files in `skills/` define routing and behavior. Proposals from review jobs never edit or deploy these files.
- **Scope:** Email, inventory ingestion, availability claims, pricing claims, and property matching are out of scope.

## Checks

```sh
uv run ruff format --check app tests migrations
uv run ruff check app tests migrations
uv run mypy app tests
uv run pytest -q
uv run alembic upgrade head
uv run alembic check
```

The customer-to-handover-to-admin-reply-to-handback-to-resume flow is covered by `uv run pytest -q tests/test_end_to_end_flow.py` and uses only local fakes.

For the Alembic checks, configure `DATABASE_URL` to a disposable database. The checked migration chain also supports SQLite for local schema verification; PostgreSQL remains the intended runtime database.

## Operational references

- [Operations and recovery](docs/operations.md)
- [Implementation decisions and external prerequisites](docs/decisions.md)
- [Environment example](.env.example)
- [Authoritative project guide](AI_CODING_AGENT_GUIDE.md)
