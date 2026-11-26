from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import SkillCandidateFile, SkillConsolidationBatch
from app.skill_updates.storage import (
    ArtifactIntegrityError,
    ArtifactStorageError,
    CandidateManifestEntry,
    CandidateStorage,
    StoredArtifact,
)

SOURCE_TYPES = ("buyer", "seller", "portfolio", "general", "unknown")
logger = logging.getLogger("whatsapp_assistant.skill_update_batches")
_CANDIDATE_BODY = re.compile(r"\A---\s*\n.*?\n---\s*\n(?P<body>.*)\Z", re.S)


class BatchError(RuntimeError):
    """A batch cannot be safely consolidated or verified."""


@dataclass(frozen=True)
class BatchClaim:
    batch_id: uuid.UUID
    source_type: str
    candidate_ids: tuple[uuid.UUID, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class ConsolidationResult:
    batch_id: uuid.UUID
    manifest_sha256: str
    manifest_path: str
    part_paths: tuple[str, ...]
    candidate_parts: dict[str, int]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class SkillBatchService:
    """Claims exact customer candidate batches and creates verified work artifacts."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        storage: CandidateStorage,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.storage = storage

    def claim_batch(
        self,
        source_type: str,
        *,
        worker_id: str,
        now: datetime | None = None,
        lease_minutes: int = 30,
    ) -> BatchClaim | None:
        if (
            not self.settings.skill_updates_enabled
            or not self.settings.skill_update_consolidation_enabled
        ):
            raise BatchError("skill_update_consolidation_disabled")
        if source_type not in SOURCE_TYPES:
            raise ValueError("unsupported customer source type")
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if not 1 <= lease_minutes <= 240:
            raise ValueError("lease_minutes must be between 1 and 240")
        claimed_at = self._utc(now or datetime.now(UTC))
        batch_id = uuid.uuid4()
        with self.session_factory.begin() as session:
            candidates = list(
                session.scalars(
                    select(SkillCandidateFile)
                    .where(
                        SkillCandidateFile.candidate_type == "customer_skill",
                        SkillCandidateFile.source_conversation_type == source_type,
                        SkillCandidateFile.status == "pending",
                        SkillCandidateFile.sha256.is_not(None),
                    )
                    .order_by(
                        SkillCandidateFile.created_at,
                        SkillCandidateFile.sequence,
                        SkillCandidateFile.id,
                    )
                    .limit(self.settings.skill_update_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            if not candidates:
                return None
            partial_due_to_age = len(candidates) < self.settings.skill_update_batch_size
            if partial_due_to_age:
                oldest_created = min(self._utc(item.created_at) for item in candidates)
                age_minutes = max(0, int((claimed_at - oldest_created).total_seconds() / 60))
                if age_minutes < self.settings.skill_update_batch_max_wait_minutes:
                    logger.info(
                        "skill_update_batch_not_claimed source_type=%s pending_count=%s "
                        "batch_size=%s age_minutes=%s max_wait_minutes=%s "
                        "reason=insufficient_pending_candidates paths=%s",
                        source_type,
                        len(candidates),
                        self.settings.skill_update_batch_size,
                        age_minutes,
                        self.settings.skill_update_batch_max_wait_minutes,
                        [item.relative_path for item in candidates],
                    )
                    return None
                logger.info(
                    "skill_update_batch_partial_claim_due_to_age source_type=%s "
                    "pending_count=%s batch_size=%s age_minutes=%s max_wait_minutes=%s",
                    source_type,
                    len(candidates),
                    self.settings.skill_update_batch_size,
                    age_minutes,
                    self.settings.skill_update_batch_max_wait_minutes,
                )
            candidates.sort(key=lambda item: (item.sequence, str(item.id)))
            if any(item.sha256 is None for item in candidates):
                raise BatchError("candidate hash is missing")

            manifest = self._manifest(batch_id, source_type, candidates)
            manifest_bytes = _canonical_json(manifest)
            manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
            # Reserve rows conditionally as well as locking them. This also makes the
            # claim safe on SQLite, where SELECT FOR UPDATE is ignored.
            ids = [item.id for item in candidates]
            update_result = session.execute(
                update(SkillCandidateFile)
                .where(
                    SkillCandidateFile.id.in_(ids),
                    SkillCandidateFile.status == "pending",
                )
                .values(status="claimed")
            )
            changed = getattr(update_result, "rowcount", None)
            if changed != len(candidates):
                return None

            stored_manifest = self._write_or_verify_manifest(batch_id, manifest_bytes, None)
            batch = SkillConsolidationBatch(
                id=batch_id,
                source_conversation_type=source_type,
                status="claimed",
                candidate_ids_json=[str(item.id) for item in candidates],
                manifest_sha256=manifest_hash,
                base_skill_hashes_json={},
                target_skill_keys_json=[],
                artifact_paths_json=[stored_manifest.relative_path],
                validation_json={"manifest_version": "customer-skill-batch-v1"},
                attempt_count=0,
                started_at=claimed_at,
                lease_owner=worker_id,
                lease_expires_at=claimed_at + timedelta(minutes=lease_minutes),
            )
            session.add(batch)
            logger.info(
                "skill_update_batch_claimed batch_id=%s source_type=%s candidate_count=%s "
                "candidate_ids=%s paths=%s",
                batch_id,
                source_type,
                len(candidates),
                [str(item.id) for item in candidates],
                [item.relative_path for item in candidates],
            )
            return BatchClaim(
                batch_id=batch_id,
                source_type=source_type,
                candidate_ids=tuple(ids),
                manifest_sha256=manifest_hash,
            )

    def consolidate(
        self,
        batch_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> ConsolidationResult:
        if (
            not self.settings.skill_updates_enabled
            or not self.settings.skill_update_consolidation_enabled
        ):
            raise BatchError("skill_update_consolidation_disabled")
        at = self._utc(now or datetime.now(UTC))
        try:
            batch, candidates, manifest = self._load_claimed_batch(batch_id, worker_id, at)
            manifest_bytes = _canonical_json(manifest)
            manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
            if manifest_hash != batch.manifest_sha256:
                raise ArtifactIntegrityError("batch manifest hash does not match its frozen record")
            manifest_path = f"work/batch-{batch_id}-manifest.json"
            self._write_or_verify_manifest(batch_id, manifest_bytes, manifest_path)

            rules: list[tuple[str, bytes]] = []
            for ordinal, candidate in enumerate(candidates, start=1):
                entry = CandidateManifestEntry(
                    source_type=batch.source_conversation_type,
                    sequence=candidate.sequence,
                    relative_path=candidate.relative_path,
                    sha256=candidate.sha256 or "",
                )
                content = self.storage.read_candidate(entry)
                match = _CANDIDATE_BODY.match(content)
                if match is None:
                    raise ArtifactIntegrityError("candidate Markdown body is invalid")
                block = (
                    f"## Candidate {ordinal}: {candidate.id}\n\n"
                    f"Source type: {batch.source_conversation_type}\n\n"
                    f"{match.group('body').strip()}\n"
                ).encode()
                rules.append((str(candidate.id), block))

            parts, candidate_parts = self._split_rules(batch_id, rules)
            paths: list[str] = [manifest_path]
            part_metadata: list[dict[str, Any]] = []
            for index, content in enumerate(parts, start=1):
                name = f"batch-{batch_id}-consolidated-{index}.md"
                artifact = self.storage.write_work_artifact(name, content)
                paths.append(artifact.relative_path)
                part_metadata.append(
                    {
                        "relative_path": artifact.relative_path,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                    }
                )
            self._mark_consolidated(batch_id, paths, candidate_parts, part_metadata)
            return ConsolidationResult(
                batch_id=batch_id,
                manifest_sha256=manifest_hash,
                manifest_path=manifest_path,
                part_paths=tuple(paths[1:]),
                candidate_parts=candidate_parts,
            )
        except (ArtifactStorageError, ArtifactIntegrityError, BatchError, ValueError) as exc:
            reason = self._reason_code(exc)
            if reason not in {
                "batch_not_claimed",
                "batch_lease_expired",
                "batch_lease_owner_mismatch",
            }:
                self._mark_failed(batch_id, reason)
            if isinstance(exc, BatchError):
                raise
            raise BatchError(reason) from exc

    def _load_claimed_batch(
        self, batch_id: uuid.UUID, worker_id: str, now: datetime
    ) -> tuple[SkillConsolidationBatch, list[SkillCandidateFile], dict[str, Any]]:
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is None or batch.status not in {"claimed", "consolidating"}:
                raise BatchError("batch_not_claimed")
            if not worker_id or batch.lease_owner != worker_id:
                raise BatchError("batch_lease_owner_mismatch")
            if batch.lease_expires_at is not None and self._utc(batch.lease_expires_at) <= now:
                raise BatchError("batch_lease_expired")
            try:
                candidate_ids = [uuid.UUID(value) for value in batch.candidate_ids_json]
            except (TypeError, ValueError) as exc:
                raise BatchError("batch_candidate_manifest_invalid") from exc
            expected_count = len(batch.candidate_ids_json)
            if (
                not 1 <= expected_count <= 50
                or len(candidate_ids) != expected_count
                or len(set(candidate_ids)) != expected_count
            ):
                raise BatchError("batch_candidate_count_invalid")
            records = list(
                session.scalars(
                    select(SkillCandidateFile)
                    .where(SkillCandidateFile.id.in_(candidate_ids))
                    .with_for_update()
                )
            )
            by_id = {item.id: item for item in records}
            if len(by_id) != expected_count:
                raise ArtifactIntegrityError("a claimed candidate is missing from the database")
            candidates = [by_id[candidate_id] for candidate_id in candidate_ids]
            if any(
                item.candidate_type != "customer_skill"
                or item.status != "claimed"
                or item.source_conversation_type != batch.source_conversation_type
                or item.sha256 is None
                for item in candidates
            ):
                raise ArtifactIntegrityError("claimed candidate metadata does not match its batch")
            manifest = self._manifest(batch.id, batch.source_conversation_type, candidates)
            manifest_bytes = _canonical_json(manifest)
            expected_hash = hashlib.sha256(manifest_bytes).hexdigest()
            recorded_manifest_path = f"work/batch-{batch_id}-manifest.json"
            try:
                stored = self.storage.read_work_artifact(recorded_manifest_path)
            except ArtifactStorageError:
                stored = None
            if stored is not None and stored != manifest_bytes:
                raise ArtifactIntegrityError("stored batch manifest was modified")
            if batch.manifest_sha256 != expected_hash:
                raise ArtifactIntegrityError("database batch manifest was modified")
            return batch, candidates, manifest

    @staticmethod
    def _manifest(
        batch_id: uuid.UUID, source_type: str, candidates: list[SkillCandidateFile]
    ) -> dict[str, Any]:
        return {
            "manifest_version": "customer-skill-batch-v1",
            "batch_id": str(batch_id),
            "source_type": source_type,
            "candidates": [
                {
                    "candidate_id": str(candidate.id),
                    "sequence": candidate.sequence,
                    "relative_path": candidate.relative_path,
                    "sha256": candidate.sha256,
                }
                for candidate in candidates
            ],
        }

    def _split_rules(
        self, batch_id: uuid.UUID, rules: list[tuple[str, bytes]]
    ) -> tuple[list[str], dict[str, int]]:
        max_bytes = self.settings.skill_update_consolidated_max_bytes
        parts: list[str] = []
        mapping: dict[str, int] = {}
        current_rules: list[tuple[str, bytes]] = []

        def render(index: int, items: list[tuple[str, bytes]]) -> str:
            prefix = f"# Skill candidate consolidation\n\nBatch: {batch_id}\nPart: {index}\n\n"
            return prefix + "\n".join(block.decode("utf-8") for _, block in items)

        for candidate_id, block in rules:
            tentative = [*current_rules, (candidate_id, block)]
            next_index = len(parts) + 1
            if len(render(next_index, tentative).encode("utf-8")) <= max_bytes:
                current_rules = tentative
                continue
            if not current_rules:
                raise BatchError("single_candidate_exceeds_consolidated_limit")
            parts.append(render(next_index, current_rules))
            part_index = len(parts)
            for prior_id, _ in current_rules:
                mapping[prior_id] = part_index
            current_rules = [(candidate_id, block)]
            next_index = len(parts) + 1
            if len(render(next_index, current_rules).encode("utf-8")) > max_bytes:
                raise BatchError("single_candidate_exceeds_consolidated_limit")
        if current_rules:
            parts.append(render(len(parts) + 1, current_rules))
            part_index = len(parts)
            for candidate_id, _ in current_rules:
                mapping[candidate_id] = part_index
        if len(mapping) != len(rules):
            raise BatchError("consolidation_accounting_incomplete")
        return parts, mapping

    def _write_or_verify_manifest(
        self, batch_id: uuid.UUID, content: bytes, expected_path: str | None
    ) -> StoredArtifact:
        name = f"batch-{batch_id}-manifest.json"
        if expected_path is not None and expected_path != f"work/{name}":
            raise ArtifactIntegrityError("manifest path does not match the batch id")
        try:
            existing = self.storage.read_work_artifact(name)
        except ArtifactStorageError:
            existing = None
        if existing is not None:
            if existing != content:
                raise ArtifactIntegrityError(
                    "existing batch manifest differs from the frozen manifest"
                )
            return StoredArtifact(
                f"work/{name}", hashlib.sha256(existing).hexdigest(), len(existing)
            )
        return self.storage.write_work_artifact(name, content)

    def _mark_consolidated(
        self,
        batch_id: uuid.UUID,
        paths: list[str],
        candidate_parts: dict[str, int],
        part_metadata: list[dict[str, Any]],
    ) -> None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None:
                raise BatchError("batch_metadata_missing")
            if batch.status not in {"claimed", "consolidating"}:
                raise BatchError("batch_state_changed")
            batch.status = "consolidating"
            batch.artifact_paths_json = paths
            batch.validation_json = {
                **dict(batch.validation_json),
                "candidate_parts": candidate_parts,
                "accounted_candidate_count": len(candidate_parts),
                "consolidated_artifacts": part_metadata,
            }

    def _mark_failed(self, batch_id: uuid.UUID, reason: str) -> None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is not None and batch.status in {"claimed", "consolidating"}:
                batch.status = "failed"
                batch.validation_json = {
                    **dict(batch.validation_json),
                    "error_code": reason,
                    "failed_at": datetime.now(UTC).isoformat(),
                }

    @staticmethod
    def _reason_code(error: Exception) -> str:
        value = str(error).strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
        return normalized[:120] or "batch_integrity_failure"

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
