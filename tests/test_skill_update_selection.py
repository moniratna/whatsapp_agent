from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.db.models import (
    AdminContextNote,
    AdminUser,
    Contact,
    Conversation,
    HandoverCase,
    Message,
    SkillCandidateFile,
    SkillUpdateRun,
)
from app.skill_updates.selection import (
    SelectedConversation,
    SelectionCursor,
    persist_selection_page,
    persist_snapshot_metadata,
    select_eligible_conversations,
    source_type_for_intent,
)
from app.skill_updates.snapshots import (
    ConversationSnapshotBuilder,
    SnapshotIneligible,
    delete_snapshot_artifact,
)
from app.skill_updates.storage import CandidateStorage

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
SKILLS = Path(__file__).parents[1] / "skills"


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_release="snapshot-test-release")


def add_conversation(
    session: Session,
    *,
    intent: str | None = "buy",
    consent: str = "opted_in",
    retention: str = "active",
    legal_hold: bool = False,
    state: str = "closed",
    closed_at: datetime = NOW - timedelta(days=2),
    with_customer_message: bool = True,
    identifier_text: str = "My name is Jane Doe. Call +971 50 123 4567 at jane@example.test.",
    conversation_id: uuid.UUID | None = None,
) -> tuple[Conversation, Contact]:
    contact = Contact(
        provider_account_id="test",
        phone_e164=f"+971500000{uuid.uuid4().int % 1000:03d}",
        consent_status=consent,
        retention_state=retention,
        legal_hold=legal_hold,
    )
    session.add(contact)
    session.flush()
    conversation = Conversation(
        id=conversation_id or uuid.uuid4(),
        contact_id=contact.id,
        channel="whatsapp",
        provider_account_id="test",
        state=state,
        active_intent=intent,
        closed_at=closed_at if state == "closed" else None,
    )
    session.add(conversation)
    session.flush()
    if with_customer_message:
        session.add(
            Message(
                conversation_id=conversation.id,
                provider_account_id="test",
                direction="inbound",
                sender_type="customer",
                message_type="text",
                body_text=identifier_text,
                created_at=closed_at - timedelta(minutes=2),
            )
        )
        session.flush()
    return conversation, contact


def test_source_intent_mapping_is_persisted_type_mapping() -> None:
    assert source_type_for_intent("buy") == "buyer"
    assert source_type_for_intent("sell") == "seller"
    assert source_type_for_intent("mixed") == "general"
    assert source_type_for_intent("portfolio") == "portfolio"
    assert source_type_for_intent("missing-value") == "unknown"
    assert source_type_for_intent(None) == "unknown"


def test_selection_filters_ineligible_conversations_and_pages_stably(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    base_closed = NOW - timedelta(days=4)
    ordered_ids = [uuid.UUID(int=value) for value in (10, 20, 30, 40)]
    with factory.begin() as session:
        for conversation_id in ordered_ids:
            add_conversation(session, closed_at=base_closed, conversation_id=conversation_id)
        add_conversation(session, retention="pending_deletion")
        add_conversation(session, retention="anonymized")
        add_conversation(session, legal_hold=True)
        add_conversation(session, state="admin_owned")
        add_conversation(session, closed_at=NOW - timedelta(days=100))
        add_conversation(session, with_customer_message=False)

        first = select_eligible_conversations(
            session, settings, now=NOW, limit=2
        )
        second = select_eligible_conversations(
            session,
            settings,
            now=NOW,
            cursor=first.cursor_out,
            window_start=first.window_start,
            window_end=first.window_end,
            limit=2,
        )
        retry_first = select_eligible_conversations(
            session, settings, now=NOW, limit=2
        )

    assert [item.conversation_id for item in first.conversations] == ordered_ids[:2]
    assert [item.conversation_id for item in second.conversations] == ordered_ids[2:]
    assert [item.conversation_id for item in retry_first.conversations] == ordered_ids[:2]
    assert first.has_more is True
    assert second.has_more is False
    assert [item.source_type for item in (*first.conversations, *second.conversations)] == [
        "buyer",
        "buyer",
        "buyer",
        "buyer",
    ]
    assert first.to_json()["policy_version"] == "closed-or-active-agent-v3"


def test_unknown_and_opted_out_contacts_are_not_filtered_from_skill_learning(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    unknown_id = uuid.UUID(int=50)
    opted_out_id = uuid.UUID(int=60)
    with factory.begin() as session:
        add_conversation(session, consent="unknown", conversation_id=unknown_id)
        add_conversation(session, consent="opted_out", conversation_id=opted_out_id)
        page = select_eligible_conversations(session, settings, now=NOW, limit=10)

    assert {item.conversation_id for item in page.conversations} == {
        unknown_id,
        opted_out_id,
    }


def test_active_agent_conversation_is_selected_and_snapshotted_as_of_run_time(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    with factory.begin() as session:
        conversation, _contact = add_conversation(
            session, state="active_agent", closed_at=NOW - timedelta(minutes=10)
        )
        conversation.created_at = NOW - timedelta(days=1)
        page = select_eligible_conversations(session, settings, now=NOW, limit=10)
        selected = next(
            item for item in page.conversations if item.conversation_id == conversation.id
        )
        builder = ConversationSnapshotBuilder(
            skill_directory=SKILLS,
            application_release=settings.app_release,
        )
        snapshot = builder.build(session, selected, now=NOW)

    assert selected.conversation_state == "active_agent"
    assert selected.snapshot_cutoff_at == NOW
    assert snapshot.conversation_state == "active_agent"
    assert snapshot.closed_at is None
    assert snapshot.snapshot_cutoff_at == NOW.isoformat()
    assert len(snapshot.messages) == 1


def test_selection_limit_is_bounded_by_configuration(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    with factory() as session, pytest.raises(ValueError, match="configured bounds"):
        select_eligible_conversations(session, settings, now=NOW, limit=501)


def test_selection_cursor_state_is_persisted_without_losing_snapshot_manifest(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    with factory.begin() as session:
        run = SkillUpdateRun(
            selection_json={"snapshot_artifacts": [{"snapshot_id": "prior"}]},
            application_release="test",
            reviewer_release="1",
        )
        session.add(run)
        session.flush()
        page = select_eligible_conversations(session, settings, now=NOW, limit=10)
        persist_selection_page(session, run.id, page)
        run_id = run.id

    with factory() as session:
        run = session.get(SkillUpdateRun, run_id)
        assert run is not None
        assert run.selection_json["policy_version"] == "closed-or-active-agent-v3"
        assert run.selection_json["snapshot_artifacts"][0]["snapshot_id"] == "prior"


def test_snapshot_is_deidentified_ordered_pinned_and_excludes_private_notes(
    factory: sessionmaker[Session], settings: Settings, tmp_path: Path
) -> None:
    with factory.begin() as session:
        conversation, _contact = add_conversation(
            session,
            identifier_text=(
                "My name is Jane Doe. Call +971 50 123 4567 or jane@example.test; "
                "I own villa 14 Palm Street."
            ),
        )
        customer_message = session.scalar(
            select(Message).where(Message.conversation_id == conversation.id)
        )
        assert customer_message is not None
        admin = AdminUser(
            subject_id="admin-1", email="admin@example.test", role="admin", is_active=True
        )
        session.add(admin)
        session.flush()
        case = HandoverCase(
            conversation_id=conversation.id,
            status="handed_back",
            reason_code="manual",
        )
        session.add(case)
        session.flush()
        session.add(
            AdminContextNote(
                conversation_id=conversation.id,
                handover_case_id=case.id,
                admin_id=admin.id,
                note_text="PRIVATE ADMIN NOTE",
                visibility="internal",
            )
        )
        session.add(
            Message(
                conversation_id=conversation.id,
                provider_account_id="test",
                direction="outbound",
                sender_type="admin",
                sender_admin_id=admin.id,
                message_type="text",
                body_text="Thanks, Jane. I can help clarify the next step.",
                created_at=conversation.closed_at - timedelta(minutes=1),
            )
        )
        session.flush()
        selected = SelectedConversation(
            conversation.id,
            conversation.closed_at,
            "buyer",
        )
        builder = ConversationSnapshotBuilder(
            skill_directory=SKILLS,
            application_release=settings.app_release,
        )
        snapshot = builder.build(session, selected, now=NOW)
        repeated = builder.build(session, selected, now=NOW + timedelta(minutes=1))
        conversation_id = conversation.id

    assert snapshot.snapshot_sha256 == repeated.snapshot_sha256
    assert snapshot.snapshot_id == repeated.snapshot_id
    assert snapshot.pins.application_release == "snapshot-test-release"
    assert snapshot.pins.reviewer_hash == snapshot.pins.skill_hashes["skill_reviewer"]
    assert [message.sender_type for message in snapshot.messages] == ["customer", "admin"]
    assert [message.created_at for message in snapshot.messages] == sorted(
        message.created_at for message in snapshot.messages
    )
    joined_text = " ".join(message.body_text or "" for message in snapshot.messages)
    assert "[NAME]" in joined_text
    assert "[PHONE]" in joined_text
    assert "[EMAIL]" in joined_text
    assert "[ADDRESS]" in joined_text
    assert "Jane Doe" not in joined_text
    assert "Palm Street" not in joined_text
    assert "PRIVATE ADMIN NOTE" not in joined_text
    assert str(conversation_id) not in " ".join(
        [snapshot.conversation_ref, *(message.message_ref for message in snapshot.messages)]
    )

    root = tmp_path / "storage"
    root.mkdir(mode=0o700)
    storage = CandidateStorage(root, allow_ephemeral_for_tests=True)
    artifact = builder.store(snapshot, storage)
    stored = storage.read_work_artifact(
        Path(artifact.relative_path).name, artifact.artifact_sha256
    ).decode("utf-8")
    assert "PRIVATE ADMIN NOTE" not in stored
    assert str(conversation_id) not in stored


def test_snapshot_allows_opted_out_but_rechecks_retention_before_building(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    with factory.begin() as session:
        conversation, contact = add_conversation(session)
        selected = SelectedConversation(conversation.id, conversation.closed_at, "buyer")
        contact.consent_status = "opted_out"
        builder = ConversationSnapshotBuilder(skill_directory=SKILLS, application_release="test")
        snapshot = builder.build(session, selected, now=NOW)
        assert snapshot.conversation_id == conversation.id
        contact.retention_state = "pending_deletion"
        with pytest.raises(SnapshotIneligible, match="retention"):
            builder.build(session, selected, now=NOW)


def test_selection_mapping_change_invalidates_snapshot(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    with factory.begin() as session:
        conversation, _contact = add_conversation(session, intent="buy")
        selected = SelectedConversation(conversation.id, conversation.closed_at, "buyer")
        conversation.active_intent = "sell"
        builder = ConversationSnapshotBuilder(skill_directory=SKILLS, application_release="test")
        with pytest.raises(SnapshotIneligible, match="intent changed"):
            builder.build(session, selected, now=NOW)


def test_snapshot_metadata_is_idempotent_and_expires_by_explicit_hash(
    factory: sessionmaker[Session], settings: Settings, tmp_path: Path
) -> None:
    with factory.begin() as session:
        conversation, _contact = add_conversation(session)
        selected = SelectedConversation(conversation.id, conversation.closed_at, "buyer")
        run = SkillUpdateRun(
            selection_json={}, application_release="test", reviewer_release="1"
        )
        session.add(run)
        session.flush()
        run_id = run.id
        builder = ConversationSnapshotBuilder(
            skill_directory=SKILLS,
            application_release="test",
            retention_days=7,
        )
        snapshot = builder.build(session, selected, now=NOW)

    root = tmp_path / "snapshot-storage"
    root.mkdir(mode=0o700)
    storage = CandidateStorage(root, allow_ephemeral_for_tests=True)
    record = builder.store(snapshot, storage).to_json()
    with factory.begin() as session:
        persist_snapshot_metadata(session, run_id, record)
        later = {**record, "expires_at": (NOW + timedelta(days=9)).isoformat()}
        persist_snapshot_metadata(session, run_id, later)
    with factory() as session:
        run = session.get(SkillUpdateRun, run_id)
        assert run is not None
        records = run.selection_json["snapshot_artifacts"]
        assert len(records) == 1
        assert records[0]["expires_at"] == record["expires_at"]
        assert records[0]["conversation_id"] == str(snapshot.conversation_id)
        assert run.conversation_count == 1

    assert delete_snapshot_artifact(storage, record, now=NOW) is False
    assert delete_snapshot_artifact(storage, record, now=NOW + timedelta(days=7)) is True
    assert delete_snapshot_artifact(storage, record, now=NOW + timedelta(days=8)) is True


def test_snapshot_builder_does_not_write_candidate_or_active_skill_files(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    with factory.begin() as session:
        conversation, _contact = add_conversation(session)
        selected = SelectedConversation(conversation.id, conversation.closed_at, "buyer")
        snapshot = ConversationSnapshotBuilder(
            skill_directory=SKILLS,
            application_release=settings.app_release,
        ).build(session, selected, now=NOW)
        assert snapshot.source_type == "buyer"
        assert session.scalar(select(SkillCandidateFile.id)) is None


def test_cursor_round_trip_and_invalid_cursor() -> None:
    cursor = SelectionCursor(NOW, uuid.uuid4())
    assert SelectionCursor.from_json(cursor.to_json()) == cursor
    with pytest.raises(ValueError, match="invalid skill update selection cursor"):
        SelectionCursor.from_json({"selected_at": "bad"})
