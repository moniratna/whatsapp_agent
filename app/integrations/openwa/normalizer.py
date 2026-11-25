import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from app.integrations.openwa.schemas import NormalizedInboundMessage, OpenWaEnvelope, OpenWaMessage

_PHONE_ID = re.compile(r"^(?P<number>\+?[0-9]{5,15})@(?:c\.us|s\.whatsapp\.net)$")
_PHONE_NUMBER = re.compile(r"^\+?[0-9]{5,15}$")


def sender_address_kind(sender: str | None) -> str:
    """Describe the sender address format without exposing its phone or identifier."""
    if not sender:
        return "missing"
    if _PHONE_NUMBER.fullmatch(sender):
        return "bare_number"
    suffix = sender.rpartition("@")[2]
    if suffix in {"c.us", "s.whatsapp.net", "lid", "g.us", "broadcast"}:
        return suffix
    return "other"


class UnsupportedOpenWaEvent(ValueError):
    """The event is valid open-wa traffic but is not processed by this receiver."""


def stable_event_key(envelope: OpenWaEnvelope) -> str:
    if envelope.idempotency_key:
        source = f"{envelope.session_id}:{envelope.idempotency_key}"
        return hashlib.sha256(source.encode()).hexdigest()
    if envelope.event == "interactive.response":
        response = envelope.payload.get("response", {})
        response_id = response.get("messageId") if isinstance(response, dict) else None
        source = f"{envelope.session_id}:{envelope.event}:{response_id or envelope.timestamp}"
        return hashlib.sha256(source.encode()).hexdigest()
    message = envelope.payload.get("message", {})
    if envelope.event == "ack.changed":
        ctx = envelope.payload.get("ctx")
        message_id = ctx.get("id") if isinstance(ctx, dict) else None
        source = (
            f"{envelope.session_id}:{envelope.event}:{message_id}:{envelope.payload.get('ack')}"
        )
    else:
        message_id = message.get("id") if isinstance(message, dict) else None
        source = f"{envelope.session_id}:{envelope.event}:{message_id or envelope.timestamp}"
    return hashlib.sha256(source.encode()).hexdigest()


def provider_account_id(session_id: str) -> str:
    return session_id


def normalize_inbound(envelope: OpenWaEnvelope) -> NormalizedInboundMessage:
    if envelope.event == "interactive.response":
        response: Any = envelope.payload.get("response")
        if not isinstance(response, dict):
            raise ValueError("interactive.response is missing payload.response")
        chat_id = response.get("chatId")
        message_id = response.get("messageId")
        option_id = response.get("id")
        option_label = response.get("label")
        if not isinstance(chat_id, str) or not isinstance(message_id, str):
            raise ValueError("interactive response is missing chatId or messageId")
        phone_match = _PHONE_ID.fullmatch(chat_id)
        if phone_match is None:
            raise UnsupportedOpenWaEvent("interactive_response_not_direct_chat")
        phone = phone_match.group("number")
        body = option_label if isinstance(option_label, str) and option_label else option_id
        if not isinstance(body, str) or not body.strip():
            raise ValueError("interactive response is missing selected option")
        raw_timestamp = response.get("timestamp")
        timestamp_ms = (
            timestamp_to_milliseconds(raw_timestamp) if raw_timestamp is not None else None
        )
        return NormalizedInboundMessage(
            provider_account_id=provider_account_id(envelope.session_id),
            provider_message_id=message_id,
            sender_phone_e164=phone if phone.startswith("+") else f"+{phone}",
            sender_chat_id=chat_id,
            body_text=body,
            message_type="interactive_reply",
            provider_timestamp_ms=timestamp_ms,
            interactive_payload={
                key: response[key]
                for key in ("id", "label", "type", "originalMessageId")
                if response.get(key) is not None
            },
        )
    if envelope.event != "message.received":
        raise UnsupportedOpenWaEvent(envelope.event)
    raw_message: Any = envelope.payload.get("message")
    if not isinstance(raw_message, dict):
        raise ValueError("message.received is missing payload.message")
    message = OpenWaMessage.model_validate(raw_message)
    if message.from_me:
        raise UnsupportedOpenWaEvent("outbound_echo")
    if message.is_group or message.sender.endswith("@g.us"):
        raise UnsupportedOpenWaEvent("group_message")
    if not message.id:
        raise ValueError("message.received is missing payload.message.id")
    if not message.sender:
        raise ValueError("message.received is missing payload.message.from")
    phone_match = _PHONE_ID.fullmatch(message.sender)
    if phone_match:
        phone = phone_match.group("number")
    elif _PHONE_NUMBER.fullmatch(message.sender):
        phone = message.sender
    elif message.sender.endswith("@lid") and message.sender_phone:
        sender_phone_match = _PHONE_NUMBER.fullmatch(message.sender_phone)
        if sender_phone_match is None:
            raise UnsupportedOpenWaEvent("unmapped_private_sender")
        phone = sender_phone_match.group(0)
    else:
        raise UnsupportedOpenWaEvent("non_direct_chat")
    if not phone.startswith("+"):
        phone = f"+{phone}"
    timestamp_ms = int(message.timestamp * 1000) if message.timestamp is not None else None
    if timestamp_ms is None and envelope.timestamp is not None:
        timestamp_ms = timestamp_to_milliseconds(envelope.timestamp)
    if timestamp_ms is not None and timestamp_ms < 100_000_000_000:
        timestamp_ms *= 1000
    attachment = None
    if message.message_type and message.message_type.lower() not in {"chat", "text"}:
        attachment = {
            "mime_type": message.mimetype,
            "filename": message.filename,
            "scan_status": "unavailable",
        }
    return NormalizedInboundMessage(
        provider_account_id=provider_account_id(envelope.session_id),
        provider_message_id=message.id,
        sender_phone_e164=phone,
        sender_chat_id=message.sender,
        body_text=message.body or message.caption,
        message_type="text" if not message.message_type else message.message_type.lower(),
        provider_timestamp_ms=timestamp_ms,
        attachment=attachment,
        interactive_payload=None,
    )


def timestamp_to_milliseconds(value: int | float | str) -> int | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return None
    return int(value)


def timestamp_from_milliseconds(timestamp_ms: int | None) -> datetime | None:
    if timestamp_ms is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def minimal_event_payload(envelope: OpenWaEnvelope) -> dict[str, Any]:
    """Retain only fields needed for durable processing and safe reconciliation."""
    if envelope.event in {"message.received", "interactive.response"}:
        normalized = normalize_inbound(envelope)
        return {
            "provider_account_id": normalized.provider_account_id,
            "provider_message_id": normalized.provider_message_id,
        }
    if envelope.event == "ack.changed":
        payload = envelope.payload
        ctx_payload = payload.get("ctx")
        ctx: dict[str, Any] = ctx_payload if isinstance(ctx_payload, dict) else {}
        ack = payload.get("ack")
        return {
            "provider_account_id": provider_account_id(envelope.session_id),
            "provider_message_id": ctx.get("id") or ctx.get("serializedId"),
            "ack": ack,
        }
    return {"event": envelope.event, "session_id": envelope.session_id}
