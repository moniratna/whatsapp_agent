from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).parent))
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from test_skill_update_proposals import (  # noqa: E402
    NOW,
    SKILLS,
    all_accept,
    make_service,
    setup_batch,
)

from app.db.base import Base
from app.db.models import SkillActiveUpdate, SkillCandidateFile, SkillConsolidationBatch
from app.skill_updates.activation import ActivationDisabled, ActivationError, SkillActivationService
from app.skill_updates.merge_reviewer import FakeSecondReviewer
from app.skill_updates.storage import CandidateStorage


@pytest.fixture
def factory() -> sessionmaker[Session]:
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


def ready_batch(factory: sessionmaker[Session], artifacts: CandidateStorage, skills: Path):
    batch_id, ids, settings = setup_batch(factory, artifacts, skill_directory=skills)
    settings.skill_updates_enabled = True
    settings.skill_update_auto_merge_enabled = True
    make_service(factory, settings, artifacts, FakeSecondReviewer(all_accept(ids)), skills).prepare(
        batch_id, worker_id="merge-worker", now=NOW
    )
    return batch_id, ids, settings


def test_success_updates_only_target_then_cleans_exact_batch_and_replays(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    before = {name: (skills / name).read_bytes() for name in ("buyer.md", "seller.md")}
    batch_id, ids, settings = ready_batch(factory, artifacts, skills)

    result = SkillActivationService(
        factory, settings, artifacts, skill_directory=skills
    ).process_batch(batch_id, worker_id="merge-worker", now=NOW)

    assert result["status"] == "cleaned"
    assert result["removed_count"] == 50
    assert (skills / "buyer.md").read_bytes() != before["buyer.md"]
    assert (skills / "seller.md").read_bytes() == before["seller.md"]
    assert not any(artifacts.root.glob("buyer/candidate_skills/*.md"))
    with factory() as session:
        batch = session.get(SkillConsolidationBatch, batch_id)
        update = session.scalar(
            select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
        )
        rows = list(
            session.scalars(select(SkillCandidateFile).where(SkillCandidateFile.id.in_(ids)))
        )
        assert batch is not None and batch.status == "cleaned"
        assert update is not None and update.status == "applied"
        assert len(rows) == 50 and all(row.status == "cleaned" for row in rows)
    replay = SkillActivationService(
        factory, settings, artifacts, skill_directory=skills
    ).process_batch(batch_id, worker_id="merge-worker", now=NOW)
    assert replay["status"] == "cleaned"
    assert replay["removed_count"] == 0
    rollback = SkillActivationService(
        factory, settings, artifacts, skill_directory=skills
    ).rollback(batch_id)
    assert rollback["status"] == "rolled_back"
    assert (skills / "buyer.md").read_bytes() == before["buyer.md"]
    assert (skills / "seller.md").read_bytes() == before["seller.md"]


def test_configured_three_candidate_demo_batch_completes_end_to_end(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, ids, settings = setup_batch(
        factory, artifacts, count=3, batch_size=3, skill_directory=skills
    )
    settings.skill_update_auto_merge_enabled = True
    make_service(
        factory, settings, artifacts, FakeSecondReviewer(all_accept(ids)), skills
    ).prepare(batch_id, worker_id="merge-worker", now=NOW)

    result = SkillActivationService(
        factory, settings, artifacts, skill_directory=skills
    ).process_batch(batch_id, worker_id="merge-worker", now=NOW)

    assert result["status"] == "cleaned"
    assert result["removed_count"] == 3
    with factory() as session:
        batch = session.get(SkillConsolidationBatch, batch_id)
        assert batch is not None and batch.status == "cleaned"
        rows = list(session.scalars(select(SkillCandidateFile)))
        assert len(rows) == 3 and all(row.status == "cleaned" for row in rows)
def test_second_target_write_failure_rolls_back_and_preserves_candidates(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, ids, settings = setup_batch(factory, artifacts, skill_directory=skills)
    settings.skill_updates_enabled = True
    settings.skill_update_auto_merge_enabled = True
    from test_skill_update_proposals import CandidateRouting, SecondReviewOutput

    routes = [
        CandidateRouting(
            candidate_id=item,
            decision="accept",
            target_skill_key="buyer" if index < 25 else "seller",
        )
        for index, item in enumerate(ids)
    ]
    make_service(
        factory,
        settings,
        artifacts,
        FakeSecondReviewer(SecondReviewOutput(reviews=routes)),
        skills,
    ).prepare(batch_id, worker_id="merge-worker", now=NOW)
    original = {name: (skills / name).read_bytes() for name in ("buyer.md", "seller.md")}
    service = SkillActivationService(factory, settings, artifacts, skill_directory=skills)
    atomic_replace = service._atomic_replace_active
    calls = 0

    def fail_second(target: str, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replacement failure")
        atomic_replace(target, content)

    service._atomic_replace_active = fail_second  # type: ignore[method-assign]
    with pytest.raises(ActivationError, match="injected_replacement_failure"):
        service.apply(batch_id, worker_id="merge-worker", now=NOW)

    assert {name: (skills / name).read_bytes() for name in original} == original
    assert len(list(artifacts.root.glob("buyer/candidate_skills/*.md"))) == 50
    with factory() as session:
        batch = session.get(SkillConsolidationBatch, batch_id)
        update = session.scalar(
            select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
        )
        assert batch is not None and batch.status == "failed"
        assert batch.next_attempt_at is not None
        assert update is not None and update.status == "rolled_back"


def test_activation_stays_disabled_by_default(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, _, settings = ready_batch(factory, artifacts, skills)
    settings.skill_update_auto_merge_enabled = False
    with pytest.raises(ActivationDisabled):
        SkillActivationService(factory, settings, artifacts, skill_directory=skills).apply(
            batch_id, worker_id="merge-worker", now=datetime.now(UTC)
        )


def test_replay_recovers_filesystem_commit_after_lease_expiry(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, _, settings = ready_batch(factory, artifacts, skills)
    service = SkillActivationService(factory, settings, artifacts, skill_directory=skills)
    batch, candidates, _ = service._load_for_apply(batch_id, "merge-worker", NOW)
    proposal = service._load_proposals(batch, candidates)
    service._begin_apply(batch_id, "merge-worker", NOW)
    service._prepare_update(batch_id, proposal, NOW)
    (skills / "buyer.md").write_bytes(proposal["contents"]["buyer"])
    with factory.begin() as session:
        locked = session.get(SkillConsolidationBatch, batch_id)
        assert locked is not None
        locked.lease_owner = "old-worker"
        locked.lease_expires_at = datetime(2026, 11, 25, 11, 0, tzinfo=UTC)

    result = service.process_batch(batch_id, worker_id="recovery-worker", now=NOW)

    assert result["status"] == "cleaned"
    with factory() as session:
        update = session.scalar(
            select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
        )
        assert update is not None and update.status == "applied"


def test_tampered_manifest_candidate_blocks_all_cleanup(
    factory: sessionmaker[Session], artifacts: CandidateStorage, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS, skills)
    batch_id, _, settings = ready_batch(factory, artifacts, skills)
    service = SkillActivationService(factory, settings, artifacts, skill_directory=skills)
    service.apply(batch_id, worker_id="merge-worker", now=NOW)
    first = sorted((artifacts.root / "buyer" / "candidate_skills").glob("*.md"))[0]
    first.write_text("tampered", encoding="utf-8")

    with pytest.raises(ActivationError):
        service.cleanup(batch_id, now=NOW)

    assert len(list((artifacts.root / "buyer" / "candidate_skills").glob("*.md"))) == 50
    with factory() as session:
        rows = list(session.scalars(select(SkillCandidateFile)))
        batch = session.get(SkillConsolidationBatch, batch_id)
        assert len(rows) == 50 and all(row.status == "claimed" for row in rows)
        assert batch is not None and batch.status == "failed"
