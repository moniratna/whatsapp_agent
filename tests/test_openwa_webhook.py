from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.webhooks import router
from app.config.settings import Settings, get_settings
from app.db.base import Base
from app.db.models import (
    Contact,
    Conversation,
    HandoverCase,
    IntegrationEvent,
    Message,
    MessageDeliveryEvent,
)


@pytest.fixture
def db_session_factory() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def client(db_session_factory: sessionmaker[Session]) -> TestClient:
    application = FastAPI()
    application.state.engine = db_session_factory.kw["bind"]
    application.state.session_factory = db_session_factory
    application.include_router(router)
    test_settings = Settings(
        app_env="test",
        database_url="sqlite://",
        openwa_enabled=True,
        openwa_webhook_secret=SecretStr("test-webhook-secret"),
    )
    application.dependency_overrides[get_settings] = lambda: test_settings
    return TestClient(application)


def sample_message(body_text: str = "Looking for a two bedroom apartment") -> dict[str, object]:
    return {
        "webhookId": "webhook-instance-id",
        "sessionId": "support",
        "event": "message.received",
        "idempotencyKey": "delivery-123",
        "timestamp": 1_800_000_000_000,
        "payload": {
            "message": {
                "id": "false_919876543210@c.us_abc123",
                "from": "919876543210@c.us",
                "body": body_text,
                "fromMe": False,
                "type": "chat",
                "t": 1_800_000_000,
            }
        },
    }


def test_authenticated_message_is_committed_before_ack(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/webhooks/open-wa",
        headers={"X-Webhook-Secret": "test-webhook-secret"},
        json=sample_message(),
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "duplicate": False}
    with db_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Contact)) == 1
        assert session.scalar(select(func.count()).select_from(Conversation)) == 1
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        message = session.scalar(select(Message))
        assert message is not None
        assert message.body_text == "Looking for a two bedroom apartment"
        assert message.sender_type == "customer"
        event = session.scalar(
            select(IntegrationEvent).where(IntegrationEvent.integration == "openwa")
        )
        assert event is not None
        assert event.status == "succeeded"
        processing_event = session.scalar(
            select(IntegrationEvent).where(IntegrationEvent.integration == "app")
        )
        assert processing_event is not None
        assert processing_event.event_type == "conversation.process_message"
        assert processing_event.status == "pending"


def test_duplicate_webhook_is_acknowledged_without_duplicate_message(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    headers = {"X-Webhook-Secret": "test-webhook-secret"}
    first = client.post("/webhooks/open-wa", headers=headers, json=sample_message())
    duplicate = client.post("/webhooks/open-wa", headers=headers, json=sample_message())

    assert first.status_code == duplicate.status_code == 202
    assert duplicate.json() == {"accepted": True, "duplicate": True}
    with db_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        assert session.scalar(select(func.count()).select_from(IntegrationEvent)) == 2


def test_bad_webhook_secret_is_rejected_before_persistence(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/webhooks/open-wa",
        headers={"X-Webhook-Secret": "wrong"},
        json=sample_message(),
    )

    assert response.status_code == 401
    with db_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IntegrationEvent)) == 0


def test_opt_out_is_applied_in_the_inbound_transaction(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    payload = sample_message("STOP")
    payload["idempotencyKey"] = "stop-delivery-1"
    message_container = payload["payload"]
    assert isinstance(message_container, dict)
    message = message_container["message"]
    assert isinstance(message, dict)
    message["id"] = "false_919876543210@c.us_stop-1"
    response = client.post(
        "/webhooks/open-wa",
        headers={"X-Webhook-Secret": "test-webhook-secret"},
        json=payload,
    )
    assert response.status_code == 202
    with db_session_factory() as session:
        contact = session.scalar(select(Contact))
        conversation = session.scalar(select(Conversation))
        handover = session.scalar(select(HandoverCase))
        assert contact is not None and contact.consent_status == "opted_out"
        assert conversation is not None and conversation.state == "admin_owned"
        assert handover is not None and handover.reason_code == "opt_out"


def test_acknowledgement_is_appended_and_updates_delivery_projection(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    headers = {"X-Webhook-Secret": "test-webhook-secret"}
    accepted = client.post("/webhooks/open-wa", headers=headers, json=sample_message())
    assert accepted.status_code == 202

    with db_session_factory() as session:
        message = session.scalar(select(Message))
        assert message is not None
        message.direction = "outbound"
        message.sender_type = "system"
        message.provider_message_id = "sent-provider-id"
        session.commit()

    event = {
        "sessionId": "support",
        "event": "ack.changed",
        "timestamp": 1_800_000_100_000,
        "payload": {"ctx": {"id": "sent-provider-id"}, "ack": 3},
    }
    response = client.post("/webhooks/open-wa", headers=headers, json=event)

    assert response.status_code == 202
    with db_session_factory() as session:
        message = session.scalar(select(Message))
        delivery = session.scalar(select(MessageDeliveryEvent))
        assert message is not None
        assert delivery is not None
        assert message.delivery_status == "read"
        assert delivery.status == "read"
