import asyncio
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.conversations.outbound import DuplicateOutboundRequest, enqueue_outbound_message
from app.db.base import Base
from app.db.models import (
    AgentRun,
    Contact,
    Conversation,
    HandoverCase,
    IntegrationEvent,
    Message,
    MessageSendAttempt,
)
from app.integrations.openwa.client import OpenWaError
from app.workers.outbound import OutboundWorker


class FakeOpenWa:
    def __init__(self, *, error: OpenWaError | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, chat_id: str, body: str) -> str:
        self.sent.append((chat_id, body))
        if self.error:
            raise self.error
        return "provider-message-id"


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def create_queued_message(
    session_factory: sessionmaker[Session], *, sender_type: str = "admin"
) -> tuple[uuid.UUID, uuid.UUID]:
    with session_factory.begin() as session:
        contact = Contact(
            provider_account_id="support",
            phone_e164="+919876543210",
            consent_status="unknown",
            retention_state="active",
        )
        session.add(contact)
        session.flush()
        if sender_type == "admin":
            from app.db.models import AdminUser

            admin_user = AdminUser(
                subject_id="oidc-subject",
                email="admin@example.test",
                role="admin",
            )
            session.add(admin_user)
            session.flush()
            sender_admin_id = admin_user.id
        else:
            sender_admin_id = None
        conversation = Conversation(
            contact_id=contact.id,
            channel="whatsapp",
            provider_account_id="support",
            provider_thread_id="919876543210@c.us",
            state="active_agent",
        )
        session.add(conversation)
        session.flush()
        agent_run_id = None
        if sender_type == "automated_agent":
            inbound = Message(
                conversation_id=conversation.id,
                provider_account_id="support",
                provider_message_id="trigger-message",
                direction="inbound",
                sender_type="customer",
                message_type="text",
                body_text="A customer question",
            )
            session.add(inbound)
            session.flush()
            agent_run = AgentRun(
                conversation_id=conversation.id,
                trigger_message_id=inbound.id,
                application_release="test",
                model_provider="fake",
                rollout_mode="active",
                known_fact_ids_json=[],
                requested_field_keys_json=[],
            )
            session.add(agent_run)
            session.flush()
            agent_run_id = agent_run.id
        message, _ = enqueue_outbound_message(
            session,
            conversation_id=conversation.id,
            sender_type=sender_type,
            sender_admin_id=sender_admin_id,
            agent_run_id=agent_run_id,
            body_text="Thanks, I will help with that.",
            idempotency_key="reply-key-1",
        )
        message_id = message.id
        conversation_id = conversation.id
    return message_id, conversation_id


def test_admin_outbound_message_is_sent_and_tracked(
    session_factory: sessionmaker[Session],
) -> None:
    message_id, _ = create_queued_message(session_factory)
    fake = FakeOpenWa()
    worker = OutboundWorker(session_factory, fake)

    assert asyncio.run(worker.process_one())

    assert fake.sent == [("919876543210@c.us", "Thanks, I will help with that.")]
    with session_factory() as session:
        message = session.get(Message, message_id)
        event = session.scalar(select(IntegrationEvent))
        attempt = session.scalar(select(MessageSendAttempt))
        assert message is not None and message.delivery_status == "accepted"
        assert message.provider_message_id == "provider-message-id"
        assert event is not None and event.status == "succeeded"
        assert attempt is not None and attempt.status == "accepted"


def test_ai_outbound_is_cancelled_after_admin_takeover(
    session_factory: sessionmaker[Session],
) -> None:
    message_id, conversation_id = create_queued_message(
        session_factory, sender_type="automated_agent"
    )
    with session_factory.begin() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.state = "admin_owned"
        session.add(
            HandoverCase(
                conversation_id=conversation_id,
                status="admin_owned",
                reason_code="customer_requests_human",
            )
        )
    fake = FakeOpenWa()
    worker = OutboundWorker(session_factory, fake)

    assert asyncio.run(worker.process_one())

    assert fake.sent == []
    with session_factory() as session:
        message = session.get(Message, message_id)
        event = session.scalar(select(IntegrationEvent))
        assert message is not None and message.delivery_status == "cancelled"
        assert event is not None and event.status == "dead_letter"
        assert event.last_error_code == "conversation_not_agent_owned"


def test_ambiguous_send_is_not_retried(
    session_factory: sessionmaker[Session],
) -> None:
    message_id, _ = create_queued_message(session_factory)
    fake = FakeOpenWa(error=OpenWaError("openwa_send_outcome_unknown", outcome="ambiguous"))
    worker = OutboundWorker(session_factory, fake)

    assert asyncio.run(worker.process_one())
    assert not asyncio.run(worker.process_one())

    with session_factory() as session:
        message = session.get(Message, message_id)
        event = session.scalar(select(IntegrationEvent))
        attempt = session.scalar(select(MessageSendAttempt))
        assert message is not None and message.delivery_status == "ambiguous"
        assert event is not None and event.status == "ambiguous"
        assert attempt is not None and attempt.status == "ambiguous"
    assert len(fake.sent) == 1


def test_opted_out_contact_blocks_even_admin_dispatch(
    session_factory: sessionmaker[Session],
) -> None:
    message_id, _ = create_queued_message(session_factory)
    with session_factory.begin() as session:
        contact = session.scalar(select(Contact))
        assert contact is not None
        contact.consent_status = "opted_out"
    fake = FakeOpenWa()
    worker = OutboundWorker(session_factory, fake)

    assert asyncio.run(worker.process_one())
    assert fake.sent == []
    with session_factory() as session:
        message = session.get(Message, message_id)
        event = session.scalar(select(IntegrationEvent))
        assert message is not None and message.delivery_status == "cancelled"
        assert event is not None and event.last_error_code == "contact_opted_out"


def test_outbound_idempotency_rejects_duplicate_message(
    session_factory: sessionmaker[Session],
) -> None:
    _, conversation_id = create_queued_message(session_factory)
    with session_factory.begin() as session:
        with pytest.raises(DuplicateOutboundRequest):
            enqueue_outbound_message(
                session,
                conversation_id=conversation_id,
                sender_type="admin",
                sender_admin_id=uuid.uuid4(),
                body_text="A duplicate reply",
                idempotency_key="reply-key-1",
            )
    with session_factory() as session:
        assert len(list(session.scalars(select(Message)))) == 1
