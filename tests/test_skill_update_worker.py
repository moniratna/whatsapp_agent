from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.db.models import SkillUpdateRun
from app.skill_updates.storage import CandidateStorage
from app.workers.skill_updates import SkillUpdateWorker


def test_scheduled_worker_creates_and_completes_disabled_stage_run(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage_dir = tmp_path / "artifacts"
    storage_dir.mkdir(mode=0o700)
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_release="worker-test",
        skill_updates_enabled=True,
        skill_update_storage_dir=storage_dir,
        skill_update_schedule_minutes=60,
    )
    worker = SkillUpdateWorker(
        factory,
        settings,
        storage=CandidateStorage(storage_dir, allow_ephemeral_for_tests=True),
        skill_directory=Path(__file__).parents[1] / "skills",
    )

    assert worker.process_one(now=datetime(2026, 11, 25, tzinfo=UTC)) is True

    with factory() as session:
        run = session.scalar(select(SkillUpdateRun))
        assert run is not None
        assert run.status == "completed"
        assert run.application_release == "worker-test"
        assert run.reviewer_release == "1"
        assert run.completed_at is not None
    engine.dispose()


def test_disabled_worker_does_not_enqueue_runs(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(bind=engine)
    settings = Settings(_env_file=None, app_env="test")
    worker = SkillUpdateWorker(factory, settings)

    assert worker.process_one(now=datetime(2026, 11, 25, tzinfo=UTC)) is False
    with factory() as session:
        assert session.scalar(select(SkillUpdateRun)) is None
    engine.dispose()
