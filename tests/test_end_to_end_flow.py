import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.admin import router as admin_router
from app.api.routes.webhooks import router as webhook_router
from app.config.settings import Settings, get_settings
from app.db.base import Base
from app.db.models import (
    AgentRun,
    Conversation,
    HandoverCase,
    IntegrationEvent,
    Message,
    RuntimeControl,
)
from app.integrations.bedrock.provider import FakeLlm
from app.workers.conversation import ConversationWorker
from app.workers.outbound import OutboundWorker


class LocalTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, chat_id: str, body: str) -> str:
        self.sent.append((chat_id, body))
        return f"local-provider-{len(self.sent)}"


def _event(message_id: str, body: str, index: int) -> dict[str, object]:
    return {
        "webhookId": "test",
        "sessionId": "support",
        "event": "message.received",
        "idempotencyKey": f"delivery-{index}",
        "timestamp": 1_800_000_000_000 + index * 1000,
        "payload": {
            "message": {
                "id": message_id,
                "from": "919876543210@c.us",
                "body": body,
                "fromMe": False,
                "type": "chat",
                "t": 1_800_000_000 + index,
            }
        },
    }


def test_customer_handover_admin_reply_handback_and_resume(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(RuntimeControl(id=1, ai_mode="active", kill_switch=False, version=1))
    settings = Settings(
        app_env="test",
        database_url="sqlite://",
        openwa_enabled=True,
        openwa_webhook_secret=SecretStr("hook-secret"),
        ai_mode="active",
        ai_kill_switch=False,
    )
    application = FastAPI()
    application.state.engine = engine
    application.state.session_factory = factory
    application.dependency_overrides[get_settings] = lambda: settings
    application.include_router(webhook_router)
    application.include_router(admin_router)
    client = TestClient(application)
    local_transport = LocalTransport()
    conversation_worker = ConversationWorker(
        factory,
        FakeLlm(),
        settings,
        skill_directory=Path(__file__).parents[1] / "skills",
    )
    outbound_worker = OutboundWorker(factory, local_transport)
    webhook_headers = {"X-Webhook-Secret": "hook-secret"}
    admin_headers = {"X-Dev-User": "supervisor-1", "X-Dev-Role": "supervisor"}

    first = client.post(
        "/webhooks/open-wa",
        headers=webhook_headers,
        json=_event("inbound-1", "I want to buy an apartment", 1),
    )
    assert first.status_code == 202
    assert conversation_worker.process_one()
    assert asyncio.run(outbound_worker.process_one())
    assert len(local_transport.sent) == 1

    second = client.post(
        "/webhooks/open-wa",
        headers=webhook_headers,
        json=_event("inbound-2", "What is the legal value of my apartment?", 2),
    )
    assert second.status_code == 202
    assert conversation_worker.process_one()
    with factory() as session:
        conversation = session.scalar(select(Conversation))
        handover = session.scalar(select(HandoverCase))
        assert conversation is not None and conversation.state == "admin_owned"
        assert handover is not None and handover.status == "open"
        conversation_id = conversation.id
        handover_id = handover.id
        automated_count = len(
            list(session.scalars(select(Message).where(Message.sender_type == "automated_agent")))
        )
    assert automated_count == 1
    assert not asyncio.run(outbound_worker.process_one())

    admin_reply = client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers={**admin_headers, "Idempotency-Key": "admin-reply-1"},
        json={"body_text": "I have asked a specialist to follow up."},
    )
    assert admin_reply.status_code == 202
    assert asyncio.run(outbound_worker.process_one())

    handback = client.post(
        f"/api/handovers/{handover_id}/handback",
        headers=admin_headers,
        json={"summary": "Customer asked for a valuation specialist."},
    )
    assert handback.status_code == 200
    assert handback.json()["conversation_state"] == "active_agent"

    third = client.post(
        "/webhooks/open-wa",
        headers=webhook_headers,
        json=_event("inbound-3", "I want to buy another apartment", 3),
    )
    assert third.status_code == 202
    assert conversation_worker.process_one()
    assert asyncio.run(outbound_worker.process_one())
    assert len(local_transport.sent) == 3
    with factory() as session:
        run_count = len(list(session.scalars(select(AgentRun))))
        outbound_messages = list(
            session.scalars(
                select(Message).where(Message.direction == "outbound").order_by(Message.created_at)
            )
        )
        assert run_count == 3
        assert [message.sender_type for message in outbound_messages] == [
            "automated_agent",
            "admin",
            "automated_agent",
        ]
        processed_events = list(
            session.scalars(
                select(IntegrationEvent).where(
                    IntegrationEvent.integration == "app",
                    IntegrationEvent.event_type == "conversation.process_message",
                )
            )
        )
        assert len(processed_events) == 3
        assert all(event.status == "succeeded" for event in processed_events)
    client.close()
    engine.dispose()
