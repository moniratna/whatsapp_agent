import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base

json_type = JSON().with_variant(JSONB(), "postgresql")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("provider_account_id", "phone_e164", name="uq_contacts_account_phone"),
        CheckConstraint(
            "consent_status IN ('unknown','opted_in','opted_out')", name="ck_contacts_consent"
        ),
        CheckConstraint(
            "retention_state IN ('active','pending_deletion','anonymized')",
            name="ck_contacts_retention_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    preferred_language: Mapped[str | None] = mapped_column(String(32))
    consent_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    consent_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("channel = 'whatsapp'", name="ck_conversations_channel"),
        CheckConstraint(
            "state IN ('active_agent','admin_owned','closed')", name="ck_conversations_state"
        ),
        Index("ix_conversations_state_activity", "state", "updated_at"),
        Index("ix_conversations_contact_activity", "contact_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="whatsapp")
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    provider_session_id: Mapped[str | None] = mapped_column(String(128))
    provider_thread_id: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active_agent")
    active_intent: Mapped[str | None] = mapped_column(String(32))
    assigned_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','supervisor','read_only')", name="ck_admin_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("direction IN ('inbound','outbound')", name="ck_messages_direction"),
        CheckConstraint(
            "sender_type IN ('customer','automated_agent','admin','system')",
            name="ck_messages_sender_type",
        ),
        CheckConstraint(
            "sender_type != 'admin' OR sender_admin_id IS NOT NULL",
            name="ck_messages_admin_sender",
        ),
        CheckConstraint(
            "sender_type = 'automated_agent' OR agent_run_id IS NULL",
            name="ck_messages_agent_run_sender",
        ),
        CheckConstraint(
            "sender_type != 'automated_agent' OR agent_run_id IS NOT NULL",
            name="ck_messages_automated_agent_run_required",
        ),
        CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive','neutral','negative','frustrated')",
            name="ck_messages_sentiment",
        ),
        CheckConstraint(
            "sentiment_confidence IS NULL OR (sentiment_confidence >= 0 AND sentiment_confidence <= 1)",
            name="ck_messages_sentiment_confidence",
        ),
        Index("ix_messages_conversation_order", "conversation_id", "created_at", "id"),
        Index(
            "uq_messages_provider_id",
            "provider_account_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
            sqlite_where=text("provider_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), nullable=False
    )
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    provider_message_id: Mapped[str | None] = mapped_column(String(256))
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_type: Mapped[str] = mapped_column(String(24), nullable=False)
    sender_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    body_text: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str | None] = mapped_column(String(16))
    sentiment_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    interactive_payload: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "agent_runs.id",
            ondelete="CASCADE",
            use_alter=True,
            name="fk_messages_agent_run_id_agent_runs",
        )
    )
    provider_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str | None] = mapped_column(String(24))
    skill_key: Mapped[str | None] = mapped_column(String(128))
    application_release: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationEvent(TimestampMixin, Base):
    __tablename__ = "integration_events"
    __table_args__ = (
        CheckConstraint("direction IN ('inbound','outbound')", name="ck_integration_direction"),
        CheckConstraint(
            "status IN ('pending','processing','succeeded','retry_wait','dead_letter','ambiguous','cancelled')",
            name="ck_integration_status",
        ),
        Index(
            "uq_integration_inbound_external_id",
            "integration",
            "external_event_id",
            unique=True,
            postgresql_where=text("direction = 'inbound' AND external_event_id IS NOT NULL"),
            sqlite_where=text("direction = 'inbound' AND external_event_id IS NOT NULL"),
        ),
        Index(
            "uq_integration_outbound_external_id",
            "integration",
            "external_event_id",
            unique=True,
            postgresql_where=text("direction = 'outbound' AND external_event_id IS NOT NULL"),
            sqlite_where=text("direction = 'outbound' AND external_event_id IS NOT NULL"),
        ),
        Index("ix_integration_worker_poll", "status", "next_attempt_at", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    integration: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(String(256))
    aggregate_type: Mapped[str | None] = mapped_column(String(64))
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    payload_json: Mapped[dict[str, Any]] = mapped_column(json_type, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HandoverCase(TimestampMixin, Base):
    __tablename__ = "handover_cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open','assigned','admin_owned','resolved','handed_back')",
            name="ck_handover_status",
        ),
        Index(
            "uq_handover_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('open','assigned','admin_owned')"),
            sqlite_where=text("status IN ('open','assigned','admin_owned')"),
        ),
        Index("ix_handover_queue", "status", "assigned_admin_id", "opened_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_summary: Mapped[str | None] = mapped_column(Text)
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    assigned_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminContextNote(Base):
    __tablename__ = "admin_context_notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), nullable=False
    )
    handover_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("handover_cases.id"), nullable=False
    )
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admin_users.id"), nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageDeliveryEvent(Base):
    __tablename__ = "message_delivery_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_account_id", "provider_event_id", name="uq_delivery_event_provider"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    provider_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(json_type, nullable=False, default=dict)


class MessageSendAttempt(Base):
    __tablename__ = "message_send_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started','accepted','rejected','ambiguous','failed')",
            name="ck_send_attempt_status",
        ),
        UniqueConstraint("message_id", "attempt_number", name="uq_send_attempt_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="started")
    request_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(256))
    error_code: Mapped[str | None] = mapped_column(String(128))
    response_metadata: Mapped[dict[str, Any] | None] = mapped_column(json_type)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientContext(TimestampMixin, Base):
    __tablename__ = "client_context"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contacts.id"), unique=True, nullable=False
    )
    purpose_notes: Mapped[str | None] = mapped_column(Text)
    other_shared_context: Mapped[dict[str, Any]] = mapped_column(
        json_type, nullable=False, default=dict, server_default="{}"
    )


class PropertyRequirement(TimestampMixin, Base):
    __tablename__ = "property_requirements"
    __table_args__ = (
        CheckConstraint("transaction_type IN ('buy','sell')", name="ck_requirement_transaction"),
        CheckConstraint(
            "status IN ('active','paused','fulfilled','withdrawn','superseded')",
            name="ck_requirement_status",
        ),
        CheckConstraint(
            "match_readiness IN ('incomplete','ready','needs_review')",
            name="ck_requirement_match_readiness",
        ),
        CheckConstraint("budget_min IS NULL OR budget_min >= 0", name="ck_requirement_budget_min"),
        CheckConstraint("budget_max IS NULL OR budget_max >= 0", name="ck_requirement_budget_max"),
        CheckConstraint(
            "budget_min IS NULL OR budget_max IS NULL OR budget_min <= budget_max",
            name="ck_requirement_budget_order",
        ),
        CheckConstraint("size_min IS NULL OR size_min >= 0", name="ck_requirement_size_min"),
        CheckConstraint("size_max IS NULL OR size_max >= 0", name="ck_requirement_size_max"),
        CheckConstraint(
            "size_min IS NULL OR size_max IS NULL OR size_min <= size_max",
            name="ck_requirement_size_order",
        ),
        CheckConstraint(
            "bedrooms_min IS NULL OR bedrooms_max IS NULL OR bedrooms_min <= bedrooms_max",
            name="ck_requirement_bedrooms_order",
        ),
        CheckConstraint(
            "bedrooms_min IS NULL OR bedrooms_min >= 0", name="ck_requirement_bedrooms_min"
        ),
        CheckConstraint(
            "bedrooms_max IS NULL OR bedrooms_max >= 0", name="ck_requirement_bedrooms_max"
        ),
        CheckConstraint(
            "timing_from IS NULL OR timing_to IS NULL OR timing_from <= timing_to",
            name="ck_requirement_timing_order",
        ),
        CheckConstraint(
            "currency IS NULL OR (length(currency) = 3 AND currency = upper(currency))",
            name="ck_requirement_currency",
        ),
        Index("ix_requirements_contact_status", "contact_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    transaction_type: Mapped[str] = mapped_column(String(8), nullable=False)
    property_type: Mapped[str | None] = mapped_column(String(64))
    locations: Mapped[list[dict[str, Any]]] = mapped_column(
        json_type, nullable=False, default=list, server_default="[]"
    )
    budget_min: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    budget_max: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    bedrooms_min: Mapped[int | None] = mapped_column(SmallInteger)
    bedrooms_max: Mapped[int | None] = mapped_column(SmallInteger)
    size_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    size_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    size_unit: Mapped[str | None] = mapped_column(String(24))
    timing_from: Mapped[date_type | None] = mapped_column(Date)
    timing_to: Mapped[date_type | None] = mapped_column(Date)
    timing_text: Mapped[str | None] = mapped_column(String(512))
    purpose: Mapped[str | None] = mapped_column(String(128))
    preferences: Mapped[dict[str, Any]] = mapped_column(
        json_type, nullable=False, default=dict, server_default="{}"
    )
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    match_readiness: Mapped[str] = mapped_column(String(24), nullable=False, default="incomplete")


class PortfolioAsset(TimestampMixin, Base):
    __tablename__ = "portfolio_assets"
    __table_args__ = (
        CheckConstraint("status IN ('current','sold','unknown')", name="ck_portfolio_status"),
        CheckConstraint("size IS NULL OR size >= 0", name="ck_portfolio_size"),
        CheckConstraint("value IS NULL OR value >= 0", name="ck_portfolio_value"),
        Index("ix_portfolio_contact_status", "contact_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    property_type: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    ownership_status: Mapped[str | None] = mapped_column(String(64))
    bedrooms: Mapped[int | None] = mapped_column(SmallInteger)
    size: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    size_unit: Mapped[str | None] = mapped_column(String(24))
    value: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    value_currency: Mapped[str | None] = mapped_column(String(3))
    acquisition_date: Mapped[date_type | None] = mapped_column(Date)
    portfolio_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")


class ContextFact(Base):
    __tablename__ = "context_facts"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('client_context','requirement','portfolio_asset')",
            name="ck_fact_entity_type",
        ),
        CheckConstraint(
            "source_type IN ('customer_message','admin','agent_extraction')",
            name="ck_fact_source_type",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_fact_confidence"
        ),
        Index("ix_facts_entity_field", "entity_type", "entity_id", "field_key", "superseded_at"),
        Index(
            "uq_facts_active_field",
            "entity_type",
            "entity_id",
            "field_key",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
            sqlite_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    field_key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[Any] = mapped_column(json_type, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (
        CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="ck_attachment_size"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    provider_media_id: Mapped[str | None] = mapped_column(String(256))
    storage_key: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(256))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    scan_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started','completed','handed_over','failed','cancelled')",
            name="ck_agent_run_status",
        ),
        Index("ix_agent_runs_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), nullable=False
    )
    trigger_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    response_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    skill_key: Mapped[str | None] = mapped_column(String(128))
    application_release: Mapped[str] = mapped_column(String(128), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(256))
    rollout_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="started")
    decision_type: Mapped[str | None] = mapped_column(String(64))
    handover_reason: Mapped[str | None] = mapped_column(String(128))
    known_fact_ids_json: Mapped[list[str]] = mapped_column(json_type, nullable=False, default=list)
    requested_field_keys_json: Mapped[list[str]] = mapped_column(
        json_type, nullable=False, default=list
    )
    decision_context_hash: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRunDraft(Base):
    __tablename__ = "agent_run_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_admin','sent','discarded','expired')", name="ck_draft_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    draft_payload_json: Mapped[dict[str, Any] | None] = mapped_column(json_type)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_admin")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeControl(Base):
    __tablename__ = "runtime_controls"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_runtime_singleton"),
        CheckConstraint(
            "ai_mode IN ('off','shadow','assisted','canary','active')", name="ck_runtime_ai_mode"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    ai_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="shadow")
    kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    changed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiRolloutAllowlist(Base):
    __tablename__ = "ai_rollout_allowlist"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contacts.id"), unique=True, nullable=False
    )
    enabled_by_admin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrmLink(TimestampMixin, Base):
    __tablename__ = "crm_links"
    __table_args__ = (
        CheckConstraint(
            "sync_status IN ('pending','synced','failed','conflict')", name="ck_crm_sync_status"
        ),
        UniqueConstraint("crm_system", "external_contact_id", name="uq_crm_external_contact"),
        UniqueConstraint("crm_system", "external_record_id", name="uq_crm_external_record"),
        Index(
            "uq_crm_contact_mapping",
            "contact_id",
            "crm_system",
            unique=True,
            postgresql_where=text("requirement_id IS NULL"),
            sqlite_where=text("requirement_id IS NULL"),
        ),
        Index(
            "uq_crm_requirement_mapping",
            "requirement_id",
            "crm_system",
            unique=True,
            postgresql_where=text("requirement_id IS NOT NULL"),
            sqlite_where=text("requirement_id IS NOT NULL"),
        ),
        Index(
            "uq_crm_portfolio_mapping",
            "portfolio_asset_id",
            "crm_system",
            unique=True,
            postgresql_where=text("portfolio_asset_id IS NOT NULL"),
            sqlite_where=text("portfolio_asset_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("property_requirements.id"))
    portfolio_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolio_assets.id"))
    crm_system: Mapped[str] = mapped_column(String(32), nullable=False, default="zoho")
    external_contact_id: Mapped[str | None] = mapped_column(String(128))
    external_record_id: Mapped[str | None] = mapped_column(String(128))
    sync_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class TranscriptExport(Base):
    __tablename__ = "transcript_exports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','ready','failed','expired')", name="ck_export_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    requested_by_admin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id"), nullable=False
    )
    filters_json: Mapped[dict[str, Any]] = mapped_column(json_type, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    deidentified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    row_count: Mapped[int | None] = mapped_column(Integer)
    artifact_uri: Mapped[str | None] = mapped_column(String(1024))
    checksum: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillReviewRun(Base):
    __tablename__ = "skill_review_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('scheduled','manual')", name="ck_skill_review_mode"),
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_skill_review_status",
        ),
        Index("ix_skill_review_queue", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    cohort_filters_json: Mapped[dict[str, Any]] = mapped_column(json_type, nullable=False)
    reviewer_skill_key: Mapped[str] = mapped_column(
        String(128), nullable=False, default="skill_reviewer"
    )
    application_release: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    conversation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_artifact_uri: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillImprovementProposal(TimestampMixin, Base):
    __tablename__ = "skill_improvement_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review','approved','rejected','implemented',"
            "'deployed','superseded')",
            name="ck_skill_proposal_status",
        ),
        UniqueConstraint(
            "skill_review_run_id",
            "affected_skill_key",
            "fingerprint",
            name="uq_skill_proposal_fingerprint",
        ),
        Index("ix_skill_proposal_queue", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    skill_review_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skill_review_runs.id"), nullable=False
    )
    affected_skill_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    issue_category: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    frequency_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_conversation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cohort_size: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(json_type, nullable=False)
    proposed_diff: Mapped[str] = mapped_column(Text, nullable=False)
    regression_tests_json: Mapped[list[dict[str, Any]] | None] = mapped_column(json_type)
    validation_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_review")
    reviewed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    code_change_ref: Mapped[str | None] = mapped_column(String(256))
    deployed_release: Mapped[str | None] = mapped_column(String(128))
