from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import random
import re
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import (
    ReviewerUpdateRun,
    SkillActiveUpdate,
    SkillCandidateFile,
    SkillConsolidationBatch,
)
from app.skill_updates.batches import SkillBatchService
from app.skill_updates.schemas import RegressionCase
from app.skill_updates.storage import (
    ArtifactIntegrityError,
    ArtifactStorageError,
    CandidateManifestEntry,
    CandidateStorage,
)
from app.skills.loader import ACTIVE_SKILL_MANIFEST, load_skills, parse_skill_text

_TARGETS = frozenset({"buyer", "seller", "portfolio", "general", "unknown"})
_LOGGER = logging.getLogger(__name__)


class ActivationError(RuntimeError):
    def __init__(self, code: str, *, transient: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


class ActivationDisabled(ActivationError):
    def __init__(self) -> None:
        super().__init__("automatic_skill_merge_disabled")


class BatchRetryCoordinator:
    """Bounded exponential retry state for recoverable batch infrastructure failures."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        random_source: random.Random | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.random_source = random_source or random.Random()

    def schedule_failure(
        self,
        batch_id: uuid.UUID,
        error_code: str,
        *,
        transient: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        at = self._utc(now or datetime.now(UTC))
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None:
                raise ActivationError("batch_metadata_missing")
            retry_state = dict(batch.validation_json.get("retry", {}))
            history = list(retry_state.get("history", []))
            if transient and batch.attempt_count < self.settings.skill_update_max_attempts:
                exponent = max(batch.attempt_count - 1, 0)
                base_delay = min(
                    self.settings.skill_update_retry_initial_seconds * (2**exponent),
                    self.settings.skill_update_retry_max_seconds,
                )
                delay = max(1, int(base_delay * self.random_source.uniform(0.8, 1.2)))
                next_attempt = at + timedelta(seconds=delay)
                parked = False
            else:
                next_attempt = None
                parked = True
            entry = {
                "attempt": batch.attempt_count,
                "error_code": error_code[:128],
                "transient": transient,
                "recorded_at": at.isoformat(),
                "next_attempt_at": next_attempt.isoformat() if next_attempt else None,
                "parked": parked,
            }
            history.append(entry)
            batch.status = "failed"
            batch.next_attempt_at = next_attempt
            batch.validation_json = {
                **dict(batch.validation_json),
                "retry": {
                    "history": history[-20:],
                    "parked": parked,
                    "last_error_code": error_code[:128],
                },
            }
            batch.lease_owner = None
            batch.lease_expires_at = None
            return entry

    def begin_retry(
        self,
        batch_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
        lease_minutes: int = 30,
    ) -> bool:
        at = self._utc(now or datetime.now(UTC))
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None or batch.status != "failed" or batch.next_attempt_at is None:
                return False
            if self._utc(batch.next_attempt_at) > at:
                return False
            if batch.attempt_count >= self.settings.skill_update_max_attempts:
                retry = dict(batch.validation_json.get("retry", {}))
                retry["parked"] = True
                batch.validation_json = {**dict(batch.validation_json), "retry": retry}
                batch.next_attempt_at = None
                return False
            if not worker_id or len(worker_id) > 128:
                raise ValueError("worker_id must contain 1 to 128 characters")
            if not 1 <= lease_minutes <= 240:
                raise ValueError("lease_minutes must be between 1 and 240")
            batch.status = "validating"
            batch.next_attempt_at = None
            batch.lease_owner = worker_id
            batch.lease_expires_at = at + timedelta(minutes=lease_minutes)
            return True

    def acquire_revalidation_lease(
        self,
        batch_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
        lease_minutes: int = 30,
    ) -> bool:
        at = self._utc(now or datetime.now(UTC))
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None or batch.status != "consolidating":
                return False
            if not batch.validation_json.get("active_base_conflict", {}).get(
                "revalidation_required"
            ):
                return False
            if batch.lease_expires_at is not None and self._utc(batch.lease_expires_at) > at:
                return batch.lease_owner == worker_id
            if not worker_id or len(worker_id) > 128:
                raise ValueError("worker_id must contain 1 to 128 characters")
            if not 1 <= lease_minutes <= 240:
                raise ValueError("lease_minutes must be between 1 and 240")
            batch.lease_owner = worker_id
            batch.lease_expires_at = at + timedelta(minutes=lease_minutes)
            return True

    def acquire_recovery_lease(
        self,
        batch_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
        lease_minutes: int = 30,
    ) -> bool:
        at = self._utc(now or datetime.now(UTC))
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None or batch.status != "updating_skills":
                return False
            update = session.scalar(
                select(SkillActiveUpdate)
                .where(SkillActiveUpdate.batch_id == batch_id)
                .with_for_update()
            )
            if update is None or update.status != "applying":
                return False
            if batch.lease_expires_at is not None and self._utc(batch.lease_expires_at) > at:
                return batch.lease_owner == worker_id
            if not worker_id or len(worker_id) > 128:
                raise ValueError("worker_id must contain 1 to 128 characters")
            if not 1 <= lease_minutes <= 240:
                raise ValueError("lease_minutes must be between 1 and 240")
            batch.lease_owner = worker_id
            batch.lease_expires_at = at + timedelta(minutes=lease_minutes)
            return True

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SkillActivationService:
    """Applies validated local proposals, recovers crashes, then cleans manifest sources."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        storage: CandidateStorage,
        *,
        skill_directory: Path = Path("skills"),
        retries: BatchRetryCoordinator | None = None,
        reviewer_update_enqueue: Callable[[uuid.UUID], uuid.UUID | None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.storage = storage
        self.skill_directory = Path(skill_directory)
        self.retries = retries or BatchRetryCoordinator(session_factory, settings)
        self.reviewer_update_enqueue = reviewer_update_enqueue

    def process_batch(
        self,
        batch_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is not None and batch.status in {"cleanup_pending", "cleaned"}:
                self._enqueue_reviewer_update(batch_id)
                return self.cleanup(batch_id, now=now)
        result = self.apply(batch_id, worker_id=worker_id, now=now)
        if result.get("status") == "cleanup_pending":
            self._enqueue_reviewer_update(batch_id)
            cleanup = self.cleanup(batch_id, now=now)
            return {**result, **cleanup}
        return result

    def apply(
        self,
        batch_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if (
            not self.settings.skill_updates_enabled
            or not self.settings.skill_update_consolidation_enabled
            or not self.settings.skill_update_auto_merge_enabled
        ):
            raise ActivationDisabled()
        at = self._utc(now or datetime.now(UTC))
        attempt_started = False
        update_id: uuid.UUID | None = None
        try:
            self.retries.acquire_recovery_lease(batch_id, worker_id=worker_id, now=at)
            with self._file_lock():
                batch, candidates, update = self._load_for_apply(batch_id, worker_id, at)
                if batch.status in {"cleanup_pending", "cleaned"} or (
                    update and update.status == "applied"
                ):
                    return {
                        "batch_id": str(batch_id),
                        "status": batch.status
                        if batch.status in {"cleanup_pending", "cleaned"}
                        else "cleanup_pending",
                        "active_skill_update_id": str(update.id) if update else None,
                    }
                if batch.status == "validating":
                    update = self._begin_apply(batch_id, worker_id, at)
                    attempt_started = True
                elif batch.status == "updating_skills":
                    if batch.lease_owner != worker_id:
                        raise ActivationError("batch_lease_owner_mismatch")
                    update_id = update.id if update else None
                else:
                    raise ActivationError("batch_not_ready_for_activation")

                if update is not None:
                    update_id = update.id
                proposal_data = self._load_proposals(batch, candidates)
                if update is None or update.status in {"rolled_back", "failed"}:
                    if (
                        self._hash_active_files(list(proposal_data["base_hashes"]))
                        != batch.base_skill_hashes_json
                    ):
                        self._queue_revalidation(batch_id, "active_skill_base_hash_changed")
                        return {
                            "batch_id": str(batch_id),
                            "status": "revalidation_required",
                            "reason_code": "active_skill_base_hash_changed",
                        }
                    update = self._prepare_update(batch_id, proposal_data, at)
                    update_id = update.id
                elif update.status in {"prepared", "applying"}:
                    proposal_data = self._load_recovery_data(batch, update)
                elif update.status == "rollback_required":
                    raise ActivationError("active_skill_rollback_requires_operator")
                elif update.status == "applied":
                    self._mark_merge_success(batch_id, update.id, proposal_data["proposed_hashes"])
                    self._enqueue_reviewer_update(batch_id)
                    return {"batch_id": str(batch_id), "status": "cleanup_pending"}

                if update is not None and update.status == "applying":
                    recovery = self._recover_in_progress(update, proposal_data)
                    if recovery == "already_applied":
                        self._mark_merge_success(
                            batch_id, update.id, proposal_data["proposed_hashes"]
                        )
                        self._enqueue_reviewer_update(batch_id)
                        return {
                            "batch_id": str(batch_id),
                            "status": "cleanup_pending",
                            "active_skill_update_id": str(update.id),
                        }
                    if recovery == "restored_base":
                        pass

                self._apply_files(proposal_data)
                applied_hashes = self._verify_proposed_files(proposal_data)
                self._mark_merge_success(batch_id, update.id, applied_hashes)
                self._enqueue_reviewer_update(batch_id)
                return {
                    "batch_id": str(batch_id),
                    "status": "cleanup_pending",
                    "active_skill_update_id": str(update.id),
                    "applied_skill_hashes": applied_hashes,
                }
        except ActivationError as exc:
            self._handle_activation_failure(batch_id, update_id, exc, attempt_started, at)
            raise
        except Exception as exc:
            code = self._error_code(exc)
            transient = isinstance(exc, (OSError, SQLAlchemyError)) and not isinstance(
                exc, (ArtifactIntegrityError, IntegrityError)
            )
            error = ActivationError(code, transient=transient)
            self._handle_activation_failure(batch_id, update_id, error, attempt_started, at)
            raise error from exc

    def cleanup(self, batch_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, Any]:
        at = self._utc(now or datetime.now(UTC))
        try:
            batch, candidates, entries = self._load_cleanup_manifest(batch_id)
            if batch.status == "cleaned":
                return {"batch_id": str(batch_id), "status": "cleaned", "removed_count": 0}
            removed = self.storage.delete_manifest(entries)
            with self.session_factory.begin() as session:
                locked_batch = session.scalar(
                    select(SkillConsolidationBatch)
                    .where(SkillConsolidationBatch.id == batch_id)
                    .with_for_update()
                )
                if locked_batch is None or locked_batch.status not in {
                    "cleanup_pending",
                    "cleaned",
                }:
                    raise ActivationError("batch_cleanup_state_changed")
                rows = list(
                    session.scalars(
                        select(SkillCandidateFile)
                        .where(SkillCandidateFile.id.in_([item.id for item in candidates]))
                        .with_for_update()
                    )
                )
                if len(rows) != len(candidates):
                    raise ArtifactIntegrityError("cleanup candidate metadata is incomplete")
                for row in rows:
                    row.status = "cleaned"
                locked_batch.status = "cleaned"
                locked_batch.completed_at = locked_batch.completed_at or at
                locked_batch.lease_owner = None
                locked_batch.lease_expires_at = None
            return {
                "batch_id": str(batch_id),
                "status": "cleaned",
                "removed_count": len(removed),
                "removed_paths": removed,
            }
        except (ArtifactIntegrityError, ArtifactStorageError) as exc:
            self._mark_cleanup_failed(batch_id, self._error_code(exc))
            raise ActivationError(self._error_code(exc)) from exc
        except (OSError, SQLAlchemyError) as exc:
            code = self._error_code(exc)
            self.retries.schedule_failure(batch_id, code, transient=True, now=at)
            raise ActivationError(code, transient=True) from exc

    def rollback(self, batch_id: uuid.UUID) -> dict[str, Any]:
        """Restore exact verified base bytes if target files still match this update."""

        with self._file_lock():
            with self.session_factory() as session:
                update = session.scalar(
                    select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
                )
            if update is None or update.status != "applied":
                raise ActivationError("active_skill_update_not_rollbackable")
            targets = list(update.target_skill_keys_json)
            current_bytes = self._read_active_bytes(targets)
            current_hashes = {
                key: hashlib.sha256(content).hexdigest() for key, content in current_bytes.items()
            }
            if any(
                current_hashes[target]
                not in {
                    update.applied_skill_hashes_json.get(target),
                    update.base_skill_hashes_json.get(target),
                }
                for target in targets
            ):
                raise ActivationError("active_skill_hash_changed_since_update")
            backups: dict[str, bytes] = {}
            for target in targets:
                record = update.backup_artifacts_json.get(target)
                if not isinstance(record, dict):
                    raise ArtifactIntegrityError("active skill backup metadata is missing")
                relative = record.get("relative_path")
                digest = record.get("sha256")
                if not isinstance(relative, str) or not relative.startswith("backups/"):
                    raise ArtifactIntegrityError("active skill backup path is invalid")
                if not isinstance(digest, str):
                    raise ArtifactIntegrityError("active skill backup hash is missing")
                backup = self.storage.read_backup_artifact(Path(relative).name, digest)
                if hashlib.sha256(backup).hexdigest() != update.base_skill_hashes_json[target]:
                    raise ArtifactIntegrityError("active skill backup does not match base hash")
                backups[target] = backup
            try:
                for target, backup in backups.items():
                    self._atomic_replace_active(target, backup)
                expected = {key: update.base_skill_hashes_json[key] for key in targets}
                if self._hash_active_files(targets) != expected:
                    raise ActivationError("active_skill_rollback_verification_failed")
            except Exception:
                for target, original in current_bytes.items():
                    self._atomic_replace_active(target, original)
                raise
            with self.session_factory.begin() as session:
                locked = session.get(SkillActiveUpdate, update.id)
                batch = session.get(SkillConsolidationBatch, batch_id)
                if locked is None or batch is None:
                    raise ActivationError("active_skill_update_record_missing")
                locked.status = "rolled_back"
                locked.error_code = None
                batch.validation_json = {
                    **dict(batch.validation_json),
                    "rollback": {
                        "status": "completed",
                        "rolled_back_at": datetime.now(UTC).isoformat(),
                        "restored_hashes": expected,
                    },
                }
            return {"batch_id": str(batch_id), "status": "rolled_back", "restored_hashes": expected}

    def _load_for_apply(
        self, batch_id: uuid.UUID, worker_id: str, now: datetime
    ) -> tuple[SkillConsolidationBatch, list[SkillCandidateFile], SkillActiveUpdate | None]:
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is None:
                raise ActivationError("batch_metadata_missing")
            if batch.status in {"cleanup_pending", "cleaned"}:
                update = session.scalar(
                    select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
                )
                return batch, [], update
            if batch.status not in {"validating", "updating_skills"}:
                raise ActivationError("batch_not_ready_for_activation")
            if batch.lease_owner != worker_id:
                raise ActivationError("batch_lease_owner_mismatch")
            if batch.lease_expires_at is not None and self._utc(batch.lease_expires_at) <= now:
                raise ActivationError("batch_lease_expired")
            review = batch.validation_json.get("second_review", {})
            if review.get("status") != "passed":
                raise ActivationError("second_review_validation_missing")
            try:
                ids = [uuid.UUID(value) for value in batch.candidate_ids_json]
            except (TypeError, ValueError) as exc:
                raise ArtifactIntegrityError("batch candidate manifest is invalid") from exc
            expected_count = len(batch.candidate_ids_json)
            if (
                not 1 <= expected_count <= 50
                or len(ids) != expected_count
                or len(set(ids)) != expected_count
            ):
                raise ArtifactIntegrityError("batch candidate count is invalid")
            candidates_raw = list(
                session.scalars(select(SkillCandidateFile).where(SkillCandidateFile.id.in_(ids)))
            )
            by_id = {item.id: item for item in candidates_raw}
            if len(by_id) != expected_count:
                raise ArtifactIntegrityError("batch candidate records are incomplete")
            candidates = [by_id[item_id] for item_id in ids]
            if any(
                item.status != "claimed"
                or item.candidate_type != "customer_skill"
                or item.source_conversation_type != batch.source_conversation_type
                or item.sha256 is None
                for item in candidates
            ):
                raise ArtifactIntegrityError("candidate state or type differs from the batch")
            manifest = SkillBatchService._manifest(
                batch.id, batch.source_conversation_type, candidates
            )
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            if hashlib.sha256(manifest_bytes).hexdigest() != batch.manifest_sha256:
                raise ArtifactIntegrityError("batch manifest database hash does not match")
            stored_manifest = self.storage.read_work_artifact(f"batch-{batch_id}-manifest.json")
            if stored_manifest != manifest_bytes:
                raise ArtifactIntegrityError("batch manifest artifact does not match the database")
            for candidate in candidates:
                self.storage.read_candidate(
                    CandidateManifestEntry(
                        source_type=candidate.source_conversation_type or "unknown",
                        sequence=candidate.sequence,
                        relative_path=candidate.relative_path,
                        sha256=candidate.sha256 or "",
                    )
                )
            update = session.scalar(
                select(SkillActiveUpdate)
                .where(SkillActiveUpdate.batch_id == batch_id)
                .with_for_update()
            )
            return batch, candidates, update

    def _begin_apply(
        self, batch_id: uuid.UUID, worker_id: str, now: datetime
    ) -> SkillActiveUpdate | None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None or batch.status != "validating" or batch.lease_owner != worker_id:
                raise ActivationError("batch_state_or_lease_changed")
            if batch.attempt_count >= self.settings.skill_update_max_attempts:
                raise ActivationError("batch_retry_limit_reached")
            batch.status = "updating_skills"
            batch.attempt_count += 1
            batch.next_attempt_at = None
            return session.scalar(
                select(SkillActiveUpdate)
                .where(SkillActiveUpdate.batch_id == batch_id)
                .with_for_update()
            )

    def _load_proposals(
        self, batch: SkillConsolidationBatch, candidates: list[SkillCandidateFile]
    ) -> dict[str, Any]:
        review = batch.validation_json.get("second_review", {})
        outcomes = review.get("outcomes", {})
        expected_count = len(candidates)
        if (
            review.get("status") != "passed"
            or review.get("accounted_candidate_count") != expected_count
        ):
            raise ArtifactIntegrityError("second-review validation is incomplete")
        if len(outcomes) != expected_count or set(outcomes) != {
            str(item.id) for item in candidates
        }:
            raise ArtifactIntegrityError(
                "second-review outcomes do not account for every candidate"
            )
        targets = batch.target_skill_keys_json
        if len(targets) != len(set(targets)) or any(item not in _TARGETS for item in targets):
            raise ArtifactIntegrityError("proposed target skill list is invalid")
        proposal_records = review.get("proposal_artifacts", {})
        if set(proposal_records) != set(targets):
            raise ArtifactIntegrityError("proposal artifacts do not match assigned targets")
        accepted_ids_by_target: dict[str, set[str]] = {target: set() for target in targets}
        for candidate in candidates:
            outcome = outcomes.get(str(candidate.id))
            if not isinstance(outcome, dict):
                raise ArtifactIntegrityError("candidate review outcome is malformed")
            if outcome.get("decision") == "accepted":
                target = outcome.get("target_skill_key")
                if (
                    target not in _TARGETS
                    or target not in accepted_ids_by_target
                    or candidate.target_skill_key != target
                ):
                    raise ArtifactIntegrityError(
                        "candidate target assignment does not match review"
                    )
                accepted_ids_by_target[target].add(str(candidate.id))
            elif outcome.get("decision") != "rejected" or candidate.target_skill_key is not None:
                raise ArtifactIntegrityError("candidate rejection outcome is malformed")
        contents: dict[str, bytes] = {}
        proposed_hashes: dict[str, str] = {}
        for target in targets:
            record = proposal_records[target]
            relative = record.get("relative_path")
            digest = record.get("sha256")
            if not isinstance(relative, str) or not relative.startswith("work/"):
                raise ArtifactIntegrityError("proposed skill artifact path is invalid")
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ArtifactIntegrityError("proposed skill artifact hash is invalid")
            expected_ids = record.get("accepted_candidate_ids")
            if set(expected_ids or []) != accepted_ids_by_target[target]:
                raise ArtifactIntegrityError("proposal does not account for assigned candidates")
            content = self.storage.read_work_artifact(Path(relative).name, digest)
            if len(content) != record.get("size_bytes"):
                raise ArtifactIntegrityError("proposed skill size metadata does not match")
            skill = parse_skill_text(content.decode("utf-8"), ACTIVE_SKILL_MANIFEST[target])
            if skill.header.key != target or skill.header.intent != target:
                raise ArtifactIntegrityError("proposed skill metadata targets the wrong skill")
            contents[target] = content
            proposed_hashes[target] = digest
        base_hashes = batch.base_skill_hashes_json
        expected_base_keys = set(ACTIVE_SKILL_MANIFEST) - {"skill_reviewer"}
        if set(base_hashes) != expected_base_keys:
            raise ArtifactIntegrityError("active skill base hashes are incomplete")
        return {
            "targets": list(targets),
            "contents": contents,
            "base_hashes": dict(base_hashes),
            "proposed_hashes": proposed_hashes,
            "proposal_records": proposal_records,
            "review": review,
            "outcomes": outcomes,
        }

    def _prepare_update(
        self, batch_id: uuid.UUID, proposal_data: dict[str, Any], now: datetime
    ) -> SkillActiveUpdate:
        current_bytes = self._read_active_bytes(proposal_data["targets"])
        current_hashes = {
            key: hashlib.sha256(value).hexdigest() for key, value in current_bytes.items()
        }
        target_base_hashes = {
            key: proposal_data["base_hashes"][key] for key in proposal_data["targets"]
        }
        if current_hashes != target_base_hashes:
            raise ActivationError("active_skill_base_hash_changed")
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is None:
                raise ActivationError("batch_metadata_missing")
            existing = session.scalar(
                select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
            )
        update_id = existing.id if existing is not None else uuid.uuid4()
        backup_records: dict[str, dict[str, Any]] = {}
        for target, content in current_bytes.items():
            base_hash = current_hashes[target]
            name = f"update-{update_id}-{target}-{base_hash[:12]}.md"
            stored = self.storage.write_backup_artifact(name, content)
            if stored.sha256 != base_hash:
                raise ArtifactIntegrityError("backup does not match active skill base hash")
            backup_records[target] = {
                "relative_path": stored.relative_path,
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "retain_until": (
                    now + timedelta(days=self.settings.skill_update_backup_retention_days)
                ).isoformat(),
            }
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None or batch.status != "updating_skills":
                raise ActivationError("batch_state_changed_before_update_record")
            update = session.scalar(
                select(SkillActiveUpdate)
                .where(SkillActiveUpdate.batch_id == batch_id)
                .with_for_update()
            )
            attempt_record = {
                "attempt": batch.attempt_count,
                "prepared_at": now.isoformat(),
                "base_hashes": proposal_data["base_hashes"],
                "proposed_hashes": proposal_data["proposed_hashes"],
            }
            if update is None:
                update = SkillActiveUpdate(
                    id=update_id,
                    batch_id=batch_id,
                    status="applying",
                    manifest_sha256=batch.manifest_sha256,
                    target_skill_keys_json=proposal_data["targets"],
                    base_skill_hashes_json=proposal_data["base_hashes"],
                    proposed_skill_hashes_json=proposal_data["proposed_hashes"],
                    backup_artifacts_json=backup_records,
                    attempts_json=[attempt_record],
                )
                session.add(update)
                session.flush()
            else:
                update.status = "applying"
                update.manifest_sha256 = batch.manifest_sha256
                update.target_skill_keys_json = proposal_data["targets"]
                update.base_skill_hashes_json = proposal_data["base_hashes"]
                update.proposed_skill_hashes_json = proposal_data["proposed_hashes"]
                update.backup_artifacts_json = backup_records
                update.attempts_json = [*update.attempts_json, attempt_record][-20:]
                update.error_code = None
                update.applied_at = None
            batch.active_skill_update_id = update.id
            return update

    def _load_recovery_data(
        self, batch: SkillConsolidationBatch, update: SkillActiveUpdate
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            candidates = list(
                session.scalars(
                    select(SkillCandidateFile).where(
                        SkillCandidateFile.id.in_(
                            [uuid.UUID(value) for value in batch.candidate_ids_json]
                        )
                    )
                )
            )
        proposal_data = self._load_proposals(batch, candidates)
        if (
            update.manifest_sha256 != batch.manifest_sha256
            or update.base_skill_hashes_json != proposal_data["base_hashes"]
            or update.proposed_skill_hashes_json != proposal_data["proposed_hashes"]
            or update.target_skill_keys_json != proposal_data["targets"]
        ):
            raise ArtifactIntegrityError(
                "active skill update record differs from validated proposal"
            )
        return proposal_data

    def _recover_in_progress(self, update: SkillActiveUpdate, proposal_data: dict[str, Any]) -> str:
        current = self._hash_active_files(proposal_data["targets"])
        proposed = proposal_data["proposed_hashes"]
        base = {key: proposal_data["base_hashes"][key] for key in proposal_data["targets"]}
        non_targets = [
            key for key in proposal_data["base_hashes"] if key not in proposal_data["targets"]
        ]
        current_all = self._hash_active_files(list(proposal_data["base_hashes"]))
        if any(current_all[key] != proposal_data["base_hashes"][key] for key in non_targets):
            raise ActivationError("active_skill_conflict_during_recovery")
        if current == proposed:
            return "already_applied"
        if any(
            current.get(key) not in {base.get(key), proposed.get(key)}
            for key in proposal_data["targets"]
        ):
            raise ActivationError("active_skill_conflict_during_recovery")
        self._restore_backups(update)
        return "restored_base"

    def _restore_backups(self, update: SkillActiveUpdate) -> None:
        current = self._hash_active_files(update.target_skill_keys_json)
        for target, digest in current.items():
            if digest not in {
                update.base_skill_hashes_json.get(target),
                update.proposed_skill_hashes_json.get(target),
            }:
                raise ActivationError("active_skill_conflict_during_recovery")
        for target in update.target_skill_keys_json:
            record = update.backup_artifacts_json.get(target)
            if not isinstance(record, dict):
                raise ArtifactIntegrityError("active skill backup metadata is missing")
            relative = record.get("relative_path")
            backup_digest = record.get("sha256")
            if not isinstance(relative, str) or not relative.startswith("backups/"):
                raise ArtifactIntegrityError("active skill backup path is invalid")
            backup = self.storage.read_backup_artifact(Path(relative).name, str(backup_digest))
            if hashlib.sha256(backup).hexdigest() != update.base_skill_hashes_json[target]:
                raise ArtifactIntegrityError("active skill backup does not match base hash")
            self._atomic_replace_active(target, backup)

    def _apply_files(self, proposal_data: dict[str, Any]) -> None:
        targets = proposal_data["targets"]
        if not targets:
            return
        if (
            self._hash_active_files(list(proposal_data["base_hashes"]))
            != proposal_data["base_hashes"]
        ):
            raise ActivationError("active_skill_base_hash_changed")
        before_skills = load_skills(self.skill_directory)
        for target in targets:
            proposed = proposal_data["contents"][target]
            parsed = parse_skill_text(proposed.decode("utf-8"), ACTIVE_SKILL_MANIFEST[target])
            if not parsed.instructions.startswith(before_skills[target].instructions):
                raise ArtifactIntegrityError("proposed skill removes existing active instructions")
            self._validate_regression_records(target, proposal_data)
        for target in targets:
            self._atomic_replace_active(target, proposal_data["contents"][target])

    def _validate_regression_records(self, target: str, proposal_data: dict[str, Any]) -> None:
        target_ids = {
            candidate_id
            for candidate_id, outcome in proposal_data["outcomes"].items()
            if outcome.get("decision") == "accepted" and outcome.get("target_skill_key") == target
        }
        with self.session_factory() as session:
            candidates = list(
                session.scalars(
                    select(SkillCandidateFile).where(
                        SkillCandidateFile.id.in_([uuid.UUID(value) for value in target_ids])
                    )
                )
            )
        if {str(candidate.id) for candidate in candidates} != target_ids:
            raise ArtifactIntegrityError("accepted regression candidate metadata is incomplete")
        for candidate in candidates:
            cases = candidate.regression_cases_json
            if not cases:
                raise ArtifactIntegrityError("accepted learning has no regression case")
            for case in cases:
                RegressionCase.model_validate(case)

    def _verify_proposed_files(self, proposal_data: dict[str, Any]) -> dict[str, str]:
        current = self._hash_active_files(proposal_data["targets"])
        if current != proposal_data["proposed_hashes"]:
            raise ArtifactIntegrityError("active skill replacement hash verification failed")
        current_all = self._hash_active_files(list(proposal_data["base_hashes"]))
        for target in proposal_data["targets"]:
            if current_all[target] != proposal_data["proposed_hashes"][target]:
                raise ArtifactIntegrityError("active skill changed during replacement")
        for key, digest in proposal_data["base_hashes"].items():
            if key not in proposal_data["targets"] and current_all[key] != digest:
                raise ArtifactIntegrityError("untargeted active skill changed during replacement")
        load_skills(self.skill_directory)
        return current

    def _atomic_replace_active(self, target: str, content: bytes) -> None:
        if target not in _TARGETS:
            raise ActivationError("active_skill_target_not_allowlisted")
        path = self.skill_directory / ACTIVE_SKILL_MANIFEST[target]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != (self.skill_directory / Path(ACTIVE_SKILL_MANIFEST[target]).parent).resolve()
        ):
            raise ActivationError("active_skill_path_is_unsafe")
        mode = stat.S_IMODE(path.stat().st_mode)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target}-update-", dir=self.skill_directory
        )
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory(self.skill_directory)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _prepare_update_record_for_empty_batch(
        self, batch_id: uuid.UUID, proposal_data: dict[str, Any], now: datetime
    ) -> SkillActiveUpdate:
        return self._prepare_update(batch_id, proposal_data, now)

    def _mark_merge_success(
        self, batch_id: uuid.UUID, update_id: uuid.UUID, applied_hashes: dict[str, str]
    ) -> None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            update = session.scalar(
                select(SkillActiveUpdate).where(SkillActiveUpdate.id == update_id).with_for_update()
            )
            if batch is None or update is None:
                raise ActivationError("active_skill_update_record_missing")
            update.status = "applied"
            update.applied_skill_hashes_json = applied_hashes
            update.applied_at = datetime.now(UTC)
            update.error_code = None
            batch.active_skill_update_id = update.id
            batch.status = "cleanup_pending"
            batch.completed_at = batch.completed_at or datetime.now(UTC)
            batch.next_attempt_at = None
            batch.validation_json = {
                **dict(batch.validation_json),
                "active_update": {
                    "status": "applied",
                    "update_id": str(update.id),
                    "applied_skill_hashes": applied_hashes,
                    "applied_at": update.applied_at.isoformat(),
                    "backup_artifacts": update.backup_artifacts_json,
                },
            }

    def _load_cleanup_manifest(
        self, batch_id: uuid.UUID
    ) -> tuple[SkillConsolidationBatch, list[SkillCandidateFile], list[CandidateManifestEntry]]:
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is None or batch.status not in {"cleanup_pending", "cleaned"}:
                raise ActivationError("batch_not_ready_for_cleanup")
            update = session.scalar(
                select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
            )
            if update is None or update.status != "applied":
                raise ArtifactIntegrityError("active skill update is not durably applied")
            content = self.storage.read_work_artifact(f"batch-{batch_id}-manifest.json")
            if hashlib.sha256(content).hexdigest() != batch.manifest_sha256:
                raise ArtifactIntegrityError("cleanup manifest hash does not match database")
            try:
                manifest = json.loads(content)
            except (TypeError, ValueError) as exc:
                raise ArtifactIntegrityError("cleanup manifest JSON is invalid") from exc
            if (
                manifest.get("batch_id") != str(batch_id)
                or manifest.get("source_type") != batch.source_conversation_type
                or manifest.get("manifest_version") != "customer-skill-batch-v1"
            ):
                raise ArtifactIntegrityError("cleanup manifest identity is invalid")
            records = manifest.get("candidates")
            expected_count = len(batch.candidate_ids_json)
            if (
                not 1 <= expected_count <= 50
                or not isinstance(records, list)
                or len(records) != expected_count
            ):
                raise ArtifactIntegrityError("cleanup manifest candidate count is invalid")
            ids = [uuid.UUID(str(record["candidate_id"])) for record in records]
            if (
                len(set(ids)) != expected_count
                or [str(item) for item in ids] != batch.candidate_ids_json
            ):
                raise ArtifactIntegrityError("cleanup manifest candidate order differs from batch")
            rows_raw = list(
                session.scalars(select(SkillCandidateFile).where(SkillCandidateFile.id.in_(ids)))
            )
            by_id = {row.id: row for row in rows_raw}
            if len(by_id) != expected_count:
                raise ArtifactIntegrityError("cleanup candidate metadata is incomplete")
            candidates = [by_id[item] for item in ids]
            entries: list[CandidateManifestEntry] = []
            for record, candidate in zip(records, candidates, strict=True):
                if candidate.status not in {"claimed", "cleaned"}:
                    raise ArtifactIntegrityError("cleanup candidate state is invalid")
                if (
                    record.get("sequence") != candidate.sequence
                    or record.get("relative_path") != candidate.relative_path
                    or record.get("sha256") != candidate.sha256
                    or candidate.source_conversation_type != batch.source_conversation_type
                    or candidate.candidate_type != "customer_skill"
                ):
                    raise ArtifactIntegrityError(
                        "cleanup manifest does not match candidate metadata"
                    )
                entries.append(
                    CandidateManifestEntry(
                        source_type=batch.source_conversation_type,
                        sequence=candidate.sequence,
                        relative_path=candidate.relative_path,
                        sha256=candidate.sha256 or "",
                    )
                )
            return batch, candidates, entries

    def _handle_activation_failure(
        self,
        batch_id: uuid.UUID,
        update_id: uuid.UUID | None,
        error: ActivationError,
        attempt_started: bool,
        at: datetime,
    ) -> None:
        if error.code == "active_skill_base_hash_changed":
            self._queue_revalidation(batch_id, error.code)
            return
        rollback_ok = True
        update: SkillActiveUpdate | None = None
        with self.session_factory() as session:
            if update_id is not None:
                update = session.get(SkillActiveUpdate, update_id)
        if update is not None and update.status == "applying":
            if error.code == "active_skill_conflict_during_recovery":
                with self.session_factory.begin() as session:
                    locked = session.get(SkillActiveUpdate, update.id)
                    if locked is not None:
                        locked.status = "rollback_required"
                        locked.error_code = error.code[:128]
                self.retries.schedule_failure(batch_id, error.code, transient=False, now=at)
                return
            try:
                self._restore_backups(update)
            except Exception:
                rollback_ok = False
                with self.session_factory.begin() as session:
                    locked = session.get(SkillActiveUpdate, update.id)
                    if locked is not None:
                        locked.status = "rollback_required"
                        locked.error_code = "rollback_restore_failed"
            if rollback_ok:
                with self.session_factory.begin() as session:
                    locked = session.get(SkillActiveUpdate, update.id)
                    if locked is not None and locked.status == "applying":
                        locked.status = "rolled_back"
                        locked.error_code = error.code[:128]
        if attempt_started or error.transient or not rollback_ok:
            transient = error.transient and rollback_ok
            self.retries.schedule_failure(
                batch_id,
                error.code if rollback_ok else "rollback_restore_failed",
                transient=transient,
                now=at,
            )
        else:
            with self.session_factory.begin() as session:
                batch = session.get(SkillConsolidationBatch, batch_id)
                if batch is not None:
                    batch.status = "failed"
                    batch.next_attempt_at = None
                    batch.validation_json = {
                        **dict(batch.validation_json),
                        "activation_failure": {
                            "error_code": error.code[:128],
                            "transient": False,
                            "failed_at": datetime.now(UTC).isoformat(),
                            "parked": True,
                        },
                    }
            if update is not None and update.status == "applying":
                with self.session_factory.begin() as session:
                    locked = session.get(SkillActiveUpdate, update.id)
                    if locked is not None and rollback_ok:
                        locked.status = "rolled_back"
                        locked.error_code = error.code[:128]

    def _queue_revalidation(self, batch_id: uuid.UUID, reason: str) -> None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is not None:
                batch.status = "consolidating"
                batch.next_attempt_at = None
                if batch.lease_owner:
                    batch.lease_expires_at = datetime.now(UTC) + timedelta(minutes=30)
                batch.validation_json = {
                    **dict(batch.validation_json),
                    "active_base_conflict": {
                        "reason_code": reason,
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "revalidation_required": True,
                    },
                }

    def _mark_cleanup_failed(self, batch_id: uuid.UUID, reason: str) -> None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is not None and batch.status == "cleanup_pending":
                batch.status = "failed"
                batch.next_attempt_at = None
                batch.validation_json = {
                    **dict(batch.validation_json),
                    "cleanup_failure": {"reason_code": reason, "parked": True},
                }

    def _read_active_bytes(self, targets: list[str]) -> dict[str, bytes]:
        contents: dict[str, bytes] = {}
        for target in targets:
            if target not in _TARGETS:
                raise ActivationError("active_skill_target_not_allowlisted")
            path = self.skill_directory / ACTIVE_SKILL_MANIFEST[target]
            if path.is_symlink() or not path.is_file():
                raise ActivationError("active_skill_file_unavailable")
            data = path.read_bytes()
            if len(data) > self.settings.skill_update_active_skill_max_bytes:
                raise ActivationError("active_skill_exceeds_configured_size")
            contents[target] = data
        return contents

    def _hash_active_files(self, targets: list[str]) -> dict[str, str]:
        return {
            key: hashlib.sha256(content).hexdigest()
            for key, content in self._read_active_bytes(targets).items()
        }

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        lock_path = self.storage.lock_file_path()
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _error_code(self, error: Exception) -> str:
        value = str(error).strip().lower()
        return re.sub(r"[^a-z0-9]+", "_", value).strip("_")[:120] or "activation_failed"

    def _enqueue_reviewer_update(self, batch_id: uuid.UUID) -> None:
        try:
            reviewer_path = self.skill_directory / ACTIVE_SKILL_MANIFEST["skill_reviewer"]
            reviewer_hash = hashlib.sha256(reviewer_path.read_bytes()).hexdigest()
            reviewer_run_id: uuid.UUID | None = None
            created = False
            with self.session_factory.begin() as session:
                batch = session.get(SkillConsolidationBatch, batch_id)
                update = session.scalar(
                    select(SkillActiveUpdate).where(SkillActiveUpdate.batch_id == batch_id)
                )
                if (
                    batch is None
                    or batch.status not in {"cleanup_pending", "cleaned"}
                    or update is None
                    or update.status != "applied"
                    or not update.applied_skill_hashes_json
                ):
                    return
                existing = session.scalar(
                    select(ReviewerUpdateRun).where(ReviewerUpdateRun.customer_batch_id == batch_id)
                )
                if existing is None:
                    existing = ReviewerUpdateRun(
                        id=uuid.uuid4(),
                        customer_batch_id=batch_id,
                        status="queued",
                        suite_version="reviewer-fixed-v1",
                        active_reviewer_sha256=reviewer_hash,
                    )
                    session.add(existing)
                    created = True
                reviewer_run_id = existing.id
            _LOGGER.info(
                "reviewer_update_run_queued batch_id=%s run_id=%s created=%s enabled=%s",
                batch_id,
                reviewer_run_id,
                created,
                self.settings.skill_reviewer_updates_enabled,
            )
            if self.reviewer_update_enqueue is not None:
                self.reviewer_update_enqueue(batch_id)
        except Exception:
            # Reviewer work is independently replayable and must not invalidate a
            # customer skill update that has already passed hash verification.
            _LOGGER.exception("reviewer update enqueue failed for batch %s", batch_id)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
