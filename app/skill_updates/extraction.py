from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import (
    Contact,
    Conversation,
    Message,
    PropertyRequirement,
    SkillCandidateFile,
    SkillCandidateSequence,
    SkillUpdateRun,
)
from app.skill_updates.deidentify import deidentify_text
from app.skill_updates.reviewer import LearningReviewerPort
from app.skill_updates.schemas import (
    ExistingLearning,
    LearningReviewOutput,
    LearningReviewRequest,
)
from app.skill_updates.selection import source_type_for_intent_or_requirement
from app.skill_updates.snapshots import ConversationSnapshot
from app.skill_updates.storage import (
    ArtifactIntegrityError,
    ArtifactStorageError,
    CandidateManifestEntry,
    CandidateStorage,
)
from app.skills.loader import ACTIVE_SKILL_MANIFEST, load_skills

_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_CLAIM_PATTERNS = (
    re.compile(r"\bwe\s+(?:guarantee|can guarantee|currently offer)\b", re.I),
    re.compile(r"\bwe\s+have\s+(?:a|an|the)\s+.{0,100}\bavailable\b", re.I),
    re.compile(r"\b(?:this|the) property\s+(?:is|will be)\s+available\b", re.I),
    re.compile(r"\bguaranteed\s+(?:returns?|profit|appreciation)\b", re.I),
    re.compile(r"\bwe\s+recommend\s+(?:this|the)\s+(?:property|investment)\b", re.I),
    re.compile(r"\b(?:legal|tax|financial|mortgage) advice\s+(?:is|will be)\s+provided\b", re.I),
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "if",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "when",
        "with",
        "you",
        "your",
    }
)
_LEARNING_SECTION = re.compile(r"(?ms)^## Learning\s*\n(.*?)(?=^## Regression cases\s*$)")
logger = logging.getLogger("whatsapp_assistant.skill_update_extraction")


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    reason_code: str | None = None
    candidate_id: uuid.UUID | None = None
    relative_path: str | None = None
    sha256: str | None = None
    fingerprint: str | None = None


def normalized_learning(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_WORD.findall(normalized))


def learning_fingerprint(lesson: str, trigger: str, instruction: str) -> str:
    normalized = normalized_learning(" ".join((lesson, trigger, instruction)))
    return hashlib.sha256(normalized.encode()).hexdigest()


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in normalized_learning(value).split()
        if len(token) > 2 and token not in _STOP_WORDS
    }


def _is_semantic_duplicate(proposed: str, existing: str) -> bool:
    proposed_normalized = normalized_learning(proposed)
    existing_normalized = normalized_learning(existing)
    if not proposed_normalized or not existing_normalized:
        return False
    if proposed_normalized == existing_normalized:
        return True
    if SequenceMatcher(None, proposed_normalized, existing_normalized).ratio() >= 0.92:
        return True
    proposed_tokens = _tokens(proposed)
    existing_tokens = _tokens(existing)
    if not proposed_tokens or not existing_tokens:
        return False
    overlap = len(proposed_tokens & existing_tokens) / len(proposed_tokens | existing_tokens)
    return overlap >= 0.88


def _candidate_learning(markdown: str) -> str:
    match = _LEARNING_SECTION.search(markdown)
    return match.group(1).strip() if match else markdown



def _contains_personal_data(values: list[str]) -> bool:
    for value in values:
        redacted = deidentify_text(value)
        if redacted != value or _EMAIL.search(value) or _URL.search(value):
            return True
    return False


def _contains_unsupported_claim(values: list[str]) -> bool:
    return any(pattern.search(value) for value in values for pattern in _CLAIM_PATTERNS)


def _single_line(value: str) -> bool:
    return "\n" not in value and "\r" not in value and "\x00" not in value


def _markdown_inline(value: str) -> str:
    escaped = html.escape(value, quote=False)
    for char in ("\\", "`", "*", "_", "[", "]", "#", ">", "|"):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


class SkillUpdateExtractor:
    """First-review extraction and durable candidate creation, disabled until invoked."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        storage: CandidateStorage,
        reviewer: LearningReviewerPort,
        *,
        skill_directory: Path = Path("skills"),
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.storage = storage
        self.reviewer = reviewer
        self.skill_directory = Path(skill_directory)

    def process(
        self, snapshot: ConversationSnapshot, run_id: uuid.UUID, *, now: datetime | None = None
    ) -> ExtractionResult:
        if (
            not self.settings.skill_updates_enabled
            or not self.settings.skill_update_extraction_enabled
        ):
            raise RuntimeError("skill_update_extraction_disabled")
        checked_at = now or datetime.now(UTC)
        run, snapshot_metadata = self._load_run_and_snapshot(snapshot, run_id, checked_at)
        if isinstance(run, ExtractionResult):
            return run
        if isinstance(snapshot_metadata, ExtractionResult):
            return snapshot_metadata

        logger.info(
            "skill_candidate_review_started run_id=%s conversation_id=%s snapshot_id=%s "
            "source_type=%s state=%s message_count=%s input_limit_bytes=%s output_limit_bytes=%s",
            run_id,
            snapshot.conversation_id,
            snapshot.snapshot_id,
            snapshot.source_type,
            snapshot.conversation_state,
            len(snapshot.messages),
            self.settings.skill_update_model_input_max_bytes,
            self.settings.skill_update_model_output_max_bytes,
        )

        existing = self._candidate_for_conversation(snapshot.conversation_id)
        if existing is not None:
            logger.info(
                "skill_candidate_review_blocked run_id=%s conversation_id=%s "
                "reason=candidate_already_exists candidate_id=%s candidate_status=%s",
                run_id,
                snapshot.conversation_id,
                existing.id,
                existing.status,
            )
            recovered = self._recover_writing_candidate(existing, snapshot, run_id)
            if recovered is not None:
                return recovered
            self._record_outcome(
                run_id,
                snapshot.conversation_id,
                "already_candidate",
                "candidate_already_exists",
            )
            return ExtractionResult("already_candidate", "candidate_already_exists")

        try:
            active_skills = load_skills(self.skill_directory)
            current_hashes = {
                key: hashlib.sha256((self.skill_directory / filename).read_bytes()).hexdigest()
                for key, filename in ACTIVE_SKILL_MANIFEST.items()
            }
        except Exception as exc:
            logger.exception(
                "skill_update_model_review_failed run_id=%s conversation_id=%s "
                "error_type=%s error=%s",
                run_id,
                snapshot.conversation_id,
                type(exc).__name__,
                str(exc)[:500],
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "rejected", "active_skill_unavailable"
            )
            return ExtractionResult("rejected", "active_skill_unavailable")
        if current_hashes != snapshot.pins.skill_hashes:
            logger.warning(
                "skill_candidate_review_blocked run_id=%s conversation_id=%s "
                "reason=active_skill_version_changed",
                run_id,
                snapshot.conversation_id,
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "rejected", "active_skill_version_changed"
            )
            return ExtractionResult("rejected", "active_skill_version_changed")

        existing_learnings = [
            ExistingLearning(source_type=source_type, status="active", text=skill.instructions)
            for source_type, skill in active_skills.items()
            if source_type != "skill_reviewer"
        ]
        request = LearningReviewRequest(
            source_conversation_type=snapshot.source_type,
            snapshot=snapshot.payload(),
            active_skills={
                key: skill.instructions
                for key, skill in active_skills.items()
                if key != "skill_reviewer"
            },
            existing_learnings=existing_learnings,
        )
        request_json = json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
        logger.info(
            "skill_candidate_reviewer_request run_id=%s conversation_id=%s "
            "request_bytes=%s message_count=%s active_skill_count=%s",
            run_id,
            snapshot.conversation_id,
            len(request_json.encode()),
            len(snapshot.messages),
            len(existing_learnings),
        )
        if len(request_json.encode()) > self.settings.skill_update_model_input_max_bytes:
            logger.warning(
                "skill_candidate_review_blocked run_id=%s conversation_id=%s "
                "reason=model_input_too_large request_bytes=%s max_bytes=%s",
                run_id,
                snapshot.conversation_id,
                len(request_json.encode()),
                self.settings.skill_update_model_input_max_bytes,
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "rejected", "model_input_too_large"
            )
            return ExtractionResult("rejected", "model_input_too_large")
        try:
            raw_output = self.reviewer.review(request)
            output = self._parse_output(raw_output)
            logger.info(
                "skill_candidate_reviewer_response run_id=%s conversation_id=%s "
                "status=%s confidence=%.3f novelty=%s policy_conflict_count=%s "
                "evidence_count=%s regression_case_count=%s",
                run_id,
                snapshot.conversation_id,
                output.status,
                output.confidence,
                output.duplicate_or_novel,
                len(output.policy_conflicts),
                len(output.evidence_refs),
                len(output.regression_cases),
            )
        except Exception as exc:
            logger.exception(
                "skill_update_model_review_failed run_id=%s conversation_id=%s "
                "error_type=%s error=%s",
                run_id,
                snapshot.conversation_id,
                type(exc).__name__,
                str(exc)[:500],
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "rejected", "invalid_or_unavailable_model_output"
            )
            return ExtractionResult("rejected", "invalid_or_unavailable_model_output")

        outcome = self._validate_output(output, snapshot)
        if outcome is not None:
            status, reason = outcome
            logger.info(
                "skill_candidate_review_rejected run_id=%s conversation_id=%s "
                "status=%s reason=%s confidence=%.3f min_confidence=%.3f",
                run_id,
                snapshot.conversation_id,
                status,
                reason,
                output.confidence,
                self.settings.skill_update_min_confidence,
            )
            self._record_outcome(run_id, snapshot.conversation_id, status, reason)
            return ExtractionResult(status, reason)
        if output.status == "no_learning":
            logger.info(
                "skill_candidate_review_no_learning run_id=%s conversation_id=%s",
                run_id,
                snapshot.conversation_id,
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "no_candidate", "no_reusable_learning"
            )
            return ExtractionResult("no_candidate", "no_reusable_learning")
        assert output.lesson and output.trigger and output.instruction
        learning_text = " ".join((output.trigger, output.lesson, output.instruction))
        current_bodies = [
            skill.instructions for key, skill in active_skills.items() if key != "skill_reviewer"
        ]
        if any(_is_semantic_duplicate(learning_text, body) for body in current_bodies):
            logger.info(
                "skill_candidate_duplicate run_id=%s conversation_id=%s "
                "reason=duplicate_active_skill",
                run_id,
                snapshot.conversation_id,
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "duplicate", "duplicate_active_skill"
            )
            return ExtractionResult("duplicate", "duplicate_active_skill")

        fingerprint = learning_fingerprint(output.lesson, output.trigger, output.instruction)
        if self._fingerprint_exists(fingerprint):
            logger.info(
                "skill_candidate_duplicate run_id=%s conversation_id=%s "
                "reason=duplicate_fingerprint fingerprint=%s",
                run_id,
                snapshot.conversation_id,
                fingerprint,
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "duplicate", "duplicate_fingerprint"
            )
            return ExtractionResult("duplicate", "duplicate_fingerprint", fingerprint=fingerprint)

        if not self.settings.skill_update_candidate_writes_enabled:
            logger.warning(
                "skill_candidate_not_written run_id=%s conversation_id=%s "
                "reason=candidate_writes_disabled fingerprint=%s",
                run_id,
                snapshot.conversation_id,
                fingerprint,
            )
            self._record_outcome(
                run_id,
                snapshot.conversation_id,
                "shadow_candidate",
                "candidate_writes_disabled",
                fingerprint=fingerprint,
                confidence=output.confidence,
                evidence_refs=output.evidence_refs,
            )
            return ExtractionResult(
                "shadow_candidate", "candidate_writes_disabled", fingerprint=fingerprint
            )

        try:
            candidate_id, sequence, existing_writing = self._reserve_candidate(
                snapshot, run_id, output, fingerprint
            )
        except IntegrityError:
            existing_candidate = self._candidate_for_conversation(snapshot.conversation_id)
            if existing_candidate is not None:
                self._record_outcome(
                    run_id,
                    snapshot.conversation_id,
                    "already_candidate",
                    "candidate_already_exists",
                )
                return ExtractionResult("already_candidate", "candidate_already_exists")
            if self._fingerprint_exists(fingerprint):
                self._record_outcome(
                    run_id, snapshot.conversation_id, "duplicate", "duplicate_fingerprint"
                )
                return ExtractionResult("duplicate", "duplicate_fingerprint")
            self._record_outcome(
                run_id, snapshot.conversation_id, "failed", "candidate_reservation_conflict"
            )
            return ExtractionResult("failed", "candidate_reservation_conflict")
        if isinstance(candidate_id, ExtractionResult):
            return candidate_id
        if sequence is None:
            return ExtractionResult("failed", "candidate_sequence_missing", candidate_id)
        markdown = self._candidate_markdown(candidate_id, sequence, snapshot, run_id, output)
        try:
            artifact = (
                self.storage.reconcile_candidate(snapshot.source_type, sequence, None)
                if existing_writing
                else None
            )
            if artifact is not None:
                recovered_content = self.storage.read_candidate(artifact)
                if recovered_content != markdown:
                    raise ArtifactIntegrityError(
                        "recovered candidate differs from reserved content"
                    )
            if artifact is None:
                artifact = self.storage.write_candidate(snapshot.source_type, sequence, markdown)
        except (ArtifactStorageError, ArtifactIntegrityError):
            logger.exception(
                "skill_candidate_file_write_failed run_id=%s conversation_id=%s "
                "candidate_id=%s sequence=%s",
                run_id,
                snapshot.conversation_id,
                candidate_id,
                sequence,
            )
            self._record_outcome(
                run_id, snapshot.conversation_id, "failed", "candidate_file_write_failed"
            )
            return ExtractionResult(
                "failed", "candidate_file_write_failed", candidate_id, fingerprint=fingerprint
            )
        result = self._finalize_candidate(candidate_id, run_id, snapshot, artifact, fingerprint)
        logger.info(
            "skill_candidate_file_result run_id=%s conversation_id=%s status=%s "
            "candidate_id=%s relative_path=%s fingerprint=%s",
            run_id,
            snapshot.conversation_id,
            result.status,
            result.candidate_id or "none",
            result.relative_path or artifact.relative_path,
            fingerprint,
        )
        return result

    def _load_run_and_snapshot(
        self, snapshot: ConversationSnapshot, run_id: uuid.UUID, now: datetime
    ) -> tuple[SkillUpdateRun | ExtractionResult, dict[str, Any] | ExtractionResult | None]:
        with self.session_factory() as session:
            run = session.get(SkillUpdateRun, run_id)
            if run is None or run.status != "running":
                return ExtractionResult("rejected", "skill_update_run_not_running"), {}
            if (
                run.application_release != snapshot.pins.application_release
                or run.reviewer_release != snapshot.pins.reviewer_release
            ):
                return run, ExtractionResult("rejected", "run_version_pin_mismatch")
            # The snapshot is built directly from the database for this extraction.
            # A persisted JSON artifact is optional and is not required to create candidates.
            return run, None

    def _candidate_for_conversation(self, conversation_id: uuid.UUID) -> SkillCandidateFile | None:
        with self.session_factory() as session:
            return session.scalar(
                select(SkillCandidateFile)
                .where(
                    SkillCandidateFile.candidate_type == "customer_skill",
                    SkillCandidateFile.conversation_id == conversation_id,
                )
                .limit(1)
            )

    def _recover_writing_candidate(
        self,
        candidate: SkillCandidateFile,
        snapshot: ConversationSnapshot,
        run_id: uuid.UUID,
    ) -> ExtractionResult | None:
        if candidate.status != "writing" or candidate.run_id != run_id:
            return None
        try:
            entry = self.storage.reconcile_candidate(
                str(candidate.source_conversation_type), candidate.sequence, candidate.sha256
            )
        except (ArtifactStorageError, ArtifactIntegrityError):
            return ExtractionResult("failed", "candidate_reconciliation_failed", candidate.id)
        if entry is None:
            return None
        try:
            content = self.storage.read_candidate(entry)
        except (ArtifactStorageError, ArtifactIntegrityError):
            return ExtractionResult("failed", "candidate_reconciliation_failed", candidate.id)
        required_markers = (
            f'candidate_id: "{candidate.id}"',
            f'source_run_id: "{run_id}"',
            f'content_fingerprint: "{candidate.content_fingerprint}"',
        )
        if candidate.content_fingerprint is None or not all(
            marker in content.split("---", 2)[1] for marker in required_markers
        ):
            return ExtractionResult("failed", "candidate_reconciliation_mismatch", candidate.id)
        return self._finalize_candidate(
            candidate.id,
            run_id,
            snapshot,
            entry,
            candidate.content_fingerprint or "",
        )

    def _parse_output(self, raw: object) -> LearningReviewOutput:
        if isinstance(raw, LearningReviewOutput):
            output_bytes = raw.model_dump_json().encode()
            if len(output_bytes) > self.settings.skill_update_model_output_max_bytes:
                raise ValueError("skill_review_output_too_large")
            return raw
        if isinstance(raw, str):
            if len(raw.encode()) > self.settings.skill_update_model_output_max_bytes:
                raise ValueError("skill_review_output_too_large")
            return LearningReviewOutput.model_validate_json(raw)
        if isinstance(raw, dict):
            encoded = json.dumps(raw, ensure_ascii=False)
            if len(encoded.encode()) > self.settings.skill_update_model_output_max_bytes:
                raise ValueError("skill_review_output_too_large")
            return LearningReviewOutput.model_validate(raw)
        raise ValueError("invalid_learning_review_output")

    def _validate_output(
        self, output: LearningReviewOutput, snapshot: ConversationSnapshot
    ) -> tuple[str, str] | None:
        if output.source_conversation_type != snapshot.source_type:
            return "rejected", "source_type_mismatch"
        if output.status == "no_learning":
            if output.policy_conflicts:
                return "rejected", "policy_conflict"
            return None
        if output.confidence < self.settings.skill_update_min_confidence:
            return "rejected", "confidence_below_threshold"
        if output.duplicate_or_novel == "duplicate":
            return "duplicate", "reviewer_marked_duplicate"
        if output.duplicate_or_novel != "novel":
            return "rejected", "novelty_not_supported"
        if output.policy_conflicts:
            return "rejected", "policy_conflict"
        snapshot_refs = {message.message_ref for message in snapshot.messages}
        if not set(output.evidence_refs).issubset(snapshot_refs):
            return "rejected", "unknown_evidence_reference"
        values = [output.lesson or "", output.trigger or "", output.instruction or ""]
        for case in output.regression_cases:
            values.extend((case.case, case.expected_behavior))
        if any(not _single_line(value) for value in values):
            return "rejected", "malformed_regression_case"
        if _contains_personal_data(values):
            return "rejected", "personal_data_in_learning"
        if _contains_unsupported_claim(values):
            return "rejected", "unsupported_property_or_advice_claim"
        return None

    def _fingerprint_exists(self, fingerprint: str) -> bool:
        with self.session_factory() as session:
            return (
                session.scalar(
                    select(SkillCandidateFile.id)
                    .where(
                        SkillCandidateFile.candidate_type == "customer_skill",
                        SkillCandidateFile.content_fingerprint == fingerprint,
                        SkillCandidateFile.status != "rejected",
                    )
                    .limit(1)
                )
                is not None
            )

    def _reserve_candidate(
        self,
        snapshot: ConversationSnapshot,
        run_id: uuid.UUID,
        output: LearningReviewOutput,
        fingerprint: str,
    ) -> tuple[uuid.UUID | ExtractionResult, int | None, bool]:
        source_type = snapshot.source_type
        skill_folder = "fallback" if source_type == "unknown" else source_type
        relative_path = f"{skill_folder}/candidate_skills/{source_type}_conversation_skill_"
        with self.session_factory.begin() as session:
            eligible = session.execute(
                select(Conversation, Contact)
                .join(Contact, Contact.id == Conversation.contact_id)
                .where(
                    Conversation.id == snapshot.conversation_id,
                    Conversation.state.in_({"closed", "active_agent"}),
                    Contact.retention_state == "active",
                    Contact.legal_hold.is_(False),
                )
                .with_for_update()
            ).one_or_none()
            if eligible is None:
                self._record_outcome_in_session(
                    session, run_id, snapshot.conversation_id, "rejected", "eligibility_changed"
                )
                return ExtractionResult("rejected", "eligibility_changed"), None, False
            conversation, _contact = eligible
            transaction_type = session.scalar(
                select(PropertyRequirement.transaction_type)
                .where(
                    PropertyRequirement.conversation_id == conversation.id,
                    PropertyRequirement.status == "active",
                )
                .order_by(PropertyRequirement.updated_at.desc())
                .limit(1)
            )
            agent_skill_key = session.scalar(
                select(Message.skill_key)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type == "automated_agent",
                    Message.skill_key.is_not(None),
                    Message.created_at <= datetime.fromisoformat(snapshot.snapshot_cutoff_at),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
            if (
                source_type_for_intent_or_requirement(
                    conversation.active_intent, transaction_type, agent_skill_key
                )
                != source_type
            ):
                self._record_outcome_in_session(
                    session, run_id, snapshot.conversation_id, "rejected", "intent_changed"
                )
                return ExtractionResult("rejected", "intent_changed"), None, False

            candidate = session.scalar(
                select(SkillCandidateFile)
                .where(
                    SkillCandidateFile.candidate_type == "customer_skill",
                    SkillCandidateFile.conversation_id == snapshot.conversation_id,
                )
                .with_for_update()
            )
            if candidate is not None:
                if candidate.status == "writing" and candidate.run_id == run_id:
                    if candidate.content_fingerprint != fingerprint:
                        self._record_outcome_in_session(
                            session,
                            run_id,
                            snapshot.conversation_id,
                            "rejected",
                            "writing_candidate_fingerprint_mismatch",
                        )
                        return (
                            ExtractionResult("rejected", "writing_candidate_fingerprint_mismatch"),
                            None,
                            False,
                        )
                    candidate_id, sequence, existing_writing = (
                        candidate.id,
                        candidate.sequence,
                        True,
                    )
                else:
                    self._record_outcome_in_session(
                        session,
                        run_id,
                        snapshot.conversation_id,
                        "already_candidate",
                        "candidate_already_exists",
                    )
                    return (
                        ExtractionResult("already_candidate", "candidate_already_exists"),
                        None,
                        False,
                    )
            else:
                if (
                    session.scalar(
                        select(SkillCandidateFile.id)
                        .where(
                            SkillCandidateFile.candidate_type == "customer_skill",
                            SkillCandidateFile.content_fingerprint == fingerprint,
                            SkillCandidateFile.status != "rejected",
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    self._record_outcome_in_session(
                        session,
                        run_id,
                        snapshot.conversation_id,
                        "duplicate",
                        "duplicate_fingerprint",
                    )
                    return (
                        ExtractionResult(
                            "duplicate", "duplicate_fingerprint", fingerprint=fingerprint
                        ),
                        None,
                        False,
                    )
                bind = session.get_bind()
                if bind.dialect.name == "postgresql":
                    insert_statement: Any = pg_insert(SkillCandidateSequence)
                elif bind.dialect.name == "sqlite":
                    insert_statement = sqlite_insert(SkillCandidateSequence)
                else:
                    raise RuntimeError(
                        "candidate sequence allocation requires PostgreSQL or SQLite"
                    )
                session.execute(
                    insert_statement.values(
                        scope=source_type, last_allocated_sequence=0
                    ).on_conflict_do_nothing(index_elements=["scope"])
                )
                counter = session.scalar(
                    select(SkillCandidateSequence)
                    .where(SkillCandidateSequence.scope == source_type)
                    .with_for_update()
                )
                if counter is None:
                    raise RuntimeError("candidate sequence row could not be allocated")
                counter.last_allocated_sequence += 1
                sequence = counter.last_allocated_sequence
                candidate_id = uuid.uuid4()
                source_hash = snapshot.pins.skill_hashes[source_type]
                candidate = SkillCandidateFile(
                    id=candidate_id,
                    candidate_type="customer_skill",
                    source_conversation_type=source_type,
                    sequence=sequence,
                    run_id=run_id,
                    conversation_id=snapshot.conversation_id,
                    target_skill_key=None,
                    status="writing",
                    relative_path=f"{relative_path}{sequence}.md",
                    sha256=None,
                    content_fingerprint=fingerprint,
                    evidence_refs_json=output.evidence_refs,
                    regression_cases_json=[case.model_dump() for case in output.regression_cases],
                    source_skill_version=source_hash,
                    source_snapshot_ids_json=[snapshot.snapshot_id],
                )
                session.add(candidate)
                existing_writing = False
            self._record_outcome_in_session(
                session,
                run_id,
                snapshot.conversation_id,
                "candidate_writing",
                None,
                candidate_id=candidate_id,
                sequence=sequence,
                fingerprint=fingerprint,
            )
        return candidate_id, sequence, existing_writing

    def _finalize_candidate(
        self,
        candidate_id: uuid.UUID,
        run_id: uuid.UUID,
        snapshot: ConversationSnapshot,
        artifact: CandidateManifestEntry,
        fingerprint: str,
    ) -> ExtractionResult:
        with self.session_factory.begin() as session:
            candidate = session.scalar(
                select(SkillCandidateFile)
                .where(SkillCandidateFile.id == candidate_id)
                .with_for_update()
            )
            run = session.scalar(
                select(SkillUpdateRun).where(SkillUpdateRun.id == run_id).with_for_update()
            )
            if candidate is None or run is None:
                return ExtractionResult("failed", "candidate_metadata_missing", candidate_id)
            if candidate.status == "pending":
                return ExtractionResult(
                    "candidate_created",
                    candidate_id=candidate.id,
                    relative_path=candidate.relative_path,
                    sha256=candidate.sha256,
                    fingerprint=candidate.content_fingerprint,
                )
            if candidate.status != "writing" or candidate.run_id != run_id:
                return ExtractionResult("failed", "candidate_state_conflict", candidate_id)
            if candidate.relative_path != artifact.relative_path:
                return ExtractionResult("failed", "candidate_path_mismatch", candidate_id)
            candidate.sha256 = artifact.sha256
            candidate.status = "pending"
            run.candidate_count += 1
            self._record_outcome_in_session(
                session,
                run_id,
                snapshot.conversation_id,
                "candidate_created",
                None,
                candidate_id=candidate.id,
                relative_path=candidate.relative_path,
                sha256=artifact.sha256,
                fingerprint=fingerprint or candidate.content_fingerprint,
            )
            return ExtractionResult(
                "candidate_created",
                candidate_id=candidate.id,
                relative_path=candidate.relative_path,
                sha256=artifact.sha256,
                fingerprint=fingerprint or candidate.content_fingerprint,
            )

    def _candidate_markdown(
        self,
        candidate_id: uuid.UUID,
        sequence: int,
        snapshot: ConversationSnapshot,
        run_id: uuid.UUID,
        output: LearningReviewOutput,
    ) -> str:
        assert output.lesson and output.trigger and output.instruction
        source_hash = snapshot.pins.skill_hashes[snapshot.source_type]
        evidence = ", ".join(f'"{ref}"' for ref in output.evidence_refs)
        confidence = f"{output.confidence:.2f}"
        learning = (
            f"Trigger: {_markdown_inline(output.trigger)}\n\n"
            f"Lesson: {_markdown_inline(output.lesson)}\n\n"
            f"Instruction: {_markdown_inline(output.instruction)}"
        )
        cases = "\n".join(
            f"- Case: {_markdown_inline(case.case)}\n"
            f"  Expected: {_markdown_inline(case.expected_behavior)}"
            for case in output.regression_cases
        )
        fingerprint = learning_fingerprint(output.lesson, output.trigger, output.instruction)
        return (
            "---\n"
            f'candidate_id: "{candidate_id}"\n'
            "candidate_type: customer_skill\n"
            f"source_conversation_type: {snapshot.source_type}\n"
            "target_skill_key: null\n"
            f'source_run_id: "{run_id}"\n'
            f'source_skill_version: "{source_hash}"\n'
            f'content_fingerprint: "{fingerprint}"\n'
            f"confidence: {confidence}\n"
            f"evidence_refs: [{evidence}]\n"
            "---\n\n"
            "## Learning\n\n"
            f"{learning}\n\n"
            "## Regression cases\n\n"
            f"{cases}\n"
        )

    def _record_outcome(
        self,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        status: str,
        reason_code: str | None,
        **details: Any,
    ) -> None:
        with self.session_factory.begin() as session:
            self._record_outcome_in_session(
                session, run_id, conversation_id, status, reason_code, **details
            )

    @staticmethod
    def _record_outcome_in_session(
        session: Session,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        status: str,
        reason_code: str | None,
        **details: Any,
    ) -> None:
        run = session.scalar(
            select(SkillUpdateRun).where(SkillUpdateRun.id == run_id).with_for_update()
        )
        if run is None:
            return
        selection = dict(run.selection_json)
        outcomes = dict(selection.get("extraction_outcomes", {}))
        outcomes[str(conversation_id)] = {
            "status": status,
            "reason_code": reason_code,
            "recorded_at": datetime.now(UTC).isoformat(),
            **json.loads(json.dumps(details, sort_keys=True, default=str)),
        }
        selection["extraction_schema_version"] = "learning-review-v2"
        outcomes_json = json.dumps(outcomes, sort_keys=True, default=str)
        if len(outcomes_json.encode()) > 1_000_000:
            # Keep the run record bounded while retaining the newest outcomes.
            outcomes = dict(list(outcomes.items())[-500:])
            selection["extraction_outcomes_truncated"] = True
        selection["extraction_outcomes"] = outcomes
        run.selection_json = selection
