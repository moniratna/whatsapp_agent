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
from app.db.models import Contact, Conversation, Message, SkillCandidateFile, SkillUpdateRun
from app.skill_updates.extraction import SkillUpdateExtractor
from app.skill_updates.reviewer import FakeLearningReviewer
from app.skill_updates.schemas import LearningReviewOutput
from app.skill_updates.selection import SelectedConversation
from app.skill_updates.snapshots import ConversationSnapshotBuilder
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


def make_output(**changes: object) -> LearningReviewOutput:
    data: dict[str, object] = {
        "status": "learning",
        "lesson": "Confirm the customer's preferred viewing window before suggesting times.",
        "source_conversation_type": "buyer",
        "trigger": "When the customer asks to arrange a viewing",
        "instruction": (
            "Ask which day and time window works before offering available appointment slots."
        ),
        "evidence_refs": ["ev_example_001"],
        "confidence": 0.94,
        "duplicate_or_novel": "novel",
        "policy_conflicts": [],
        "regression_cases": [
            {
                "case": "The customer asks to view a property but gives no preferred time.",
                "expected_behavior": (
                    "Ask for a preferred day and time window before suggesting slots."
                ),
            }
        ],
    }
    data.update(changes)
    return LearningReviewOutput.model_validate(data)


def prepare(
    factory: sessionmaker[Session], tmp_path: Path, output: object
) -> tuple[SkillUpdateExtractor, object, uuid.UUID, Path, FakeLearningReviewer]:
    with factory.begin() as session:
        contact = Contact(
            provider_account_id="test",
            phone_e164=f"+971500000{uuid.uuid4().int % 1000:03d}",
            consent_status="opted_in",
            retention_state="active",
        )
        session.add(contact)
        session.flush()
        conversation = Conversation(
            contact_id=contact.id,
            channel="whatsapp",
            provider_account_id="test",
            state="closed",
            active_intent="buy",
            closed_at=NOW - timedelta(days=2),
        )
        session.add(conversation)
        session.flush()
        message = Message(
            conversation_id=conversation.id,
            provider_account_id="test",
            direction="inbound",
            sender_type="customer",
            message_type="text",
            body_text="I would like to arrange a viewing next week.",
            created_at=conversation.closed_at - timedelta(minutes=1),
        )
        session.add(message)
        session.flush()
        run = SkillUpdateRun(
            status="running",
            selection_json={},
            application_release="extract-test",
            reviewer_release="1",
        )
        session.add(run)
        session.flush()
        run_id = run.id
        builder = ConversationSnapshotBuilder(
            skill_directory=SKILLS,
            application_release="extract-test",
            retention_days=7,
        )
        snapshot = builder.build(
            session, SelectedConversation(conversation.id, conversation.closed_at, "buyer"), now=NOW
        )

    if isinstance(output, LearningReviewOutput) and output.evidence_refs == ["ev_example_001"]:
        output = output.model_copy(update={"evidence_refs": [snapshot.messages[0].message_ref]})

    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700, parents=True)
    storage = CandidateStorage(root, allow_ephemeral_for_tests=True)
    artifact = builder.store(snapshot, storage)
    with factory.begin() as session:
        run = session.get(SkillUpdateRun, run_id)
        assert run is not None
        metadata = artifact.to_json()
        records = list(run.selection_json.get("snapshot_artifacts", []))
        records.append(metadata)
        run.selection_json = {"snapshot_artifacts": records}

    reviewer = FakeLearningReviewer(output)
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_release="extract-test",
        skill_updates_enabled=True,
        skill_update_extraction_enabled=True,
        skill_update_candidate_writes_enabled=True,
        skill_update_min_confidence=0.85,
    )
    extractor = SkillUpdateExtractor(factory, settings, storage, reviewer, skill_directory=SKILLS)
    return extractor, snapshot, run_id, root, reviewer


def test_novel_learning_creates_one_hashed_candidate_without_changing_active_skills(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    before = {path.name: path.read_bytes() for path in SKILLS.glob("*.md")}
    extractor, snapshot, run_id, root, reviewer = prepare(factory, tmp_path, make_output())

    result = extractor.process(snapshot, run_id, now=NOW)

    assert result.status == "candidate_created", result.reason_code
    assert result.relative_path == "buyer/candidate_skills/buyer_conversation_skill_1.md"
    candidate_path = root / result.relative_path
    assert candidate_path.is_file()
    assert "[NAME]" not in candidate_path.read_text()
    assert snapshot.messages[0].message_ref in candidate_path.read_text()
    with factory() as session:
        row = session.scalar(select(SkillCandidateFile))
        run = session.get(SkillUpdateRun, run_id)
        assert row is not None and row.sha256 == result.sha256
        assert row.status == "pending" and row.content_fingerprint == result.fingerprint
        assert run is not None and run.candidate_count == 1
        assert (
            run.selection_json["extraction_outcomes"][str(snapshot.conversation_id)]["status"]
            == "candidate_created"
        )
    assert len(reviewer.requests) == 1
    assert {path.name: path.read_bytes() for path in SKILLS.glob("*.md")} == before


def test_no_learning_and_invalid_outputs_record_outcome_without_candidate_file(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    no_learning = LearningReviewOutput(
        status="no_learning",
        source_conversation_type="buyer",
        confidence=0.2,
        duplicate_or_novel="not_applicable",
    )
    extractor, snapshot, run_id, root, _ = prepare(factory, tmp_path / "no-learning", no_learning)
    result = extractor.process(snapshot, run_id, now=NOW)
    assert result.status == "no_candidate"
    assert list(root.rglob("*.md")) == []

    cases = [
        (make_output(evidence_refs=["ev_not_in_snapshot"]), "unknown_evidence_reference"),
        (make_output(confidence=0.5), "confidence_below_threshold"),
        (
            make_output(instruction="Call Jane at jane@example.com before offering slots."),
            "personal_data_in_learning",
        ),
        (make_output(policy_conflicts=["unsupported claim"]), "policy_conflict"),
        (make_output(source_conversation_type="seller"), "source_type_mismatch"),
    ]
    for index, (output, expected_reason) in enumerate(cases):
        extractor, snapshot, run_id, root, _ = prepare(
            factory, tmp_path / f"invalid-{index}", output
        )
        result = extractor.process(snapshot, run_id, now=NOW)
        assert result.status == "rejected" and result.reason_code == expected_reason
        assert list(root.rglob("*.md")) == []
        with factory() as session:
            assert session.scalar(select(SkillCandidateFile)) is None
            run = session.get(SkillUpdateRun, run_id)
            assert run is not None
            assert (
                run.selection_json["extraction_outcomes"][str(snapshot.conversation_id)]["status"]
                == "rejected"
            )


def test_duplicate_learning_does_not_allocate_or_write_a_second_candidate(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    first_extractor, snapshot, run_id, root, _ = prepare(factory, tmp_path / "first", make_output())
    first = first_extractor.process(snapshot, run_id, now=NOW)
    assert first.status == "candidate_created"

    second_factory = factory
    # A second run for the same conversation is stopped before the reviewer is called.
    second_reviewer = FakeLearningReviewer(make_output())
    second_extractor = SkillUpdateExtractor(
        second_factory,
        Settings(
            _env_file=None,
            app_env="test",
            app_release="extract-test",
            skill_updates_enabled=True,
            skill_update_extraction_enabled=True,
            skill_update_candidate_writes_enabled=True,
        ),
        first_extractor.storage,
        second_reviewer,
        skill_directory=SKILLS,
    )
    second = second_extractor.process(snapshot, run_id, now=NOW)
    assert second.status == "already_candidate"
    assert len(list(root.rglob("*.md"))) == 1
    assert not second_reviewer.requests


def test_shadow_extraction_records_quality_outcome_without_candidate_file(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    extractor, snapshot, run_id, root, _ = prepare(factory, tmp_path / "shadow", make_output())
    extractor.settings.skill_update_candidate_writes_enabled = False

    result = extractor.process(snapshot, run_id, now=NOW)

    assert result.status == "shadow_candidate"
    assert not list((root / "buyer" / "candidate_skills").rglob("*.md"))
    with factory() as session:
        run = session.get(SkillUpdateRun, run_id)
        assert run is not None
        outcome = run.selection_json["extraction_outcomes"][str(snapshot.conversation_id)]
        assert outcome["reason_code"] == "candidate_writes_disabled"
        assert outcome["fingerprint"] == result.fingerprint
