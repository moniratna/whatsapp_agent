from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.db.models import SkillCandidateFile, SkillConsolidationBatch
from app.skill_updates.batches import SkillBatchService
from app.skill_updates.merge_reviewer import FakeSecondReviewer
from app.skill_updates.merge_schemas import CandidateRouting, SecondReviewOutput
from app.skill_updates.proposals import BatchProposalService, ProposalError
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


def setup_batch(
    factory: sessionmaker[Session],
    storage: CandidateStorage,
    *,
    count: int = 50,
    batch_size: int = 50,
    skill_directory: Path = SKILLS,
    max_bytes: int = 50_000,
) -> tuple[uuid.UUID, list[uuid.UUID], Settings]:
    rows: list[SkillCandidateFile] = []
    for sequence in range(1, count + 1):
        candidate_id = uuid.uuid4()
        fingerprint = hashlib.sha256(f"rule-{sequence}".encode()).hexdigest()
        topics = [
            "ownership timing",
            "community transport",
            "room configuration",
            "garden maintenance",
            "access requirements",
            "monthly budget",
            "move-in schedule",
            "parking arrangement",
            "pet accommodation",
            "school proximity",
        ]
        actions = ["ask", "confirm", "clarify", "summarize", "check"]
        details = [
            "preferred timeframe",
            "access requirements",
            "layout priorities",
            "maintenance expectations",
            "shared amenities",
            "spending comfort",
            "handover constraints",
            "vehicle needs",
            "animal requirements",
            "education preferences",
        ]
        index = sequence - 1
        topic = topics[index % len(topics)]
        action = actions[(index // len(topics)) % len(actions)]
        detail = details[(index // len(actions)) % len(details)]
        instruction = (
            f"For {topic}, {action} about {detail} and record the customer's stated response."
        )
        content = (
            "---\n"
            f'candidate_id: "{candidate_id}"\n'
            "candidate_type: customer_skill\n"
            "source_conversation_type: buyer\n"
            "target_skill_key: null\n"
            f'source_run_id: "{uuid.uuid4()}"\n'
            'source_skill_version: "source-hash"\n'
            f'content_fingerprint: "{fingerprint}"\n'
            "confidence: 0.94\n"
            'evidence_refs: ["ev_source_001"]\n'
            "---\n\n"
            "## Learning\n\n"
            f"Trigger: when topic {sequence} is raised by the customer\n\n"
            f"Lesson: gather the distinct preference category {sequence}\n\n"
            f"Instruction: {instruction}\n\n"
            "## Regression cases\n\n"
            f"- Case: Customer asks about unique topic {sequence}.\n"
            f"  Expected: Ask about priority {sequence} and record the choice.\n"
        )
        entry = storage.write_candidate("buyer", sequence, content)
        rows.append(
            SkillCandidateFile(
                id=candidate_id,
                candidate_type="customer_skill",
                source_conversation_type="buyer",
                sequence=sequence,
                conversation_id=uuid.uuid4(),
                status="pending",
                relative_path=entry.relative_path,
                sha256=entry.sha256,
                content_fingerprint=fingerprint,
                evidence_refs_json=["ev_source_001"],
                regression_cases_json=[
                    {
                        "case": f"Customer asks about unique topic {sequence}.",
                        "expected_behavior": (
                            f"Ask about priority {sequence} and record the choice."
                        ),
                    }
                ],
                source_snapshot_ids_json=[f"snapshot-{sequence}"],
            )
        )
    with factory.begin() as session:
        session.add_all(rows)

    settings = Settings(
        _env_file=None,
        app_env="test",
        skill_updates_enabled=True,
        skill_update_extraction_enabled=True,
        skill_update_candidate_writes_enabled=True,
        skill_update_consolidation_enabled=True,
        skill_update_batch_size=batch_size,
        skill_update_consolidated_max_bytes=max_bytes,
        skill_update_active_skill_max_bytes=200_000,
        skill_update_storage_dir=storage.root,
    )
    batch_service = SkillBatchService(factory, settings, storage)
    claimed = batch_service.claim_batch("buyer", worker_id="merge-worker", now=NOW)
    assert claimed is not None
    batch_service.consolidate(claimed.batch_id, worker_id="merge-worker", now=NOW)
    return claimed.batch_id, list(claimed.candidate_ids), settings


def all_accept(candidate_ids: list[uuid.UUID]) -> SecondReviewOutput:
    routes = [
        CandidateRouting(candidate_id=item, decision="accept", target_skill_key="buyer")
        for item in candidate_ids
    ]
    return SecondReviewOutput(reviews=routes)


def make_service(
    factory: sessionmaker[Session],
    settings: Settings,
    storage: CandidateStorage,
    reviewer: Any,
    skills: Path,
) -> BatchProposalService:
    return BatchProposalService(factory, settings, storage, reviewer, skill_directory=skills)


@pytest.fixture
def artifacts(tmp_path: Path) -> CandidateStorage:
    root = tmp_path / "skill-artifacts"
    root.mkdir(mode=0o700)
    return CandidateStorage(root, allow_ephemeral_for_tests=True)


def test_second_review_prepares_hash_pinned_target_update_without_touching_active_skills(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    active_before = {path.name: path.read_bytes() for path in skills.glob("*.md")}
    batch_id, ids, settings = setup_batch(factory, artifacts, skill_directory=skills)
    reviewer = FakeSecondReviewer(all_accept(ids))
    proposals = make_service(factory, settings, artifacts, reviewer, skills)

    result = proposals.prepare(batch_id, worker_id="merge-worker", now=NOW)

    assert result["accounted_candidate_count"] == 50
    assert result["accepted_candidate_count"] == 50
    assert result["target_skill_keys"] == ["buyer"]
    assert len(reviewer.requests) == 1
    assert len(reviewer.requests[0].candidates) == 50
    assert set(reviewer.requests[0].active_skill_hashes) == {
        "buyer",
        "seller",
        "portfolio",
        "general",
        "unknown",
    }
    assert {path.name: path.read_bytes() for path in skills.glob("*.md")} == active_before
    proposal_meta = result["proposal_artifacts"]["buyer"]
    proposal = artifacts.read_work_artifact(
        Path(proposal_meta["relative_path"]).name, proposal_meta["sha256"]
    ).decode()
    assert "## Reviewed learnings" in proposal
    assert "release: 5" in proposal.split("---", 2)[1]
    assert "Instruction:" in proposal
    with factory() as session:
        batch = session.get(SkillConsolidationBatch, batch_id)
        assert batch is not None and batch.status == "validating"
        assert batch.base_skill_hashes_json == result["base_skill_hashes"]
        assert batch.validation_json["second_review"]["accounted_candidate_count"] == 50
        assert len(batch.validation_json["second_review"]["outcomes"]) == 50
        assert all(
            candidate.status == "claimed"
            for candidate in session.scalars(select(SkillCandidateFile))
        )
        assert all(
            candidate.target_skill_key == "buyer"
            for candidate in session.scalars(select(SkillCandidateFile))
        )


def test_invalid_second_review_accounting_fails_without_active_or_proposal_changes(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    active_before = {path.name: path.read_bytes() for path in skills.glob("*.md")}
    batch_id, ids, settings = setup_batch(factory, artifacts, skill_directory=skills)
    routes = [
        CandidateRouting(candidate_id=item, decision="accept", target_skill_key="buyer")
        for item in ids
    ]
    routes[-1] = CandidateRouting(
        candidate_id=uuid.uuid4(), decision="accept", target_skill_key="buyer"
    )
    proposals = make_service(
        factory, settings, artifacts, FakeSecondReviewer(SecondReviewOutput(reviews=routes)), skills
    )

    with pytest.raises(ProposalError, match="candidate_accounting_mismatch"):
        proposals.prepare(batch_id, worker_id="merge-worker", now=NOW)

    assert not list(artifacts.root.glob("work/*-proposal-*.md"))
    assert {path.name: path.read_bytes() for path in skills.glob("*.md")} == active_before
    with factory() as session:
        batch = session.get(SkillConsolidationBatch, batch_id)
        assert batch is not None and batch.status == "failed"
        rows = list(session.scalars(select(SkillCandidateFile)))
        assert len(rows) == 50 and {row.status for row in rows} == {"claimed"}


class MutatingReviewer(FakeSecondReviewer):
    def __init__(self, output: object, skill_file: Path) -> None:
        super().__init__(output)
        self.skill_file = skill_file

    def review(self, request: Any) -> object:
        result = super().review(request)
        if len(self.requests) == 1:
            self.skill_file.write_text(
                self.skill_file.read_text() + "\nTemporary operator edit for revalidation.\n"
            )
        return result


def test_active_skill_change_repeats_review_and_rebuilds_from_new_base(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, ids, settings = setup_batch(factory, artifacts, skill_directory=skills)
    reviewer = MutatingReviewer(all_accept(ids), skills / "buyer.md")
    proposals = make_service(factory, settings, artifacts, reviewer, skills)

    result = proposals.prepare(batch_id, worker_id="merge-worker", now=NOW)

    assert len(reviewer.requests) == 2
    assert result["accounted_candidate_count"] == 50
    assert (
        result["base_skill_hashes"]["buyer"]
        == hashlib.sha256((skills / "buyer.md").read_bytes()).hexdigest()
    )
    proposal = artifacts.read_work_artifact(
        Path(result["proposal_artifacts"]["buyer"]["relative_path"]).name,
        result["proposal_artifacts"]["buyer"]["sha256"],
    ).decode()
    assert "Temporary operator edit for revalidation." in proposal


def test_second_review_can_group_proposals_into_multiple_active_skills(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, ids, settings = setup_batch(factory, artifacts, skill_directory=skills)
    routes = [
        CandidateRouting(
            candidate_id=item,
            decision="accept",
            target_skill_key="buyer" if index < 25 else "seller",
        )
        for index, item in enumerate(ids)
    ]
    reviewer = FakeSecondReviewer(SecondReviewOutput(reviews=routes))
    proposals = make_service(factory, settings, artifacts, reviewer, skills)

    result = proposals.prepare(batch_id, worker_id="merge-worker", now=NOW)

    assert result["target_skill_keys"] == ["buyer", "seller"]
    assert set(result["proposal_artifacts"]) == {"buyer", "seller"}
    assert all(
        len(artifact["accepted_candidate_ids"]) == 25
        for artifact in result["proposal_artifacts"].values()
    )
    with factory() as session:
        target_by_id = {
            str(candidate.id): candidate.target_skill_key
            for candidate in session.scalars(select(SkillCandidateFile))
        }
    assert [target_by_id[str(candidate_id)] for candidate_id in ids] == [
        "buyer" if index < 25 else "seller" for index in range(50)
    ]
