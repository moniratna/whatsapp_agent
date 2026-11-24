from __future__ import annotations

import hashlib
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import (
    Conversation,
    HandoverCase,
    Message,
    SkillImprovementProposal,
    SkillReviewRun,
)
from app.skills.loader import load_skill


class SkillReviewWorker:
    """Create proposals from de-identified operational patterns; never edits skills."""

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.worker_id = str(uuid.uuid4())

    def process_one(self) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(SkillReviewRun)
                .where(
                    (SkillReviewRun.status == "queued")
                    | (
                        (SkillReviewRun.status == "running")
                        & (SkillReviewRun.lease_expires_at <= datetime.now(UTC))
                    )
                )
                .order_by(SkillReviewRun.created_at, SkillReviewRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return False
            run.status = "running"
            run.started_at = datetime.now(UTC)
            run.lease_owner = self.worker_id
            run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=30)
            run_id = run.id
            filters = dict(run.cohort_filters_json)
        try:
            self._review(run_id, filters)
        except Exception:
            with self.session_factory.begin() as session:
                run = session.get(SkillReviewRun, run_id)
                if run:
                    run.status = "failed"
                    run.error_code = "skill_review_failed"
                    run.completed_at = datetime.now(UTC)
                    run.lease_owner = None
                    run.lease_expires_at = None
            return True
        return True

    def _review(self, run_id: uuid.UUID, filters: dict[str, object]) -> None:
        from_date = datetime.fromisoformat(str(filters["from_date"]))
        to_date = datetime.fromisoformat(str(filters["to_date"]))
        minimum_value = filters.get("min_distinct_conversations", 3)
        minimum = minimum_value if isinstance(minimum_value, int) else 3
        # Load and validate reviewer guidance from the deployed Markdown skill.
        reviewer = load_skill(Path("skills/skill_reviewer.md"))
        if reviewer.header.key != "skill_reviewer":
            raise ValueError("reviewer_skill_invalid")
        with self.session_factory() as session:
            handovers = list(
                session.execute(
                    select(HandoverCase, Conversation)
                    .join(Conversation, Conversation.id == HandoverCase.conversation_id)
                    .where(HandoverCase.opened_at >= from_date, HandoverCase.opened_at <= to_date)
                )
            )
            distinct: dict[str, set[uuid.UUID]] = defaultdict(set)
            by_reason: Counter[str] = Counter()
            evidence: dict[str, list[str]] = defaultdict(list)
            conversation_ids: set[uuid.UUID] = set()
            for case, conversation in handovers:
                reason = case.reason_code
                distinct[reason].add(conversation.id)
                by_reason[reason] += 1
                evidence[reason].append(hashlib.sha256(conversation.id.bytes).hexdigest()[:16])
                conversation_ids.add(conversation.id)
            message_count = 0
            if conversation_ids:
                message_count = len(
                    list(
                        session.scalars(
                            select(Message.id).where(
                                Message.conversation_id.in_(conversation_ids),
                                Message.created_at >= from_date,
                                Message.created_at <= to_date,
                            )
                        )
                    )
                )
        with self.session_factory.begin() as session:
            run = session.get(SkillReviewRun, run_id)
            if run is None:
                return
            run.conversation_count = len(conversation_ids)
            run.message_count = message_count
            count = 0
            for reason, conversations in distinct.items():
                if len(conversations) < minimum:
                    continue
                fingerprint = hashlib.sha256(f"unknown:{reason}".encode()).hexdigest()
                proposal = SkillImprovementProposal(
                    skill_review_run_id=run.id,
                    affected_skill_key="unknown",
                    fingerprint=fingerprint,
                    title=f"Review recurring {reason.replace('_', ' ')} escalations",
                    issue_category="handover_pattern",
                    summary=(
                        f"{by_reason[reason]} handover cases across {len(conversations)} "
                        f"distinct conversations used reason code `{reason}` "
                        "in the selected cohort. "
                        "Review the de-identified cases before changing the skill."
                    ),
                    frequency_count=by_reason[reason],
                    distinct_conversation_count=len(conversations),
                    cohort_size=len(conversation_ids),
                    severity="medium",
                    evidence_refs_json=sorted(set(evidence[reason]))[:100],
                    proposed_diff=(
                        "## Human review proposal\n\n"
                        "Review whether `skills/unknown.md` needs an additional "
                        "safe response path "
                        f"for escalation reason `{reason}`. Preserve handover "
                        "behavior until a human "
                        "authors and validates a code change.\n"
                    ),
                    regression_tests_json=[{"case": reason, "expected_decision": "handover"}],
                    validation_status="aggregate_only",
                    status="pending_review",
                )
                session.add(proposal)
                count += 1
            run.proposal_count = count
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_expires_at = None


def run_worker() -> None:
    from app.db.connection import make_engine, make_session_factory

    settings = Settings()
    worker = SkillReviewWorker(make_session_factory(make_engine(settings)), settings)
    while True:
        if not worker.process_one():
            time.sleep(1)


if __name__ == "__main__":
    run_worker()
