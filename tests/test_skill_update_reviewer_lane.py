from __future__ import annotations

import shutil
import sys
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).parent))
from test_skill_update_proposals import NOW, SKILLS, all_accept, make_service, setup_batch  # noqa: E402

from app.db.base import Base
from app.db.models import (
    ReviewerEvaluation,
    ReviewerUpdateRun,
    SkillCandidateFile,
    SkillConsolidationBatch,
)
from app.skill_updates.activation import SkillActivationService
from app.skill_updates.merge_reviewer import FakeSecondReviewer
from app.skill_updates.reviewer_updates import (
    REVIEWER_EVAL_SUITE,
    ReviewerEvalCase,
    ReviewerUpdateLane,
)
from app.skill_updates.schemas import LearningReviewOutput, RegressionCase
from app.skill_updates.storage import CandidateStorage
from app.skills.loader import Skill
from app.workers.reviewer_updates import ReviewerUpdateWorker


@pytest.fixture
def factory() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def artifacts(tmp_path: Path) -> CandidateStorage:
    root = tmp_path / "skill-artifacts"
    root.mkdir(mode=0o700)
    return CandidateStorage(root, allow_ephemeral_for_tests=True)


class Proposer:
    def __init__(self, proposal: str | None) -> None:
        self.proposal = proposal
        self.requests: list[list[str]] = []

    def propose(
        self,
        active_reviewer: Skill,
        suite: Sequence[ReviewerEvalCase],
        failed_case_ids: list[str],
    ) -> str | None:
        self.requests.append(failed_case_ids)
        return self.proposal


class Runner:
    def __init__(self, *, passes_active: bool = False) -> None:
        self.passes_active = passes_active
        self.calls: list[tuple[str, str]] = []

    def run(self, reviewer: Skill, case: ReviewerEvalCase) -> object:
        self.calls.append((reviewer.header.release, case.case_id))
        candidate = reviewer.header.release == "2" or self.passes_active
        status = case.expected_status if candidate else "no_learning"
        conflicts = ["prohibited claim"] if candidate and case.expected_policy_conflict else []
        return LearningReviewOutput(
            status=status,
            lesson="Record the timing preference" if status == "learning" else None,
            source_conversation_type=case.source_type,
            trigger="when timing is discussed" if status == "learning" else None,
            instruction="Ask and record the customer's preferred timing"
            if status == "learning"
            else None,
            evidence_refs=[case.snapshot["evidence_refs"][0]] if status == "learning" else [],
            confidence=0.95,
            duplicate_or_novel="novel" if status == "learning" else "not_applicable",
            policy_conflicts=conflicts,
            regression_cases=(
                [
                    RegressionCase(
                        case="Customer discusses timing preference.",
                        expected_behavior="Ask for and record the preferred timing.",
                    )
                ]
                if status == "learning"
                else []
            ),
        )


def proposed_reviewer() -> str:
    path = SKILLS / "skill_reviewer.md"
    text = path.read_text()
    body = text.split("---\n", 2)[2]
    return (
        "---\nkey: skill_reviewer\nrelease: 2\nintent: skill_reviewer\n"
        "triggers: [review]\nrequired_fields: []\noptional_fields: []\n---\n"
        + body
        + "\n\nFor every case, verify that the expected source type is copied exactly."
    )


def setup_lane(
    factory: sessionmaker[Session],
    artifacts: CandidateStorage,
    tmp_path: Path,
    *,
    min_improvements: int = 1,
) -> tuple[ReviewerUpdateLane, Any, Path, Any, Any]:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, ids, settings = setup_batch(factory, artifacts, skill_directory=skills)
    make_service(factory, settings, artifacts, FakeSecondReviewer(all_accept(ids)), skills).prepare(
        batch_id, worker_id="merge-worker", now=NOW
    )
    settings.skill_updates_enabled = True
    settings.skill_update_auto_merge_enabled = True
    settings.skill_reviewer_updates_enabled = True
    settings.skill_reviewer_eval_min_improved_cases = min_improvements
    activation = SkillActivationService(factory, settings, artifacts, skill_directory=skills)
    activation.process_batch(batch_id, worker_id="merge-worker", now=NOW)
    lane = ReviewerUpdateLane(
        factory,
        settings,
        artifacts,
        Proposer(proposed_reviewer()),
        Runner(),
        skill_directory=skills,
    )
    with factory() as session:
        run = session.scalar(
            select(ReviewerUpdateRun).where(ReviewerUpdateRun.customer_batch_id == batch_id)
        )
        assert run is not None
        run_id = run.id
    return lane, run_id, skills, settings, batch_id


def test_success_evaluates_candidate_individually_activates_and_cleans_one(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    lane, run_id, skills, _, _ = setup_lane(factory, artifacts, tmp_path)
    original = (skills / "skill_reviewer.md").read_bytes()

    result = lane.process(run_id)

    assert result == {"run_id": str(run_id), "status": "completed", "removed_count": 1}
    assert (skills / "skill_reviewer.md").read_bytes() != original
    assert (
        (skills / "skill_reviewer.md")
        .read_text()
        .startswith("---\nkey: skill_reviewer\nrelease: 2")
    )
    assert not list((artifacts.root / "skill_reviewer" / "candidate_skills").glob("*.md"))
    with factory() as session:
        run = session.get(ReviewerUpdateRun, run_id)
        candidate = session.get(SkillCandidateFile, run.candidate_id if run else None)
        evaluation = session.scalar(select(ReviewerEvaluation))
        assert run is not None and run.status == "completed"
        assert candidate is not None and candidate.candidate_type == "reviewer_skill"
        assert candidate.target_skill_key == "skill_reviewer" and candidate.status == "cleaned"
        assert evaluation is not None and evaluation.decision == "passed"
        assert len(evaluation.case_results_json) == len(REVIEWER_EVAL_SUITE)
    rollback = lane.rollback(run_id)
    assert rollback["status"] == "rolled_back"
    assert (skills / "skill_reviewer.md").read_bytes() == original
    assert lane.rollback(run_id)["status"] == "rolled_back"


def test_active_passes_suite_records_no_change_without_candidate(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    lane, run_id, skills, _, _ = setup_lane(factory, artifacts, tmp_path)
    lane.runner = Runner(passes_active=True)
    original = (skills / "skill_reviewer.md").read_bytes()

    result = lane.process(run_id)

    assert result["status"] == "no_change"
    assert (skills / "skill_reviewer.md").read_bytes() == original
    assert not list((artifacts.root / "skill_reviewer" / "candidate_skills").glob("*.md"))
    with factory() as session:
        run = session.get(ReviewerUpdateRun, run_id)
        assert run is not None and run.status == "no_change" and run.candidate_id is None


def test_failed_evaluation_leaves_active_reviewer_unchanged_and_candidate_for_audit(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    lane, run_id, skills, _, _ = setup_lane(factory, artifacts, tmp_path, min_improvements=4)
    original = (skills / "skill_reviewer.md").read_bytes()

    result = lane.process(run_id)

    assert result["status"] == "failed"
    assert (skills / "skill_reviewer.md").read_bytes() == original
    assert len(list((artifacts.root / "skill_reviewer" / "candidate_skills").glob("*.md"))) == 1
    with factory() as session:
        run = session.get(ReviewerUpdateRun, run_id)
        candidate = session.get(SkillCandidateFile, run.candidate_id if run else None)
        evaluation = session.scalar(select(ReviewerEvaluation))
        assert run is not None and run.status == "failed"
        assert candidate is not None and candidate.status == "rejected"
        assert evaluation is not None and evaluation.decision == "failed"


def test_customer_candidate_never_enters_reviewer_lane(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    lane, run_id, _, _, _ = setup_lane(factory, artifacts, tmp_path)
    with factory() as session:
        run = session.get(ReviewerUpdateRun, run_id)
        assert run is not None
        batch = session.get(SkillConsolidationBatch, run.customer_batch_id)
        assert batch is not None
        customer_rows = list(
            session.scalars(
                select(SkillCandidateFile).where(
                    SkillCandidateFile.candidate_type == "customer_skill"
                )
            )
        )
        assert len(customer_rows) == 50
        assert run.candidate_id is None
        assert all(row.source_conversation_type == "buyer" for row in customer_rows)


def test_disabled_reviewer_lane_does_not_process(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    lane, run_id, _, settings, _ = setup_lane(factory, artifacts, tmp_path)
    settings.skill_reviewer_updates_enabled = False
    with pytest.raises(RuntimeError, match="reviewer_updates_disabled"):
        lane.process(run_id)


def test_worker_consumes_only_when_reviewer_flag_is_enabled(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    lane, run_id, _, settings, _ = setup_lane(factory, artifacts, tmp_path)

    class RecordingLane:
        def __init__(self) -> None:
            self.processed: list[Any] = []

        def process(self, run_id: Any) -> dict[str, str]:
            self.processed.append(run_id)
            return {"status": "no_change"}

    recording = RecordingLane()
    worker = ReviewerUpdateWorker(factory, settings, recording)  # type: ignore[arg-type]
    assert worker.process_one()
    assert recording.processed == [run_id]
    settings.skill_reviewer_updates_enabled = False
    assert not worker.process_one()
    assert recording.processed == [run_id]
