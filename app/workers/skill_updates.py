from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import (
    Conversation,
    SkillCandidateFile,
    SkillCandidateSequence,
    SkillConsolidationBatch,
    SkillUpdateRun,
)
from app.skill_updates.activation import SkillActivationService
from app.skill_updates.batches import SkillBatchService
from app.skill_updates.extraction import SkillUpdateExtractor
from app.skill_updates.merge_reviewer import BedrockSecondReviewer
from app.skill_updates.proposals import BatchProposalService
from app.skill_updates.reviewer import BedrockLearningReviewer
from app.skill_updates.selection import (
    SELECTION_POLICY_VERSION,
    SelectionCursor,
    persist_selection_page,
    select_eligible_conversations,
)
from app.skill_updates.snapshots import (
    ConversationSnapshotBuilder,
    SkillVersionPins,
    SnapshotIneligible,
    delete_snapshot_artifact,
)
from app.skill_updates.storage import CandidateStorage

logger = logging.getLogger("whatsapp_assistant.skill_update_worker")
SOURCE_TYPES = ("buyer", "seller", "portfolio", "general", "unknown")
EXTRACTION_PROCESSING_VERSION = "learning-review-v2"


class SkillUpdateWorker:
    """Scheduled customer extraction and per-type batch pipeline poller."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        storage: CandidateStorage | None = None,
        extractor: SkillUpdateExtractor | None = None,
        batches: SkillBatchService | None = None,
        proposals: BatchProposalService | None = None,
        activation: SkillActivationService | None = None,
        skill_directory: Path = Path("skills"),
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.worker_id = str(uuid.uuid4())
        self.skill_directory = Path(skill_directory)
        self.storage = storage
        self.extractor = extractor
        self.batches = batches
        self.proposals = proposals
        self.activation = activation
        self._last_candidate_inventory_log_at: datetime | None = None

    def process_one(self, *, now: datetime | None = None) -> bool:
        if not self.settings.skill_updates_enabled:
            logger.info("skill_update_worker_idle reason=skill_updates_disabled")
            return False
        logger.debug(
            "skill_update_worker_config extraction_enabled=%s candidate_writes_enabled=%s "
            "consolidation_enabled=%s auto_merge_enabled=%s schedule_minutes=%s batch_size=%s",
            self.settings.skill_update_extraction_enabled,
            self.settings.skill_update_candidate_writes_enabled,
            self.settings.skill_update_consolidation_enabled,
            self.settings.skill_update_auto_merge_enabled,
            self.settings.skill_update_schedule_minutes,
            self.settings.skill_update_batch_size,
        )
        at = self._utc(now or datetime.now(UTC))
        self._ensure_services()
        self._expire_snapshots(at)
        if self._advance_batch(at):
            return True
        if self._advance_run(at):
            return True
        self._enqueue_scheduled_run(at)
        return self._advance_run(at)

    def _ensure_services(self) -> None:
        if self.storage is None:
            self.storage = CandidateStorage.from_settings(self.settings)
        if self.settings.skill_update_extraction_enabled and self.extractor is None:
            self.extractor = SkillUpdateExtractor(
                self.session_factory,
                self.settings,
                self.storage,
                BedrockLearningReviewer(self.settings),
                skill_directory=self.skill_directory,
            )
        if self.settings.skill_update_consolidation_enabled and self.batches is None:
            self.batches = SkillBatchService(self.session_factory, self.settings, self.storage)
            self.proposals = BatchProposalService(
                self.session_factory,
                self.settings,
                self.storage,
                BedrockSecondReviewer(self.settings),
                skill_directory=self.skill_directory,
            )
        if self.settings.skill_update_auto_merge_enabled and self.activation is None:
            self.activation = SkillActivationService(
                self.session_factory,
                self.settings,
                self.storage,
                skill_directory=self.skill_directory,
            )

    def _enqueue_scheduled_run(self, now: datetime) -> None:
        pins = SkillVersionPins.capture(self.skill_directory, self.settings.app_release)
        with self.session_factory.begin() as session:
            latest = session.scalar(
                select(SkillUpdateRun)
                .order_by(SkillUpdateRun.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is not None and latest.created_at:
                created = self._utc(latest.created_at)
                if now - created < timedelta(minutes=self.settings.skill_update_schedule_minutes):
                    return
            run = SkillUpdateRun(
                status="queued",
                selection_json={
                    "policy_version": SELECTION_POLICY_VERSION,
                    "application_release": pins.application_release,
                    "active_skill_hashes": pins.skill_hashes,
                    "reviewer_hash": pins.reviewer_hash,
                    "reviewer_release": pins.reviewer_release,
                    "prompt_schema_version": EXTRACTION_PROCESSING_VERSION,
                    "model_configuration_id": self.settings.bedrock_model_id or "unconfigured",
                    "processed_snapshots": (
                        dict(latest.selection_json.get("processed_snapshots", {}))
                        if latest is not None
                        else {}
                    ),
                },
                application_release=self.settings.app_release,
                reviewer_release=pins.reviewer_release,
            )
            session.add(run)
            session.flush()
            logger.info(
                "skill_update_run_queued run_id=%s schedule_minutes=%s batch_size=%s "
                "extraction_enabled=%s candidate_writes_enabled=%s consolidation_enabled=%s "
                "model_configured=%s min_confidence=%.3f input_limit_bytes=%s",
                run.id,
                self.settings.skill_update_schedule_minutes,
                self.settings.skill_update_batch_size,
                self.settings.skill_update_extraction_enabled,
                self.settings.skill_update_candidate_writes_enabled,
                self.settings.skill_update_consolidation_enabled,
                bool(self.settings.bedrock_model_id),
                self.settings.skill_update_min_confidence,
                self.settings.skill_update_model_input_max_bytes,
            )

    def _advance_run(self, now: datetime) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(
                select(SkillUpdateRun)
                .where(
                    (
                        (SkillUpdateRun.status == "queued")
                        & (
                            SkillUpdateRun.selection_json["retry_after"].as_string().is_(None)
                            | (
                                SkillUpdateRun.selection_json["retry_after"].as_string()
                                <= now.isoformat()
                            )
                        )
                    )
                    | (
                        (SkillUpdateRun.status == "running")
                        & (SkillUpdateRun.lease_expires_at <= now)
                    )
                )
                .order_by(SkillUpdateRun.created_at, SkillUpdateRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return False
            run.status = "running"
            run.started_at = run.started_at or now
            run.lease_owner = self.worker_id
            run.lease_expires_at = now + timedelta(minutes=30)
            run.attempt_count += 1
            run_id = run.id
            state = dict(run.selection_json)
            attempt = run.attempt_count
        logger.info("skill_update_run_started run_id=%s attempt=%s", run_id, attempt)
        try:
            self._process_run_page(run_id, state, now)
        except Exception as exc:
            logger.exception("skill_update_run_error run_id=%s", run_id)
            with self.session_factory.begin() as session:
                run = session.get(SkillUpdateRun, run_id)
                if run is not None:
                    run.error_code = self._reason_code(exc)
                    if run.attempt_count < self.settings.skill_update_max_attempts:
                        delay = min(
                            self.settings.skill_update_retry_initial_seconds
                            * (2 ** max(run.attempt_count - 1, 0)),
                            self.settings.skill_update_retry_max_seconds,
                        )
                        state = dict(run.selection_json)
                        state["retry_after"] = (now + timedelta(seconds=delay)).isoformat()
                        run.selection_json = state
                        run.status = "queued"
                    else:
                        run.status = "failed"
                        run.completed_at = now
                    run.lease_owner = None
                    run.lease_expires_at = None
        return True

    def _process_run_page(self, run_id: uuid.UUID, state: dict[str, Any], now: datetime) -> None:
        if not self.settings.skill_update_extraction_enabled:
            logger.info(
                "skill_update_run_skipped run_id=%s reason=extraction_disabled", run_id
            )
            self._finish_run(run_id, "completed", now)
            return
        builder = ConversationSnapshotBuilder.from_settings(
            self.settings, skill_directory=self.skill_directory
        )
        expected_hashes = state.get("active_skill_hashes")
        expected_reviewer_hash = state.get("reviewer_hash")
        if expected_hashes and expected_hashes != builder.pins.skill_hashes:
            raise RuntimeError("active_skill_pin_changed_during_run")
        if expected_reviewer_hash and expected_reviewer_hash != builder.pins.reviewer_hash:
            raise RuntimeError("reviewer_pin_changed_during_run")
        start = datetime.fromisoformat(state["window_start"]) if state.get("window_start") else None
        end = datetime.fromisoformat(state["window_end"]) if state.get("window_end") else now
        cursor = SelectionCursor.from_json(state.get("cursor_out"))
        with self.session_factory.begin() as session:
            page = select_eligible_conversations(
                session,
                self.settings,
                now=now,
                cursor=cursor,
                window_start=start,
                window_end=end,
                limit=self.settings.skill_update_max_conversations_per_run,
            )
            persist_selection_page(session, run_id, page)
            run = session.get(SkillUpdateRun, run_id)
            if run is not None:
                updated = dict(run.selection_json)
                updated.update(
                    window_start=page.window_start.isoformat(),
                    window_end=page.window_end.isoformat(),
                    cursor_out=page.cursor_out.to_json() if page.cursor_out else None,
                    has_more=page.has_more,
                )
                run.selection_json = updated
            selected = page.conversations
        logger.info(
            "skill_update_selection_page run_id=%s selected=%s active_agent=%s "
            "admin_owned=%s closed=%s "
            "has_more=%s cursor_in=%s cursor_out=%s window_start=%s window_end=%s",
            run_id,
            len(selected),
            sum(item.conversation_state == "active_agent" for item in selected),
            sum(item.conversation_state == "admin_owned" for item in selected),
            sum(item.conversation_state == "closed" for item in selected),
            page.has_more,
            cursor.to_json() if cursor else "none",
            page.cursor_out.to_json() if page.cursor_out else "none",
            page.window_start.isoformat(),
            page.window_end.isoformat(),
        )
        if not selected:
            logger.info(
                "skill_update_selection_empty run_id=%s has_more=%s "
                "window_start=%s window_end=%s",
                run_id,
                page.has_more,
                page.window_start.isoformat(),
                page.window_end.isoformat(),
            )
        assert self.storage is not None and self.extractor is not None
        for item in selected:
            try:
                with self.session_factory() as session:
                    snapshot = builder.build(session, item, now=now)
                logger.debug(
                    "skill_update_snapshot_built run_id=%s conversation_id=%s "
                    "snapshot_id=%s message_count=%s source_type=%s",
                    run_id,
                    item.conversation_id,
                    snapshot.snapshot_id,
                    len(snapshot.messages),
                    snapshot.source_type,
                )
                processed = state.get("processed_snapshots", {}).get(str(item.conversation_id))
                if (
                    isinstance(processed, dict)
                    and processed.get("snapshot_id") == snapshot.snapshot_id
                    and processed.get("processing_version") == EXTRACTION_PROCESSING_VERSION
                ):
                    logger.info(
                        "skill_update_snapshot_skipped run_id=%s conversation_id=%s "
                        "reason=unchanged_snapshot_already_processed snapshot_id=%s",
                        run_id,
                        item.conversation_id,
                        snapshot.snapshot_id,
                    )
                    continue
                result = self.extractor.process(snapshot, run_id, now=now)
                self._record_processed_snapshot(run_id, snapshot, result)
                logger.info(
                    "skill_update_extraction_result run_id=%s conversation_id=%s "
                    "source_type=%s conversation_state=%s status=%s reason_code=%s "
                    "candidate_id=%s relative_path=%s",
                    run_id,
                    item.conversation_id,
                    item.source_type,
                    item.conversation_state,
                    result.status,
                    result.reason_code or "none",
                    result.candidate_id or "none",
                    result.relative_path or "none",
                )
            except SnapshotIneligible as exc:
                logger.info(
                    "skill_update_snapshot_skipped run_id=%s conversation_id=%s "
                    "conversation_state=%s reason=%s",
                    run_id,
                    item.conversation_id,
                    item.conversation_state,
                    self._reason_code(exc),
                )
                self._record_skip(run_id, item.conversation_id, "no_longer_eligible")
            except Exception:
                logger.exception(
                    "skill_update_conversation_failed run_id=%s conversation_id=%s",
                    run_id,
                    item.conversation_id,
                )
                raise
        with self.session_factory.begin() as session:
            run = session.get(SkillUpdateRun, run_id)
            if run is None:
                return
            # A full page with more data remains queued for the next poll; the stable
            # window and cursor were committed before model calls began.
            run.status = "queued" if page.has_more else "completed"
            run.completed_at = None if page.has_more else now
            run.lease_owner = None
            run.lease_expires_at = None
            logger.info(
                "skill_update_run_page_finished run_id=%s status=%s "
                "candidate_count=%s has_more=%s",
                run_id,
                run.status,
                run.candidate_count,
                page.has_more,
            )

    def _advance_batch(self, now: datetime) -> bool:
        if not self.settings.skill_update_consolidation_enabled:
            return False
        assert self.batches is not None
        self._log_candidate_inventory(now)
        batch_id: uuid.UUID | None = None
        status = ""
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(
                    SkillConsolidationBatch.status.in_(
                        {"claimed", "consolidating", "validating", "cleanup_pending", "cleaned"}
                    ),
                    or_(
                        SkillConsolidationBatch.status != "cleaned",
                        SkillConsolidationBatch.validation_json[
                            "work_artifacts_cleaned"
                        ].as_boolean().is_not(True),
                    ),
                    (SkillConsolidationBatch.lease_expires_at.is_(None))
                    | (SkillConsolidationBatch.lease_expires_at <= now),
                )
                .order_by(SkillConsolidationBatch.created_at, SkillConsolidationBatch.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if batch is not None:
                batch.lease_owner = self.worker_id
                batch.lease_expires_at = now + timedelta(minutes=30)
                batch_id, status = batch.id, batch.status
        if batch_id is None:
            logger.debug("skill_update_batch_scan_complete result=no_eligible_batch")
            for source_type in SOURCE_TYPES:
                claim = self.batches.claim_batch(source_type, worker_id=self.worker_id, now=now)
                if claim is not None:
                    batch_id, status = claim.batch_id, "claimed"
                    break
        if batch_id is None:
            return False
        logger.info(
            "skill_update_batch_detected batch_id=%s status=%s action=%s",
            batch_id,
            status,
            "cleanup_work_artifacts" if status == "cleaned" else "process_batch",
        )
        if status == "cleaned":
            self._cleanup_terminal_work(batch_id)
            return True
        try:
            if status == "claimed":
                self.batches.consolidate(batch_id, worker_id=self.worker_id, now=now)
                status = "consolidating"
                logger.info(
                    "skill_update_batch_consolidated batch_id=%s next_status=%s",
                    batch_id,
                    status,
                )
            if status == "consolidating":
                assert self.proposals is not None
                self.proposals.prepare(batch_id, worker_id=self.worker_id, now=now)
                status = "validating"
                logger.info(
                    "skill_update_batch_review_passed batch_id=%s next_status=%s",
                    batch_id,
                    status,
                )
            if status in {"validating", "cleanup_pending"} and (
                self.settings.skill_update_auto_merge_enabled
            ):
                assert self.activation is not None
                result = self.activation.process_batch(
                    batch_id, worker_id=self.worker_id, now=now
                )
                logger.info(
                    "skill_update_batch_activation_finished batch_id=%s status=%s "
                    "active_skill_update_id=%s removed_candidate_count=%s "
                    "removed_candidate_paths=%s",
                    batch_id,
                    result.get("status", "unknown"),
                    result.get("active_skill_update_id", "none"),
                    result.get("removed_count", 0),
                    result.get("removed_paths", []),
                )
                self._cleanup_terminal_work(batch_id)
            elif status == "validating":
                logger.info(
                    "skill_update_batch_waiting_for_merge batch_id=%s "
                    "reason=auto_merge_disabled",
                    batch_id,
                )
        except Exception as exc:
            logger.exception(
                "skill_update_batch_error batch_id=%s error_type=%s error=%s",
                batch_id,
                type(exc).__name__,
                exc,
            )
        return True

    def _log_candidate_inventory(self, now: datetime) -> None:
        """Periodically report which candidate artifacts are known to the batch queue."""
        interval = timedelta(minutes=max(1, self.settings.skill_update_schedule_minutes))
        if (
            self._last_candidate_inventory_log_at is not None
            and now - self._last_candidate_inventory_log_at < interval
        ):
            return
        self._last_candidate_inventory_log_at = now
        self._recover_unregistered_candidate_files()
        with self.session_factory() as session:
            rows = list(
                session.execute(
                    select(
                        SkillCandidateFile.source_conversation_type,
                        SkillCandidateFile.status,
                        func.count(SkillCandidateFile.id),
                    )
                    .where(
                        SkillCandidateFile.candidate_type == "customer_skill",
                    )
                    .group_by(
                        SkillCandidateFile.source_conversation_type,
                        SkillCandidateFile.status,
                    )
                    .order_by(
                        SkillCandidateFile.source_conversation_type,
                        SkillCandidateFile.status,
                    )
                )
            )
            pending_paths = list(
                session.scalars(
                    select(SkillCandidateFile.relative_path)
                    .where(
                        SkillCandidateFile.candidate_type == "customer_skill",
                        SkillCandidateFile.status == "pending",
                    )
                    .order_by(
                        SkillCandidateFile.source_conversation_type,
                        SkillCandidateFile.sequence,
                    )
                    .limit(100)
                )
            )
            registered_paths = set(
                session.scalars(
                    select(SkillCandidateFile.relative_path).where(
                        SkillCandidateFile.candidate_type == "customer_skill",
                    )
                )
            )
            failed_batches = list(
                session.scalars(
                    select(SkillConsolidationBatch)
                    .where(SkillConsolidationBatch.status == "failed")
                    .order_by(SkillConsolidationBatch.updated_at.desc())
                    .limit(10)
                )
            )
        db_paths = {
            item.relative_to(self.storage.root).as_posix()
            for item in (self.storage.root / "candidates").glob("*/*.md")
            if item.parent.name in SOURCE_TYPES
        } if self.storage is not None else set()
        known_paths = set(pending_paths)
        unregistered_paths = sorted(db_paths - known_paths)
        logger.info(
            "skill_update_candidate_inventory batch_size=%s database_counts=%s "
            "pending_paths=%s physical_candidate_file_count=%s "
            "unregistered_or_nonpending_paths=%s missing_physical_files=%s failed_batches=%s",
            self.settings.skill_update_batch_size,
            [
                {"source_type": source_type, "status": status, "count": count}
                for source_type, status, count in rows
            ],
            pending_paths,
            len(db_paths),
            unregistered_paths[:100],
            sorted(registered_paths - db_paths)[:100],
            [self._failed_batch_log_record(batch) for batch in failed_batches],
        )

    def _recover_unregistered_candidate_files(self) -> None:
        """Register validated candidate files discovered in persistent storage."""
        if self.storage is None:
            return
        candidates_root = self.storage.root / "candidates"
        for source_type in SOURCE_TYPES:
            candidate_glob = candidates_root.joinpath(source_type).glob(
                "*_conversation_skill_*.md"
            )
            for path in sorted(candidate_glob):
                match = re.fullmatch(
                    rf"{re.escape(source_type)}_conversation_skill_(\d+)\.md", path.name
                )
                if match is None:
                    continue
                sequence = int(match.group(1))
                try:
                    artifact = self.storage.reconcile_candidate(source_type, sequence, None)
                    if artifact is None:
                        continue
                    content = self.storage.read_candidate(artifact)
                    metadata = self._candidate_front_matter(content)
                    candidate_id = uuid.UUID(metadata["candidate_id"])
                    run_id = uuid.UUID(metadata["source_run_id"])
                    if (
                        metadata.get("candidate_type") != "customer_skill"
                        or metadata.get("source_conversation_type") != source_type
                        or metadata.get("content_fingerprint") is None
                        or not metadata.get("evidence_refs")
                    ):
                        raise ValueError("candidate metadata is incomplete")
                    self._restore_candidate_record(
                        source_type=source_type,
                        sequence=sequence,
                        relative_path=artifact.relative_path,
                        sha256=artifact.sha256,
                        candidate_id=candidate_id,
                        source_run_id=run_id,
                        metadata=metadata,
                        content=content,
                    )
                except Exception as exc:
                    logger.warning(
                        "skill_update_candidate_recovery_skipped path=%s error_type=%s error=%s",
                        path.relative_to(self.storage.root).as_posix(),
                        type(exc).__name__,
                        exc,
                    )

    def _restore_candidate_record(
        self,
        *,
        source_type: str,
        sequence: int,
        relative_path: str,
        sha256: str,
        candidate_id: uuid.UUID,
        source_run_id: uuid.UUID,
        metadata: dict[str, str],
        content: str,
    ) -> None:
        with self.session_factory.begin() as session:
            run = session.get(SkillUpdateRun, source_run_id, with_for_update=True)
            outcomes = dict(run.selection_json.get("extraction_outcomes", {})) if run else {}
            processed_snapshots = (
                dict(run.selection_json.get("processed_snapshots", {})) if run else {}
            )
            matching_conversation_id: uuid.UUID | None = None
            matching_snapshot_id: str | None = None
            for conversation_id, outcome in outcomes.items():
                if not isinstance(outcome, dict):
                    continue
                if (
                    outcome.get("status") == "candidate_created"
                    and outcome.get("candidate_id") == str(candidate_id)
                    and outcome.get("relative_path") == relative_path
                    and outcome.get("sha256") == sha256
                    and outcome.get("fingerprint") == metadata["content_fingerprint"]
                ):
                    try:
                        matching_conversation_id = uuid.UUID(conversation_id)
                    except (TypeError, ValueError):
                        matching_conversation_id = None
                    processed = processed_snapshots.get(conversation_id)
                    if isinstance(processed, dict):
                        value = processed.get("snapshot_id")
                        if isinstance(value, str) and value:
                            matching_snapshot_id = value
                    break
            existing = session.scalar(
                select(SkillCandidateFile)
                .where(SkillCandidateFile.id == candidate_id)
                .with_for_update()
            )
            if existing is not None:
                if (
                    existing.relative_path != relative_path
                    or existing.source_conversation_type != source_type
                    or existing.sequence != sequence
                    or existing.content_fingerprint
                    not in (None, metadata["content_fingerprint"])
                ):
                    logger.error(
                        "skill_update_candidate_recovery_conflict candidate_id=%s "
                        "path=%s reason=database_identity_differs_from_file",
                        candidate_id,
                        relative_path,
                    )
                    return
                if existing.status == "writing":
                    if existing.sha256 not in (None, sha256):
                        logger.error(
                            "skill_update_candidate_recovery_conflict candidate_id=%s "
                            "path=%s reason=database_hash_differs_from_file",
                            candidate_id,
                            relative_path,
                        )
                        return
                    existing.sha256 = sha256
                    existing.status = "pending"
                    existing.evidence_refs_json = [
                        value.strip().strip("\"'")
                        for value in metadata["evidence_refs"].strip("[]").split(",")
                        if value.strip()
                    ]
                    existing.source_skill_version = metadata.get("source_skill_version")
                    existing.content_fingerprint = metadata["content_fingerprint"]
                    existing.run_id = run.id if run is not None else existing.run_id
                    if run is not None:
                        run.candidate_count += 1
                        selection = dict(run.selection_json)
                        outcomes = dict(selection.get("extraction_outcomes", {}))
                        key = str(existing.conversation_id) if existing.conversation_id else None
                        if key is not None:
                            prior = outcomes.get(key)
                            outcome = dict(prior) if isinstance(prior, dict) else {}
                            outcome.update(
                                {
                                    "status": "candidate_created",
                                    "reason_code": None,
                                    "candidate_id": str(candidate_id),
                                    "sequence": sequence,
                                    "relative_path": relative_path,
                                    "sha256": sha256,
                                    "fingerprint": metadata["content_fingerprint"],
                                    "recorded_at": datetime.now(UTC).isoformat(),
                                }
                            )
                            outcomes[key] = outcome
                            selection["extraction_outcomes"] = outcomes
                            run.selection_json = selection
                    logger.info(
                        "skill_update_candidate_recovered candidate_id=%s source_type=%s "
                        "sequence=%s status=pending relative_path=%s "
                        "recovery=completed_interrupted_write",
                        candidate_id,
                        source_type,
                        sequence,
                        relative_path,
                    )
                if not existing.source_snapshot_ids_json and matching_snapshot_id:
                    existing.source_snapshot_ids_json = [matching_snapshot_id]
                    logger.info(
                        "skill_update_candidate_lineage_recovered candidate_id=%s "
                        "relative_path=%s snapshot_id=%s",
                        candidate_id,
                        relative_path,
                        matching_snapshot_id,
                    )
                return
            if matching_conversation_id is not None and session.get(
                Conversation, matching_conversation_id
            ) is None:
                matching_conversation_id = None
            if matching_conversation_id is not None and session.scalar(
                select(SkillCandidateFile.id).where(
                    SkillCandidateFile.conversation_id == matching_conversation_id,
                    SkillCandidateFile.candidate_type == "customer_skill",
                )
            ) is not None:
                logger.info(
                    "skill_update_candidate_recovery_unverified path=%s candidate_id=%s "
                    "reason=source_conversation_already_has_candidate",
                    relative_path,
                    candidate_id,
                )
                return
            if session.scalar(
                select(SkillCandidateFile.id).where(
                    SkillCandidateFile.candidate_type == "customer_skill",
                    SkillCandidateFile.content_fingerprint == metadata["content_fingerprint"],
                    SkillCandidateFile.status != "rejected",
                )
            ) is not None:
                logger.info(
                    "skill_update_candidate_recovery_unverified path=%s candidate_id=%s "
                    "reason=fingerprint_already_registered",
                    relative_path,
                    candidate_id,
                )
                return
            counter = session.get(SkillCandidateSequence, source_type, with_for_update=True)
            if counter is None:
                counter = SkillCandidateSequence(
                    scope=source_type, last_allocated_sequence=sequence
                )
                session.add(counter)
            else:
                counter.last_allocated_sequence = max(counter.last_allocated_sequence, sequence)
            evidence_refs = [
                value.strip().strip("\"'")
                for value in metadata["evidence_refs"].strip("[]").split(",")
                if value.strip()
            ]
            if any(not re.fullmatch(r"ev_[A-Za-z0-9_-]{1,80}", value) for value in evidence_refs):
                raise ValueError("candidate evidence references are invalid")
            cases: list[dict[str, str]] = []
            for case, expected in re.findall(
                r"(?m)^- Case: (.+)\n\s+Expected: (.+)$", content
            ):
                cases.append({"case": case.strip(), "expected_behavior": expected.strip()})
            session.add(
                SkillCandidateFile(
                    id=candidate_id,
                    candidate_type="customer_skill",
                    source_conversation_type=source_type,
                    sequence=sequence,
                    run_id=run.id if run is not None else None,
                    conversation_id=matching_conversation_id,
                    target_skill_key=None,
                    status="pending",
                    relative_path=relative_path,
                    sha256=sha256,
                    content_fingerprint=metadata["content_fingerprint"],
                    evidence_refs_json=evidence_refs,
                    regression_cases_json=cases,
                    source_skill_version=metadata.get("source_skill_version"),
                    source_snapshot_ids_json=[matching_snapshot_id]
                    if matching_snapshot_id
                    else [f"candidate-file:{sha256}"],
                )
            )
            logger.info(
                "skill_update_candidate_recovered candidate_id=%s source_type=%s "
                "sequence=%s status=pending relative_path=%s run_id=%s "
                "conversation_linked=%s",
                candidate_id,
                source_type,
                sequence,
                relative_path,
                run.id if run is not None else "none",
                matching_conversation_id is not None,
            )

    @staticmethod
    def _candidate_front_matter(content: str) -> dict[str, str]:
        match = re.match(r"\A---\s*\n(?P<header>.*?)\n---\s*\n", content, re.S)
        if match is None:
            raise ValueError("candidate front matter is missing")
        values: dict[str, str] = {}
        for line in match.group("header").splitlines():
            key, separator, value = line.partition(":")
            if not separator:
                raise ValueError("candidate front matter is invalid")
            values[key.strip()] = value.strip()
        return {
            key: value.strip().strip("\"'")
            for key, value in values.items()
        }

    @staticmethod
    def _failed_batch_log_record(batch: SkillConsolidationBatch) -> dict[str, Any]:
        validation = dict(batch.validation_json or {})
        second_review = validation.get("second_review")
        retry = validation.get("retry")
        activation_failure = validation.get("activation_failure")
        return {
            "batch_id": str(batch.id),
            "source_type": batch.source_conversation_type,
            "candidate_ids": batch.candidate_ids_json,
            "failure": (
                second_review.get("reason_code") if isinstance(second_review, dict) else None
            )
            or (retry.get("last_error_code") if isinstance(retry, dict) else None)
            or (
                activation_failure.get("error_code")
                if isinstance(activation_failure, dict)
                else None
            ),
        }

    def _cleanup_terminal_work(self, batch_id: uuid.UUID) -> None:
        assert self.storage is not None
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is None or batch.status != "cleaned":
                return
            paths = list(batch.artifact_paths_json)
            hashes: dict[str, str] = {
                f"work/batch-{batch_id}-manifest.json": batch.manifest_sha256
            }
            validation = dict(batch.validation_json)
            consolidated = validation.get("consolidated_artifacts", [])
            if isinstance(consolidated, list):
                for item in consolidated:
                    if isinstance(item, dict) and item.get("relative_path") and item.get("sha256"):
                        hashes[str(item["relative_path"])] = str(item["sha256"])
            review = validation.get("second_review", {})
            proposals = review.get("proposal_artifacts", {}) if isinstance(review, dict) else {}
            if isinstance(proposals, dict):
                for item in proposals.values():
                    if isinstance(item, dict) and item.get("relative_path") and item.get("sha256"):
                        hashes[str(item["relative_path"])] = str(item["sha256"])
        failed = False
        for relative in paths:
            digest = hashes.get(str(relative))
            if digest is None or not str(relative).startswith("work/"):
                continue
            try:
                self.storage.delete_work_artifact(Path(str(relative)).name, digest)
            except Exception:
                failed = True
                logger.exception("skill_batch_work_cleanup_error batch_id=%s", batch_id)
        if not failed:
            with self.session_factory.begin() as session:
                batch = session.get(SkillConsolidationBatch, batch_id)
                if batch is not None and batch.status == "cleaned":
                    batch.validation_json = {
                        **dict(batch.validation_json),
                        "work_artifacts_cleaned": True,
                    }
                    logger.info(
                        "skill_update_batch_work_cleanup_finished batch_id=%s "
                        "removed_work_artifact_count=%s",
                        batch_id,
                        len(paths),
                    )

    def _expire_snapshots(self, now: datetime) -> None:
        if self.storage is None:
            return
        with self.session_factory() as session:
            runs = list(session.scalars(select(SkillUpdateRun)))
            expired: list[tuple[uuid.UUID, str]] = []
            for run in runs:
                for item in run.selection_json.get("snapshot_artifacts", []):
                    try:
                        expires = datetime.fromisoformat(str(item["expires_at"]))
                        if self._utc(expires) <= now:
                            expired.append((run.id, str(item["snapshot_id"])))
                            delete_snapshot_artifact(self.storage, item, now=now)
                    except (KeyError, TypeError, ValueError, OSError):
                        logger.exception("skill_snapshot_cleanup_error run_id=%s", run.id)
        if expired:
            with self.session_factory.begin() as session:
                for run_id, snapshot_id in expired:
                    stored_run = session.get(SkillUpdateRun, run_id)
                    if stored_run is None:
                        continue
                    state = dict(stored_run.selection_json)
                    state["snapshot_artifacts"] = [
                        item
                        for item in state.get("snapshot_artifacts", [])
                        if str(item.get("snapshot_id")) != snapshot_id
                    ]
                    stored_run.selection_json = state

    def _record_skip(self, run_id: uuid.UUID, conversation_id: uuid.UUID, code: str) -> None:
        with self.session_factory.begin() as session:
            run = session.get(SkillUpdateRun, run_id)
            if run is not None:
                state = dict(run.selection_json)
                outcomes = dict(state.get("extraction_outcomes", {}))
                outcomes[str(conversation_id)] = {"status": "skipped", "reason_code": code}
                state["extraction_outcomes"] = outcomes
                run.selection_json = state

    def _record_processed_snapshot(self, run_id: uuid.UUID, snapshot: Any, result: Any) -> None:
        with self.session_factory.begin() as session:
            run = session.get(SkillUpdateRun, run_id)
            if run is None:
                return
            state = dict(run.selection_json)
            processed = dict(state.get("processed_snapshots", {}))
            processed[str(snapshot.conversation_id)] = {
                "snapshot_id": snapshot.snapshot_id,
                "processing_version": EXTRACTION_PROCESSING_VERSION,
                "status": result.status,
                "reason_code": result.reason_code,
            }
            state["processed_snapshots"] = processed
            run.selection_json = state

    def _finish_run(self, run_id: uuid.UUID, status: str, now: datetime) -> None:
        with self.session_factory.begin() as session:
            run = session.get(SkillUpdateRun, run_id)
            if run is not None:
                run.status = status
                run.completed_at = now
                run.lease_owner = None
                run.lease_expires_at = None

    @staticmethod
    def _reason_code(error: Exception) -> str:
        return type(error).__name__.casefold()[:120] or "skill_update_failure"

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def run_worker() -> None:
    from app.db.connection import make_engine, make_session_factory

    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(message)s")
    logger.info(
        "skill_update_worker_started enabled=%s extraction=%s candidate_writes=%s "
        "consolidation=%s auto_merge=%s schedule_minutes=%s batch_size=%s "
        "batch_max_wait_minutes=%s storage_configured=%s",
        settings.skill_updates_enabled,
        settings.skill_update_extraction_enabled,
        settings.skill_update_candidate_writes_enabled,
        settings.skill_update_consolidation_enabled,
        settings.skill_update_auto_merge_enabled,
        settings.skill_update_schedule_minutes,
        settings.skill_update_batch_size,
        settings.skill_update_batch_max_wait_minutes,
        settings.skill_update_storage_dir is not None,
    )
    worker = SkillUpdateWorker(make_session_factory(make_engine(settings)), settings)
    while True:
        try:
            if not worker.process_one():
                time.sleep(1)
        except Exception:
            logger.exception("skill_update_worker_poll_error")
            time.sleep(2)


if __name__ == "__main__":
    run_worker()
