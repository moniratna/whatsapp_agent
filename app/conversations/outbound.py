import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import Conversation, IntegrationEvent, Message


class DuplicateOutboundRequest(ValueError):
    pass


def enqueue_outbound_message(
    session: Session,
    *,
    conversation_id: uuid.UUID,
    sender_type: str,
    body_text: str,
    idempotency_key: str,
    sender_admin_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
    skill_key: str | None = None,
    application_release: str | None = None,
) -> tuple[Message, bool]:
    """Persist a message and its outbox row in the caller's transaction."""
    if sender_type not in {"admin", "automated_agent"}:
        raise ValueError("outbound messages must be admin or automated_agent messages")
    if not body_text.strip():
        raise ValueError("message body cannot be blank")
    if sender_type == "admin" and sender_admin_id is None:
        raise ValueError("admin messages require sender_admin_id")
    if sender_type == "automated_agent" and agent_run_id is None:
        raise ValueError("automated messages require agent_run_id")
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise LookupError("conversation_not_found")
    bind = session.get_bind()
    insert_fn = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
    message_id = uuid.uuid4()
    event_stmt: Any = insert_fn(IntegrationEvent).values(
        integration="openwa",
        direction="outbound",
        event_type="whatsapp.send_text",
        external_event_id=idempotency_key,
        aggregate_type="conversation",
        aggregate_id=conversation_id,
        payload_json={"message_id": str(message_id)},
        status="pending",
    )
    inserted_id = session.execute(
        event_stmt.on_conflict_do_nothing(
            index_elements=["integration", "external_event_id"],
            index_where=(
                (IntegrationEvent.direction == "outbound")
                & IntegrationEvent.external_event_id.is_not(None)
            ),
        ).returning(IntegrationEvent.id)
    ).scalar_one_or_none()
    if inserted_id is None:
        raise DuplicateOutboundRequest(idempotency_key)
    message = Message(
        id=message_id,
        conversation_id=conversation_id,
        provider_account_id=conversation.provider_account_id,
        direction="outbound",
        sender_type=sender_type,
        sender_admin_id=sender_admin_id,
        agent_run_id=agent_run_id,
        message_type="text",
        body_text=body_text,
        delivery_status="queued",
        skill_key=skill_key,
        application_release=application_release,
    )
    session.add(message)
    session.flush()
    event = session.get(IntegrationEvent, inserted_id)
    if event is None:
        raise RuntimeError("outbound_event_insert_failed")
    return message, True


def get_outbound_by_idempotency_key(
    session: Session, idempotency_key: str
) -> tuple[Message | None, IntegrationEvent | None]:
    event = session.scalar(
        select(IntegrationEvent).where(
            IntegrationEvent.integration == "openwa",
            IntegrationEvent.direction == "outbound",
            IntegrationEvent.external_event_id == idempotency_key,
        )
    )
    if event is None or event.payload_json.get("message_id") is None:
        return None, event
    try:
        message_id = uuid.UUID(str(event.payload_json["message_id"]))
    except ValueError:
        return None, event
    return session.get(Message, message_id), event
