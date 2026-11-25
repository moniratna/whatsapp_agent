from __future__ import annotations

import logging
import time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import ReviewerUpdateRun
from app.skill_updates.reviewer_updates import (
    BedrockReviewerCaseRunner,
    BedrockReviewerImprovementProposer,
    ReviewerUpdateLane,
)

logger = logging.getLogger("whatsapp_assistant.reviewer_update_worker")


class ReviewerUpdateWorker:
    """Polls the durable post-customer-merge reviewer update queue."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        lane: ReviewerUpdateLane | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.lane = lane

    def process_one(self) -> bool:
        if not self.settings.skill_reviewer_updates_enabled:
            return False
        run_id: UUID | None
        with self.session_factory() as session:
            run_id = session.scalar(
                select(ReviewerUpdateRun.id)
                .where(
                    ReviewerUpdateRun.status.in_(
                        {"queued", "evaluating", "applying", "cleanup_pending"}
                    )
                )
                .order_by(ReviewerUpdateRun.created_at, ReviewerUpdateRun.id)
                .limit(1)
            )
        if run_id is None:
            return False
        try:
            self._get_lane().process(run_id)
        except Exception:
            # The lane records deterministic failures itself. Unexpected transient
            # errors leave its durable state replayable by the next poll.
            logger.exception("reviewer_update_worker_error run_id=%s", run_id)
            time.sleep(1)
            return True
        return True

    def _get_lane(self) -> ReviewerUpdateLane:
        if self.lane is not None:
            return self.lane
        from app.skill_updates.storage import CandidateStorage

        storage = CandidateStorage.from_settings(self.settings)
        self.lane = ReviewerUpdateLane(
            self.session_factory,
            self.settings,
            storage,
            BedrockReviewerImprovementProposer(self.settings),
            BedrockReviewerCaseRunner(self.settings),
        )
        return self.lane


def run_worker() -> None:
    from app.db.connection import make_engine, make_session_factory

    settings = Settings()
    worker = ReviewerUpdateWorker(make_session_factory(make_engine(settings)), settings)
    while True:
        if not worker.process_one():
            time.sleep(1)


if __name__ == "__main__":
    run_worker()
