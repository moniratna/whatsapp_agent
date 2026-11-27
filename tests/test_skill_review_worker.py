from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.db.models import (
    Contact,
    Conversation,
    HandoverCase,
    SkillImprovementProposal,
    SkillReviewRun,
)
from app.workers.skill_reviews import SkillReviewWorker


def test_review_proposes_only_patterns_with_three_distinct_conversations() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory.begin() as session:
        run = SkillReviewRun(
            mode="manual",
            cohort_filters_json={
                "from_date": (now - timedelta(days=2)).isoformat(),
                "to_date": (now + timedelta(days=2)).isoformat(),
                "min_distinct_conversations": 3,
            },
            reviewer_skill_key="skill_reviewer",
            application_release="test",
            status="queued",
        )
        session.add(run)
        for index in range(3):
            contact = Contact(
                provider_account_id="test",
                phone_e164=f"+97150000000{index}",
                consent_status="unknown",
                retention_state="active",
            )
            session.add(contact)
            session.flush()
            conversation = Conversation(
                contact_id=contact.id,
                channel="whatsapp",
                provider_account_id="test",
                state="admin_owned",
            )
            session.add(conversation)
            session.flush()
            session.add(
                HandoverCase(
                    conversation_id=conversation.id,
                    reason_code="unknown_question",
                    status="resolved",
                )
            )
    worker = SkillReviewWorker(factory, Settings(app_env="test", database_url="sqlite://"))
    assert worker.process_one() is True
    with factory() as session:
        proposal = session.scalar(select(SkillImprovementProposal))
        loaded_run = session.scalar(select(SkillReviewRun))
        assert proposal is not None
        assert proposal.distinct_conversation_count == 3
        assert proposal.validation_status == "aggregate_only"
        assert "skills/fallback/SKILL.md" in proposal.proposed_diff
        assert loaded_run is not None
        assert loaded_run.status == "completed" and loaded_run.proposal_count == 1
    engine.dispose()
