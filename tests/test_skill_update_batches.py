from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.db.models import SkillCandidateFile, SkillConsolidationBatch
from app.skill_updates.batches import BatchError, SkillBatchService
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


def candidate_markdown(sequence: int, source_type: str) -> str:
    return (
        "---\n"
        f'candidate_id: "{uuid.uuid4()}"\n'
        "candidate_type: customer_skill\n"
        f"source_conversation_type: {source_type}\n"
        "target_skill_key: null\n"
        f'source_run_id: "{uuid.uuid4()}"\n'
        'source_skill_version: "test-hash"\n'
        f'content_fingerprint: "{hashlib.sha256(str(sequence).encode()).hexdigest()}"\n'
        "confidence: 0.90\n"
        'evidence_refs: ["ev_evidence_001"]\n'
        "---\n\n"
        "## Learning\n\n"
        f"Trigger: when the customer asks about step {sequence}\n\n"
        f"Lesson: reusable lesson for case {sequence}\n\n"
        f"Instruction: follow the safe response behavior for example {sequence}.\n\n"
        "## Regression cases\n\n"
        f"- Case: a customer asks the question numbered {sequence}.\n"
        f"  Expected: ask one clarifying question for case {sequence}.\n"
    )


def seed_candidates(
    factory: sessionmaker[Session], storage: CandidateStorage, *, source_type: str, count: int
) -> None:
    rows: list[SkillCandidateFile] = []
    for sequence in range(1, count + 1):
        content = candidate_markdown(sequence, source_type)
        entry = storage.write_candidate(source_type, sequence, content)
        rows.append(
            SkillCandidateFile(
                id=uuid.uuid4(),
                candidate_type="customer_skill",
                source_conversation_type=source_type,
                sequence=sequence,
                conversation_id=uuid.uuid4(),
                status="pending",
                relative_path=entry.relative_path,
                sha256=entry.sha256,
                evidence_refs_json=["ev_evidence_001"],
                regression_cases_json=[],
                source_snapshot_ids_json=[],
            )
        )
    with factory.begin() as session:
        session.add_all(rows)


def service(
    factory: sessionmaker[Session], storage: CandidateStorage, *, max_bytes: int = 2_000
) -> SkillBatchService:
    return SkillBatchService(
        factory,
        Settings(
            _env_file=None,
            app_env="test",
            skill_updates_enabled=True,
            skill_update_extraction_enabled=True,
            skill_update_candidate_writes_enabled=True,
            skill_update_consolidation_enabled=True,
            skill_update_batch_size=50,
            skill_update_consolidated_max_bytes=max_bytes,
            skill_update_storage_dir=storage.root,
        ),
        storage,
    )


def test_fewer_than_fifty_waits_and_queues_remain_separate(
    factory: sessionmaker[Session], artifacts: CandidateStorage
) -> None:
    seed_candidates(factory, artifacts, source_type="buyer", count=49)
    seed_candidates(factory, artifacts, source_type="seller", count=50)
    batches = service(factory, artifacts)

    assert batches.claim_batch("buyer", worker_id="worker-a") is None
    seller_batch = batches.claim_batch("seller", worker_id="worker-b")

    assert seller_batch is not None
    assert len(seller_batch.candidate_ids) == 50
    with factory() as session:
        statuses = list(
            session.scalars(
                select(SkillCandidateFile.status).where(
                    SkillCandidateFile.source_conversation_type == "buyer"
                )
            )
        )
        assert set(statuses) == {"pending"}


def test_batch_claim_is_gated_by_consolidation_flag(
    factory: sessionmaker[Session], artifacts: CandidateStorage
) -> None:
    from app.skill_updates.batches import BatchError

    disabled = SkillBatchService(
        factory,
        Settings(_env_file=None, skill_update_batch_size=50),
        artifacts,
    )
    with pytest.raises(BatchError, match="skill_update_consolidation_disabled"):
        disabled.claim_batch("buyer", worker_id="worker-a")


def test_claim_freezes_exactly_fifty_ordered_candidates_once(
    factory: sessionmaker[Session], artifacts: CandidateStorage
) -> None:
    seed_candidates(factory, artifacts, source_type="buyer", count=51)
    batches = service(factory, artifacts)

    claimed = batches.claim_batch("buyer", worker_id="worker-a")
    assert claimed is not None and len(claimed.candidate_ids) == 50
    assert batches.claim_batch("buyer", worker_id="worker-b") is None

    with factory() as session:
        rows = list(
            session.scalars(
                select(SkillCandidateFile)
                .where(SkillCandidateFile.source_conversation_type == "buyer")
                .order_by(SkillCandidateFile.sequence)
            )
        )
        batch = session.get(SkillConsolidationBatch, claimed.batch_id)
        assert batch is not None
        assert batch.candidate_ids_json == [str(item.id) for item in rows[:50]]
        assert batch.manifest_sha256 == claimed.manifest_sha256
        assert [item.status for item in rows] == ["claimed"] * 50 + ["pending"]
        manifest = artifacts.read_work_artifact(f"batch-{claimed.batch_id}-manifest.json")
        assert hashlib.sha256(manifest).hexdigest() == claimed.manifest_sha256


def test_consolidation_splits_only_between_candidate_rules_and_accounts_for_all(
    factory: sessionmaker[Session], artifacts: CandidateStorage
) -> None:
    seed_candidates(factory, artifacts, source_type="buyer", count=50)
    batches = service(factory, artifacts, max_bytes=1_800)
    claimed = batches.claim_batch("buyer", worker_id="worker-a")
    assert claimed is not None

    result = batches.consolidate(claimed.batch_id, worker_id="worker-a")

    assert len(result.part_paths) > 1
    assert len(result.candidate_parts) == 50
    assert set(result.candidate_parts) == {str(value) for value in claimed.candidate_ids}
    loaded_candidate_blocks = 0
    for index, relative_path in enumerate(result.part_paths, start=1):
        name = Path(relative_path).name
        content = artifacts.read_work_artifact(name).decode()
        assert len(content.encode()) <= 1_800
        assert f"Part: {index}" in content
        loaded_candidate_blocks += content.count("## Candidate ")
    assert loaded_candidate_blocks == 50
    with factory() as session:
        batch = session.get(SkillConsolidationBatch, claimed.batch_id)
        assert batch is not None and batch.status == "consolidating"
        assert batch.validation_json["accounted_candidate_count"] == 50
        assert len(batch.validation_json["candidate_parts"]) == 50
        assert len(batch.validation_json["consolidated_artifacts"]) == len(result.part_paths)
        assert all(
            len(item["sha256"]) == 64 for item in batch.validation_json["consolidated_artifacts"]
        )


def test_tampered_candidate_fails_closed_and_remains_claimed_for_investigation(
    factory: sessionmaker[Session], artifacts: CandidateStorage
) -> None:
    seed_candidates(factory, artifacts, source_type="buyer", count=50)
    batches = service(factory, artifacts)
    claimed = batches.claim_batch("buyer", worker_id="worker-a")
    assert claimed is not None
    with factory() as session:
        row = session.get(SkillCandidateFile, claimed.candidate_ids[0])
        assert row is not None
        path = artifacts.root / row.relative_path
        path.write_text(path.read_text().replace("reusable lesson", "tampered lesson"))

    with pytest.raises(BatchError):
        batches.consolidate(claimed.batch_id, worker_id="worker-a")

    with factory() as session:
        batch = session.get(SkillConsolidationBatch, claimed.batch_id)
        candidates = list(
            session.scalars(
                select(SkillCandidateFile).where(SkillCandidateFile.id.in_(claimed.candidate_ids))
            )
        )
        assert batch is not None and batch.status == "failed"
        assert len(candidates) == 50
        assert {item.status for item in candidates} == {"claimed"}


def test_batch_lease_prevents_a_second_worker_from_consolidating(
    factory: sessionmaker[Session], artifacts: CandidateStorage
) -> None:
    seed_candidates(factory, artifacts, source_type="buyer", count=50)
    batches = service(factory, artifacts)
    claimed = batches.claim_batch("buyer", worker_id="worker-a")
    assert claimed is not None

    with pytest.raises(BatchError, match="batch_lease_owner_mismatch"):
        batches.consolidate(claimed.batch_id, worker_id="worker-b")

    with factory() as session:
        batch = session.get(SkillConsolidationBatch, claimed.batch_id)
        assert batch is not None and batch.status == "claimed"
