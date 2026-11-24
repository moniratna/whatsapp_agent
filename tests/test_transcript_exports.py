from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.db.models import Contact, Conversation, Message, TranscriptExport
from app.workers.exports import TranscriptExportWorker


def test_export_is_private_deidentified_and_excludes_admin_notes(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        from app.db.models import AdminContextNote, AdminUser, HandoverCase

        admin = AdminUser(
            subject_id="admin-1", email="admin@example.test", role="admin", is_active=True
        )
        session.add(admin)
        contact = Contact(
            provider_account_id="test",
            phone_e164="+971500000000",
            consent_status="opted_in",
            retention_state="active",
        )
        session.add(contact)
        session.flush()
        conversation = Conversation(
            contact_id=contact.id,
            channel="whatsapp",
            provider_account_id="test",
            state="active_agent",
        )
        session.add(conversation)
        session.flush()
        handover = HandoverCase(
            conversation_id=conversation.id, reason_code="manual", status="admin_owned"
        )
        session.add(handover)
        session.add(
            Message(
                conversation_id=conversation.id,
                provider_account_id="test",
                direction="inbound",
                sender_type="customer",
                message_type="text",
                body_text=(
                    "My name is Jane Doe. Call me at +971 50 123 4567 or person@example.com. "
                    "I own villa 14 Palm Street."
                ),
            )
        )
        session.flush()
        session.add(
            AdminContextNote(
                conversation_id=conversation.id,
                handover_case_id=handover.id,
                admin_id=admin.id,
                note_text="PRIVATE NOTE",
            )
        )
        export = TranscriptExport(
            requested_by_admin_id=admin.id,
            filters_json={"conversation_ids": [str(conversation.id)]},
            status="queued",
            deidentified=True,
        )
        session.add(export)
        session.flush()
        export_id = export.id
    worker = TranscriptExportWorker(
        factory, Settings(app_env="test", exports_directory=str(tmp_path))
    )
    assert worker.process_one() is True
    with factory() as session:
        loaded_export = session.get(TranscriptExport, export_id)
        assert loaded_export is not None and loaded_export.status == "ready"
        artifact = Path(loaded_export.artifact_uri or "")
        assert artifact.parent == tmp_path
        assert artifact.stat().st_mode & 0o777 == 0o600
        content = artifact.read_text(encoding="utf-8")
        assert "[PHONE]" in content and "[EMAIL]" in content
        assert "[NAME]" in content and "[ADDRESS]" in content
        assert "Jane Doe" not in content and "Palm Street" not in content
        assert "PRIVATE NOTE" not in content
    engine.dispose()
