import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import (
    Contact,
    Conversation,
    HandoverCase,
    IntegrationEvent,
    Message,
    MessageAttachment,
    MessageDeliveryEvent,
)
from app.integrations.openwa.normalizer import (
    UnsupportedOpenWaEvent,
    minimal_event_payload,
    normalize_inbound,
    provider_account_id,
    sender_address_kind,
    stable_event_key,
    timestamp_from_milliseconds,
    timestamp_to_milliseconds,
)
from app.integrations.openwa.schemas import OpenWaEnvelope

logger = logging.getLogger("whatsapp_assistant.openwa_webhook")


def _insert_deduplicated(session: Session, values: dict[str, Any]) -> Any:
    bind = session.get_bind()
    statement: Any = (pg_insert if bind.dialect.name == "postgresql" else sqlite_insert)(
        IntegrationEvent
    ).values(**values)
    statement = statement.on_conflict_do_nothing(
        index_elements=["integration", "external_event_id"],
        index_where=(
            (IntegrationEvent.direction == "inbound")
            & IntegrationEvent.external_event_id.is_not(None)
        ),
    ).returning(IntegrationEvent.id)
    return session.execute(statement).scalar_one_or_none()


def _upsert_contact(session: Session, provider_id: str, phone: str) -> Contact:
    bind = session.get_bind()
    statement = (pg_insert if bind.dialect.name == "postgresql" else sqlite_insert)(Contact).values(
        provider_account_id=provider_id,
        phone_e164=phone,
        consent_status="unknown",
        retention_state="active",
    )
    statement = statement.on_conflict_do_nothing(
        index_elements=["provider_account_id", "phone_e164"]
    )
    session.execute(statement)
    contact = session.scalar(
        select(Contact)
        .where(Contact.provider_account_id == provider_id, Contact.phone_e164 == phone)
        .with_for_update()
    )
    if contact is None:
        raise RuntimeError("contact_upsert_failed")
    return contact


def _find_or_create_conversation(
    session: Session, contact: Contact, thread_id: str
) -> Conversation:
    conversation = session.scalar(
        select(Conversation)
        .where(Conversation.contact_id == contact.id, Conversation.state != "closed")
        .order_by(Conversation.updated_at.desc())
        .limit(1)
        .with_for_update()
    )
    if conversation is None:
        conversation = Conversation(
            contact_id=contact.id,
            channel="whatsapp",
            provider_account_id=contact.provider_account_id,
            provider_thread_id=thread_id,
            state="active_agent",
        )
        session.add(conversation)
        session.flush()
    return conversation


def _store_delivery_event(
    session: Session, event: IntegrationEvent, envelope: OpenWaEnvelope
) -> None:
    payload = event.payload_json
    provider_message_id = payload.get("provider_message_id")
    if not provider_message_id:
        event.status = "dead_letter"
        event.last_error_code = "ack_missing_message_id"
        return
    message = session.scalar(
        select(Message).where(
            Message.provider_account_id == provider_account_id(envelope.session_id),
            Message.provider_message_id == provider_message_id,
        )
    )
    if message is None:
        event.status = "retry_wait"
        event.last_error_code = "ack_message_not_found"
        return
    ack = payload.get("ack")
    ack_code = int(ack) if isinstance(ack, (int, float)) else None
    ack_statuses = {
        -1: "error",
        0: "pending",
        1: "server",
        2: "device",
        3: "read",
        4: "played",
    }
    status = ack_statuses.get(ack_code, "unknown") if ack_code is not None else "unknown"
    session.add(
        MessageDeliveryEvent(
            message_id=message.id,
            provider_account_id=provider_account_id(envelope.session_id),
            provider_event_id=event.external_event_id or str(event.id),
            status=status,
            occurred_at=timestamp_from_milliseconds(
                timestamp_to_milliseconds(envelope.timestamp)
                if envelope.timestamp is not None
                else None
            )
            or datetime.now(UTC),
            metadata_json={"ack": ack} if isinstance(ack, (str, int, float, bool)) else {},
        )
    )
    message.delivery_status = status
    event.status = "succeeded"


def accept_openwa_event(session: Session, envelope: OpenWaEnvelope) -> bool:
    """Persist event idempotently, and persist supported inbound messages atomically."""
    event_key = stable_event_key(envelope)
    try:
        payload = minimal_event_payload(envelope)
        event_status = "pending"
        error_code = None
    except UnsupportedOpenWaEvent as exc:
        raw_message = envelope.payload.get("message")
        sender = raw_message.get("from") if isinstance(raw_message, dict) else None
        logger.info(
            "openwa_event_not_processed event=%s session_id=%s reason=%s sender_kind=%s",
            envelope.event,
            envelope.session_id,
            str(exc),
            sender_address_kind(sender if isinstance(sender, str) else None),
        )
        if envelope.event == "message.received" and isinstance(raw_message, dict):
            payload = {
                "event": envelope.event,
                "session_id": envelope.session_id,
                "message": {
                    key: raw_message.get(key)
                    for key in ("id", "from", "body", "caption", "type", "fromMe")
                    if raw_message.get(key) is not None
                },
            }
            event_status = "dead_letter"
        else:
            payload = {"event": envelope.event, "session_id": envelope.session_id}
            event_status = "succeeded"
        error_code = str(exc)
    values: dict[str, Any] = {
        "integration": "openwa",
        "direction": "inbound",
        "event_type": envelope.event,
        "external_event_id": event_key,
        "aggregate_type": (
            "conversation"
            if envelope.event in {"message.received", "interactive.response"}
            else None
        ),
        "payload_json": payload,
        "status": event_status,
        "last_error_code": error_code,
    }
    event_id = _insert_deduplicated(session, values)
    if event_id is None:
        session.rollback()
        return False
    event = session.get(IntegrationEvent, event_id)
    if event is None:
        raise RuntimeError("integration_event_insert_failed")
    if event_status == "succeeded":
        session.commit()
        return True

    if envelope.event == "ack.changed":
        _store_delivery_event(session, event, envelope)
        session.commit()
        return True

    try:
        normalized = normalize_inbound(envelope)
    except ValueError as exc:
        event.status = "dead_letter"
        event.last_error_code = "invalid_message_payload"
        logger.warning(
            "openwa_message_invalid event_id=%s session_id=%s reason=%s",
            event.id,
            envelope.session_id,
            str(exc),
        )
        session.commit()
        return True

    interactive_payload = normalized.interactive_payload
    if interactive_payload is not None:
        original_message_id = interactive_payload.get("originalMessageId")
        original_message = None
        if isinstance(original_message_id, str):
            original_message = session.scalar(
                select(Message).where(
                    Message.provider_account_id == normalized.provider_account_id,
                    Message.provider_message_id == original_message_id,
                    Message.direction == "outbound",
                    Message.message_type == "interactive_list",
                )
            )
        if original_message is None:
            event.status = "dead_letter"
            event.last_error_code = "interactive_message_reference_missing"
            session.commit()
            logger.warning(
                "openwa_interactive_response_unmatched event_id=%s session_id=%s",
                event.id,
                envelope.session_id,
            )
            return True
        content = original_message.interactive_payload or {}
        selected_id = interactive_payload.get("id")
        rows = [
            row
            for section in content.get("sections", [])
            if isinstance(section, dict)
            for row in section.get("rows", [])
            if isinstance(row, dict)
        ]
        selected = next(
            (row for row in rows if row.get("id") == selected_id),
            None,
        )
        if selected is None:
            event.status = "dead_letter"
            event.last_error_code = "interactive_option_unknown"
            session.commit()
            logger.warning(
                "openwa_interactive_option_unknown event_id=%s session_id=%s",
                event.id,
                envelope.session_id,
            )
            return True
        normalized.body_text = str(selected.get("title", selected_id))

    event.aggregate_type = "message"
    prior_message = session.scalar(
        select(Message).where(
            Message.provider_account_id == normalized.provider_account_id,
            Message.provider_message_id == normalized.provider_message_id,
        )
    )
    if prior_message is not None:
        event.status = "succeeded"
        session.commit()
        logger.info(
            "openwa_message_duplicate event_id=%s session_id=%s",
            event.id,
            envelope.session_id,
        )
        return True

    contact = _upsert_contact(session, normalized.provider_account_id, normalized.sender_phone_e164)
    conversation = _find_or_create_conversation(session, contact, normalized.sender_chat_id)
    timestamp = timestamp_from_milliseconds(normalized.provider_timestamp_ms)
    message = Message(
        conversation_id=conversation.id,
        provider_account_id=normalized.provider_account_id,
        provider_message_id=normalized.provider_message_id,
        direction="inbound",
        sender_type="customer",
        message_type=normalized.message_type,
        body_text=normalized.body_text,
        interactive_payload=normalized.interactive_payload,
        provider_timestamp=timestamp,
    )
    session.add(message)
    session.flush()
    # Queue application processing in the same commit as the durable message.
    # The processor uses a separate integration namespace from the WA send outbox.
    session.add(
        IntegrationEvent(
            integration="app",
            direction="outbound",
            event_type="conversation.process_message",
            external_event_id=f"process:{message.id}",
            aggregate_type="conversation",
            aggregate_id=conversation.id,
            payload_json={"message_id": str(message.id)},
            status="pending",
        )
    )
    if normalized.attachment:
        session.add(
            MessageAttachment(
                message_id=message.id,
                provider_media_id=normalized.attachment.get("provider_media_id"),
                mime_type=normalized.attachment.get("mime_type") or "application/octet-stream",
                filename=normalized.attachment.get("filename"),
                scan_status="unavailable",
            )
        )
    conversation.last_inbound_at = timestamp or message.created_at
    if (normalized.body_text or "").strip().casefold() in {
        "stop",
        "stop all",
        "unsubscribe",
        "cancel",
        "end",
        "quit",
    }:
        contact.consent_status = "opted_out"
        contact.consent_updated_at = timestamp or datetime.now(UTC)
        if conversation.state != "closed":
            conversation.state = "admin_owned"
            conversation.version += 1
            active_case = session.scalar(
                select(HandoverCase)
                .where(
                    HandoverCase.conversation_id == conversation.id,
                    HandoverCase.status.in_(("open", "assigned", "admin_owned")),
                )
                .with_for_update()
            )
            if active_case is None:
                session.add(
                    HandoverCase(
                        conversation_id=conversation.id,
                        status="open",
                        reason_code="opt_out",
                        trigger_message_id=message.id,
                    )
                )
    event.aggregate_type = "conversation"
    event.aggregate_id = conversation.id
    event.status = "succeeded"
    session.commit()
    logger.info(
        "openwa_message_stored event_id=%s conversation_id=%s contact_id=%s message_id=%s",
        event.id,
        conversation.id,
        contact.id,
        message.id,
    )
    return True
