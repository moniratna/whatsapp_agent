from collections.abc import Generator
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.admin import router
from app.config.settings import Settings, get_settings
from app.db.base import Base
from app.db.models import (
    AdminContextNote,
    AdminUser,
    AuditEvent,
    Contact,
    ContextFact,
    Conversation,
    CrmLink,
    HandoverCase,
    IntegrationEvent,
    Message,
    PropertyRequirement,
    RuntimeControl,
)


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


@pytest.fixture
def admin_client(session_factory: sessionmaker[Session]) -> TestClient:
    application = FastAPI()
    application.state.session_factory = session_factory
    application.include_router(router)
    settings = Settings(app_env="test", database_url="sqlite://")
    application.dependency_overrides[get_settings] = lambda: settings
    return TestClient(application)


def make_conversation(session_factory: sessionmaker[Session]) -> tuple[UUID, UUID]:
    with session_factory.begin() as session:
        contact = Contact(
            provider_account_id="support",
            phone_e164="+919811223344",
            consent_status="opted_in",
            retention_state="active",
        )
        session.add(contact)
        session.flush()
        conversation = Conversation(
            contact_id=contact.id,
            channel="whatsapp",
            provider_account_id="support",
            provider_thread_id="919811223344@c.us",
        )
        session.add(conversation)
        session.flush()
        return contact.id, conversation.id


def admin_headers(role: str = "admin") -> dict[str, str]:
    return {"X-Dev-User": "alice", "X-Dev-Role": role}


def test_skill_update_operations_endpoint_exposes_sanitized_queue_metrics(
    admin_client: TestClient,
) -> None:
    response = admin_client.get("/api/operations/skill-updates", headers=admin_headers("read_only"))

    assert response.status_code == 200
    body = response.json()
    assert body["flags"]["skill_updates_enabled"] is False
    assert body["flags"]["reviewer_updates_enabled"] is False
    assert body["metrics"]["batches_by_status"] == {}
    assert body["metrics"]["candidates_by_type_and_status"] == {}
    assert body["recent_batch_attention"] == []
    assert body["recent_reviewer_attention"] == []


def test_read_only_user_cannot_send_or_take_over(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _contact_id, conversation_id = make_conversation(session_factory)
    response = admin_client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers={**admin_headers("read_only"), "Idempotency-Key": "readonly-reply"},
        json={"body_text": "Should not send"},
    )
    assert response.status_code == 403
    with session_factory() as session:
        assert session.scalar(select(Message)) is None


def test_admin_reply_is_saved_and_takeover_pauses_automation(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _contact_id, conversation_id = make_conversation(session_factory)
    response = admin_client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers={**admin_headers(), "Idempotency-Key": "reply-1"},
        json={"body_text": "I will ask the team and get back to you."},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    with session_factory() as session:
        conversation = session.get(Conversation, conversation_id)
        message = session.scalar(select(Message))
        case = session.scalar(select(HandoverCase))
        user = session.scalar(select(AdminUser))
        event = session.scalar(select(IntegrationEvent))
        assert conversation is not None and conversation.state == "admin_owned"
        assert message is not None and message.sender_type == "admin"
        assert user is not None and message.sender_admin_id == user.id
        assert case is not None and case.status == "admin_owned"
        assert event is not None and event.direction == "outbound"

    duplicate = admin_client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers={**admin_headers(), "Idempotency-Key": "reply-1"},
        json={"body_text": "I will ask the team and get back to you."},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    with session_factory() as session:
        assert len(list(session.scalars(select(Message)))) == 1


def test_private_note_is_separate_and_explicit_handback_resumes_agent(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _contact_id, conversation_id = make_conversation(session_factory)
    takeover = admin_client.post(
        f"/api/conversations/{conversation_id}/handovers",
        headers=admin_headers(),
        json={"reason_code": "manual", "take_over": True},
    )
    assert takeover.status_code == 201
    handover_id = takeover.json()["id"]
    note_text = "Private note: ask about the preferred viewing day."
    note_response = admin_client.post(
        f"/api/handovers/{handover_id}/context-notes",
        headers=admin_headers(),
        json={"note_text": note_text},
    )
    assert note_response.status_code == 201

    transcript = admin_client.get(f"/api/conversations/{conversation_id}", headers=admin_headers())
    assert transcript.status_code == 200
    assert note_text not in transcript.text
    notes = admin_client.get(f"/api/handovers/{handover_id}/context-notes", headers=admin_headers())
    assert notes.status_code == 200
    assert notes.json()["notes"][0]["note_text"] == note_text

    handback = admin_client.post(
        f"/api/handovers/{handover_id}/handback",
        headers=admin_headers(),
        json={"summary": "Human checked the requirement; resume intake."},
    )
    assert handback.status_code == 200
    with session_factory() as session:
        conversation = session.get(Conversation, conversation_id)
        case = session.get(HandoverCase, UUID(handover_id))
        assert conversation is not None and conversation.state == "active_agent"
        assert case is not None and case.status == "handed_back"
        assert len(list(session.scalars(select(AdminContextNote)))) == 2


def test_context_correction_records_admin_provenance(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    contact_id, _conversation_id = make_conversation(session_factory)
    response = admin_client.patch(
        f"/api/contacts/{contact_id}/context",
        headers=admin_headers(),
        json={"purpose_notes": "Customer is exploring investment options."},
    )
    assert response.status_code == 200
    with session_factory() as session:
        fact = session.scalar(select(ContextFact))
        audit = session.scalar(select(AuditEvent))
        assert fact is not None and fact.source_type == "admin"
        assert fact.created_by_admin_id is not None
        assert fact.value_json == "Customer is exploring investment options."
        assert audit is not None and audit.action == "contact.context_corrected"


def test_requirement_capture_and_correction_preserve_field_provenance(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    contact_id, _conversation_id = make_conversation(session_factory)
    created = admin_client.post(
        f"/api/contacts/{contact_id}/requirements",
        headers=admin_headers(),
        json={
            "transaction_type": "buy",
            "property_type": "apartment",
            "locations": [{"label": "Dubai Marina", "source_text": "Dubai Marina"}],
            "budget_max": "2000000.00",
            "currency": "AED",
            "bedrooms_min": 2,
        },
    )
    assert created.status_code == 201
    requirement_id = created.json()["id"]

    updated = admin_client.patch(
        f"/api/requirements/{requirement_id}",
        headers=admin_headers(),
        json={"budget_max": "2250000.00"},
    )
    assert updated.status_code == 200
    with session_factory() as session:
        requirement = session.get(PropertyRequirement, UUID(requirement_id))
        budget_facts = list(
            session.scalars(
                select(ContextFact).where(
                    ContextFact.entity_type == "requirement",
                    ContextFact.entity_id == UUID(requirement_id),
                    ContextFact.field_key == "budget_max",
                )
            )
        )
        assert requirement is not None and str(requirement.budget_max) == "2250000.00"
        assert len(budget_facts) == 2
        assert sum(fact.superseded_at is None for fact in budget_facts) == 1


def test_rollout_controls_are_supervisor_only_and_version_checked(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as session:
        session.add(RuntimeControl(id=1, ai_mode="shadow", kill_switch=True, version=1))
    forbidden = admin_client.patch(
        "/api/operations/runtime-controls",
        headers=admin_headers("admin"),
        json={
            "ai_mode": "active",
            "kill_switch": False,
            "reason": "reviewed",
            "expected_version": 1,
        },
    )
    assert forbidden.status_code == 403

    changed = admin_client.patch(
        "/api/operations/runtime-controls",
        headers=admin_headers("supervisor"),
        json={
            "ai_mode": "canary",
            "kill_switch": False,
            "reason": "Pilot review",
            "expected_version": 1,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    stale = admin_client.patch(
        "/api/operations/runtime-controls",
        headers=admin_headers("supervisor"),
        json={
            "ai_mode": "active",
            "kill_switch": False,
            "reason": "stale update",
            "expected_version": 1,
        },
    )
    assert stale.status_code == 409


def test_crm_retry_is_idempotent_and_visible(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    contact_id, _conversation_id = make_conversation(session_factory)
    headers = {**admin_headers(), "Idempotency-Key": "crm-retry-1"}
    first = admin_client.post(f"/api/operations/crm-sync/{contact_id}/retry", headers=headers)
    repeated = admin_client.post(f"/api/operations/crm-sync/{contact_id}/retry", headers=headers)
    status_response = admin_client.get(
        f"/api/contacts/{contact_id}/crm-status", headers=admin_headers()
    )
    assert first.status_code == repeated.status_code == 202
    assert repeated.json()["duplicate"] is True
    assert status_response.status_code == 200
    assert status_response.json()["links"][0]["sync_status"] == "pending"
    with session_factory() as session:
        assert len(list(session.scalars(select(IntegrationEvent)))) == 1
        assert session.scalar(select(CrmLink)) is not None
