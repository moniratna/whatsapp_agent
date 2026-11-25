import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings, get_settings
from app.db.connection import make_engine, make_session_factory
from app.db.models import (
    AgentRun,
    AiRolloutAllowlist,
    Contact,
    Conversation,
    HandoverCase,
    IntegrationEvent,
    Message,
    MessageSendAttempt,
    RuntimeControl,
)
from app.integrations.openwa.client import FakeOpenWaClient, OpenWaClient, OpenWaError, OpenWaPort

logger = logging.getLogger("whatsapp_assistant.outbound")


class OutboundWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        openwa_client: OpenWaPort,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ) -> None:
        self.session_factory = session_factory
        self.openwa_client = openwa_client
        self.worker_id = worker_id or str(uuid.uuid4())
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def recover_expired_leases(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        recovered = 0
        with self.session_factory.begin() as session:
            expired = list(
                session.scalars(
                    select(IntegrationEvent)
                    .where(
                        IntegrationEvent.direction == "outbound",
                        IntegrationEvent.status == "processing",
                        IntegrationEvent.lease_expires_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for event in expired:
                message = self._message_for_event(session, event)
                attempt = None
                if message is not None:
                    attempt = session.scalar(
                        select(MessageSendAttempt)
                        .where(MessageSendAttempt.message_id == message.id)
                        .order_by(MessageSendAttempt.attempt_number.desc())
                        .limit(1)
                    )
                if attempt is not None and attempt.status == "started":
                    attempt.status = "ambiguous"
                    attempt.completed_at = now
                    event.status = "ambiguous"
                    if message is not None:
                        message.delivery_status = "ambiguous"
                    event.last_error_code = "worker_lost_during_send"
                    event.processed_at = now
                else:
                    event.status = "retry_wait"
                    event.next_attempt_at = now
                    event.last_error_code = "worker_lease_expired_before_send"
                event.lease_owner = None
                event.lease_expires_at = None
                recovered += 1
        return recovered

    def claim_one(self, now: datetime | None = None) -> uuid.UUID | None:
        now = now or datetime.now(UTC)
        waiting = ("pending", "retry_wait")
        with self.session_factory.begin() as session:
            candidates = list(
                session.scalars(
                    select(IntegrationEvent)
                    .where(
                        IntegrationEvent.integration == "openwa",
                        IntegrationEvent.direction == "outbound",
                        IntegrationEvent.status.in_(waiting),
                        or_(
                            IntegrationEvent.next_attempt_at.is_(None),
                            IntegrationEvent.next_attempt_at <= now,
                        ),
                    )
                    .order_by(IntegrationEvent.created_at, IntegrationEvent.id)
                    .limit(25)
                    .with_for_update(skip_locked=True)
                )
            )
            for event in candidates:
                if event.aggregate_id is not None:
                    earlier = session.scalar(
                        select(
                            exists().where(
                                IntegrationEvent.integration == event.integration,
                                IntegrationEvent.direction == "outbound",
                                IntegrationEvent.aggregate_id == event.aggregate_id,
                                IntegrationEvent.id != event.id,
                                IntegrationEvent.created_at < event.created_at,
                                IntegrationEvent.status.in_(
                                    ("pending", "processing", "retry_wait")
                                ),
                            )
                        )
                    )
                    if earlier:
                        continue
                event.status = "processing"
                event.attempt_count += 1
                event.lease_owner = self.worker_id
                event.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                event.next_attempt_at = None
                logger.info(
                    "outbound_event_claimed event_id=%s attempt=%s worker_id=%s",
                    event.id,
                    event.attempt_count,
                    self.worker_id,
                )
                return event.id
        return None

    @staticmethod
    def _message_for_event(session: Session, event: IntegrationEvent) -> Message | None:
        payload: Any = event.payload_json
        raw_id = payload.get("message_id") if isinstance(payload, dict) else None
        try:
            message_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            return None
        return session.get(Message, message_id)

    def _prepare_send(self, event_id: uuid.UUID) -> tuple[str, str, str, int] | None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            event = session.scalar(
                select(IntegrationEvent)
                .where(
                    IntegrationEvent.id == event_id,
                    IntegrationEvent.status == "processing",
                    IntegrationEvent.lease_owner == self.worker_id,
                )
                .with_for_update()
            )
            if event is None:
                return None
            message = self._message_for_event(session, event)
            if message is None:
                self._fail_without_send(event, "outbound_message_missing", now)
                return None
            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.id == message.conversation_id)
                .with_for_update()
            )
            if conversation is None:
                self._fail_without_send(event, "conversation_missing", now)
                message.delivery_status = "failed"
                return None
            contact = session.scalar(
                select(Contact).where(Contact.id == conversation.contact_id).with_for_update()
            )
            if contact is None:
                self._fail_without_send(event, "contact_missing", now)
                message.delivery_status = "failed"
                return None
            reason = self._dispatch_block_reason(session, message, conversation, contact, now)
            if reason:
                logger.warning(
                    "outbound_send_blocked event_id=%s message_id=%s conversation_id=%s reason=%s",
                    event.id,
                    message.id,
                    conversation.id,
                    reason,
                )
                event.status = "dead_letter"
                event.last_error_code = reason
                event.processed_at = now
                event.lease_owner = None
                event.lease_expires_at = None
                message.delivery_status = "cancelled"
                return None
            body = message.body_text
            if not body:
                self._fail_without_send(event, "outbound_body_missing", now)
                message.delivery_status = "failed"
                return None
            attempt = MessageSendAttempt(
                message_id=message.id,
                attempt_number=event.attempt_count,
                status="started",
                request_id=str(uuid.uuid4()),
                started_at=now,
            )
            session.add(attempt)
            chat_id = f"{contact.phone_e164.lstrip('+')}@c.us"
            logger.info(
                "outbound_send_started event_id=%s message_id=%s conversation_id=%s "
                "attempt=%s transport=%s",
                event.id,
                message.id,
                conversation.id,
                event.attempt_count,
                "live" if isinstance(self.openwa_client, OpenWaClient) else "fake",
            )
            return str(message.id), chat_id, conversation.provider_account_id, event.attempt_count

    @staticmethod
    def _dispatch_block_reason(
        session: Session,
        message: Message,
        conversation: Conversation,
        contact: Contact,
        now: datetime,
    ) -> str | None:
        if conversation.state == "closed":
            return "conversation_closed"
        if contact.consent_status == "opted_out":
            return "contact_opted_out"
        if message.sender_type != "automated_agent":
            return None
        controls = session.get(RuntimeControl, 1)
        if controls is None:
            return "runtime_controls_missing"
        if controls.kill_switch:
            return "ai_kill_switch_enabled"
        if controls.ai_mode not in {"active", "canary"}:
            return "ai_mode_blocks_automatic_send"
        if controls.ai_mode == "canary":
            allowlisted = session.scalar(
                select(AiRolloutAllowlist.id).where(
                    AiRolloutAllowlist.contact_id == contact.id,
                    or_(
                        AiRolloutAllowlist.expires_at.is_(None),
                        AiRolloutAllowlist.expires_at > now,
                    ),
                )
            )
            if allowlisted is None:
                return "contact_not_on_canary_allowlist"
        if message.message_type == "handover_ack":
            run = session.get(AgentRun, message.agent_run_id) if message.agent_run_id else None
            active_handover = session.scalar(
                select(HandoverCase.id).where(
                    HandoverCase.conversation_id == conversation.id,
                    HandoverCase.status.in_(("open", "assigned", "admin_owned")),
                )
            )
            if (
                conversation.state != "admin_owned"
                or active_handover is None
                or run is None
                or run.status != "handed_over"
                or run.decision_type != "handover"
                or run.response_message_id != message.id
            ):
                return "invalid_handover_acknowledgement"
            return None
        if conversation.state != "active_agent":
            return "conversation_not_agent_owned"
        active_handover = session.scalar(
            select(HandoverCase.id).where(
                HandoverCase.conversation_id == conversation.id,
                HandoverCase.status.in_(("open", "assigned", "admin_owned")),
            )
        )
        if active_handover is not None:
            return "active_handover"
        return None

    @staticmethod
    def _fail_without_send(event: IntegrationEvent, reason: str, now: datetime) -> None:
        logger.error("outbound_send_failed event_id=%s reason=%s", event.id, reason)
        event.status = "dead_letter"
        event.last_error_code = reason
        event.processed_at = now
        event.lease_owner = None
        event.lease_expires_at = None

    async def process_one(self) -> bool:
        self.recover_expired_leases()
        event_id = self.claim_one()
        if event_id is None:
            return False
        prepared = self._prepare_send(event_id)
        if prepared is None:
            return True
        message_id, chat_id, session_id, attempt_number = prepared
        message_body = ""
        message_type = "text"
        interactive_payload = None
        try:
            message_body, message_type, interactive_payload = self._load_content(
                uuid.UUID(message_id)
            )
            if message_type == "interactive_list":
                if not interactive_payload:
                    raise OpenWaError("outbound_interactive_payload_missing")
                provider_message_id = await self.openwa_client.send_interactive(
                    chat_id, interactive_payload
                )
            elif isinstance(self.openwa_client, OpenWaClient):
                provider_message_id = await self.openwa_client.send_text_in_session(
                    session_id,
                    chat_id,
                    message_body,
                    idempotency_key=message_id,
                )
            else:
                provider_message_id = await self.openwa_client.send_text(chat_id, message_body)
        except OpenWaError as exc:
            if (
                message_type == "interactive_list"
                and exc.outcome == "rejected"
                and not exc.retryable
            ):
                logger.warning(
                    "outbound_interactive_fallback event_id=%s message_id=%s code=%s",
                    event_id,
                    message_id,
                    exc.code,
                )
                try:
                    if isinstance(self.openwa_client, OpenWaClient):
                        provider_message_id = await self.openwa_client.send_text_in_session(
                            session_id,
                            chat_id,
                            message_body,
                            idempotency_key=message_id,
                        )
                    else:
                        provider_message_id = await self.openwa_client.send_text(
                            chat_id, message_body
                        )
                except OpenWaError as fallback_error:
                    exc = fallback_error
                else:
                    self._record_send_success(
                        event_id, uuid.UUID(message_id), attempt_number, provider_message_id
                    )
                    logger.info(
                        "outbound_text_fallback_accepted event_id=%s message_id=%s attempt=%s",
                        event_id,
                        message_id,
                        attempt_number,
                    )
                    return True
            self._record_send_error(event_id, uuid.UUID(message_id), attempt_number, exc)
            logger.error(
                "outbound_provider_error event_id=%s message_id=%s attempt=%s code=%s "
                "outcome=%s retryable=%s",
                event_id,
                message_id,
                attempt_number,
                exc.code,
                exc.outcome,
                exc.retryable,
            )
            return True
        self._record_send_success(
            event_id, uuid.UUID(message_id), attempt_number, provider_message_id
        )
        logger.info(
            "outbound_provider_accepted event_id=%s message_id=%s attempt=%s",
            event_id,
            message_id,
            attempt_number,
        )
        return True

    def _load_content(
        self, message_id: uuid.UUID
    ) -> tuple[str, str, dict[str, Any] | None]:
        with self.session_factory() as session:
            message = session.get(Message, message_id)
            if message is None or not message.body_text:
                raise OpenWaError("outbound_body_missing")
            return message.body_text, message.message_type, message.interactive_payload

    def _record_send_success(
        self,
        event_id: uuid.UUID,
        message_id: uuid.UUID,
        attempt_number: int,
        provider_message_id: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            event = session.get(IntegrationEvent, event_id)
            message = session.get(Message, message_id)
            attempt = session.scalar(
                select(MessageSendAttempt).where(
                    MessageSendAttempt.message_id == message_id,
                    MessageSendAttempt.attempt_number == attempt_number,
                )
            )
            if event is None or message is None or attempt is None:
                raise RuntimeError("outbound_result_records_missing")
            event.status = "succeeded"
            event.processed_at = now
            event.lease_owner = None
            event.lease_expires_at = None
            event.last_error_code = None
            message.provider_message_id = provider_message_id
            message.delivery_status = "accepted"
            conversation = session.get(Conversation, message.conversation_id)
            if conversation is not None:
                conversation.last_outbound_at = now
            attempt.status = "accepted"
            attempt.provider_message_id = provider_message_id
            attempt.completed_at = now

    def _record_send_error(
        self,
        event_id: uuid.UUID,
        message_id: uuid.UUID,
        attempt_number: int,
        error: OpenWaError,
    ) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            event = session.get(IntegrationEvent, event_id)
            message = session.get(Message, message_id)
            attempt = session.scalar(
                select(MessageSendAttempt).where(
                    MessageSendAttempt.message_id == message_id,
                    MessageSendAttempt.attempt_number == attempt_number,
                )
            )
            if event is None or message is None or attempt is None:
                raise RuntimeError("outbound_error_records_missing")
            event.last_error_code = error.code
            event.lease_owner = None
            event.lease_expires_at = None
            attempt.completed_at = now
            attempt.error_code = error.code
            if error.outcome == "ambiguous":
                event.status = "ambiguous"
                event.processed_at = now
                attempt.status = "ambiguous"
                message.delivery_status = "ambiguous"
            elif error.retryable and attempt_number < self.max_attempts:
                event.status = "retry_wait"
                delay = min(3600.0, 2**attempt_number) * random.uniform(0.8, 1.2)
                event.next_attempt_at = now + timedelta(seconds=delay)
                attempt.status = "failed"
                message.delivery_status = "failed"
            else:
                event.status = "dead_letter"
                event.processed_at = now
                attempt.status = "rejected" if not error.retryable else "failed"
                message.delivery_status = "failed"


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.openwa_transport_mode == "live":
        if not settings.openwa_enabled or not settings.openwa_api_key.get_secret_value():
            raise RuntimeError("live open-wa outbound worker is not configured")
        client: OpenWaPort = OpenWaClient(settings)
    elif settings.app_env in {"development", "test"}:
        client = FakeOpenWaClient()
    else:
        raise RuntimeError("fake open-wa transport is restricted to local development")
    logger.info(
        "outbound_worker_started app_env=%s transport=%s openwa_enabled=%s",
        settings.app_env,
        "live" if isinstance(client, OpenWaClient) else "fake",
        settings.openwa_enabled,
    )
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    worker = OutboundWorker(session_factory, client)
    try:
        while True:
            processed = await worker.process_one()
            if not processed:
                await asyncio.sleep(1.0)
    finally:
        if isinstance(client, OpenWaClient):
            await client.close()
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run_worker())
