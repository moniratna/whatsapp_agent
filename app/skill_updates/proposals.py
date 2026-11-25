from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import SkillCandidateFile, SkillConsolidationBatch
from app.skill_updates.extraction import (
    _CLAIM_PATTERNS,
    _EMAIL,
    _URL,
    _candidate_learning,
    _contains_personal_data,
    _contains_unsupported_claim,
    _is_semantic_duplicate,
)
from app.skill_updates.merge_reviewer import SecondReviewerPort
from app.skill_updates.merge_schemas import (
    CandidateRouting,
    MergeCandidateInput,
    SecondReviewOutput,
    SecondReviewRequest,
    SkillTarget,
)
from app.skill_updates.schemas import RegressionCase
from app.skill_updates.storage import (
    ArtifactIntegrityError,
    ArtifactStorageError,
    CandidateManifestEntry,
    CandidateStorage,
)
from app.skills.loader import ACTIVE_SKILL_MANIFEST, load_skills

_FRONT_MATTER = re.compile(r"\A---\s*\n(?P<header>.*?)\n---\s*\n(?P<body>.*)\Z", re.S)
_REF_PATTERN = re.compile(r"^evidence_refs:\s*\[(?P<refs>.*)\]\s*$", re.M)
_LEARNING_FIELDS = re.compile(
    r"(?ms)^Trigger:\s*(?P<trigger>.*?)\s*\n\s*\n"
    r"Lesson:\s*(?P<lesson>.*?)\s*\n\s*\n"
    r"Instruction:\s*(?P<instruction>.*)\s*$"
)
_REGRESSION_CASES = re.compile(r"(?m)^- Case:")
_REGRESSION_EXPECTATIONS = re.compile(r"(?m)^[ \t]+Expected:")


class ProposalError(RuntimeError):
    """A second review or deterministic merge preparation check failed."""


class ActiveSkillConflict(ProposalError):
    """Active skill files changed while a proposal was being reviewed."""


class SecondReviewFailed(ProposalError):
    """The reviewer result could not be validated against the batch manifest."""


class BatchProposalService:
    """Validates second-review routing and persists inactive, hash-pinned proposals."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        storage: CandidateStorage,
        reviewer: SecondReviewerPort,
        *,
        skill_directory: Path = Path("skills"),
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.storage = storage
        self.reviewer = reviewer
        self.skill_directory = Path(skill_directory)

    def prepare(
        self,
        batch_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if (
            not self.settings.skill_updates_enabled
            or not self.settings.skill_update_consolidation_enabled
        ):
            raise ProposalError("skill_update_consolidation_disabled")
        checked_at = self._utc(now or datetime.now(UTC))
        try:
            candidates = self._load_batch(batch_id, worker_id, checked_at)
        except (ArtifactStorageError, ArtifactIntegrityError) as exc:
            reason = self._reason_code(exc)
            self._mark_failed(batch_id, reason)
            raise ProposalError(reason) from exc
        self._set_validating(batch_id, worker_id, checked_at)
        try:
            review_candidates = [self._read_and_validate_candidate(item) for item in candidates]
            for attempt in range(1, 4):
                try:
                    active = self._capture_active_skills()
                except ActiveSkillConflict:
                    continue
                request = self._review_request(batch_id, review_candidates, active)
                try:
                    raw_output = self.reviewer.review(request)
                except Exception as exc:
                    raise SecondReviewFailed("second_review_unavailable") from exc
                output = self._parse_output(raw_output)
                routings = self._check_routings(output, candidates)
                proposals, outcomes, targets = self._build_proposals(
                    candidates, review_candidates, routings, active
                )

                # Never publish a proposal built against a stale active-skill base.
                try:
                    current = self._capture_active_skills()
                except ActiveSkillConflict:
                    continue
                if current["hashes"] != active["hashes"]:
                    continue
                try:
                    result = self._persist_proposals(
                        batch_id,
                        worker_id,
                        candidates,
                        proposals,
                        outcomes,
                        targets,
                        active["hashes"],
                        attempt,
                        checked_at,
                    )
                except ActiveSkillConflict:
                    continue
                return result
            raise ActiveSkillConflict("active_skill_changed_during_revalidation")
        except (ArtifactStorageError, ArtifactIntegrityError, ProposalError, ValueError) as exc:
            self._mark_failed(batch_id, self._reason_code(exc))
            if isinstance(exc, ProposalError):
                raise
            raise ProposalError(self._reason_code(exc)) from exc

    def _load_batch(
        self, batch_id: uuid.UUID, worker_id: str, now: datetime
    ) -> list[SkillCandidateFile]:
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is None or batch.status not in {"consolidating", "validating"}:
                raise ProposalError("batch_not_ready_for_second_review")
            if not worker_id or batch.lease_owner != worker_id:
                raise ProposalError("batch_lease_owner_mismatch")
            if batch.lease_expires_at is not None and self._utc(batch.lease_expires_at) <= now:
                raise ProposalError("batch_lease_expired")
            try:
                ids = [uuid.UUID(value) for value in batch.candidate_ids_json]
            except (TypeError, ValueError) as exc:
                raise ArtifactIntegrityError("batch candidate list is invalid") from exc
            expected_count = len(batch.candidate_ids_json)
            if (
                not 1 <= expected_count <= 50
                or len(ids) != expected_count
                or len(set(ids)) != expected_count
            ):
                raise ArtifactIntegrityError("batch candidate count is invalid")
            rows = list(
                session.scalars(select(SkillCandidateFile).where(SkillCandidateFile.id.in_(ids)))
            )
            by_id = {row.id: row for row in rows}
            if len(by_id) != expected_count:
                raise ArtifactIntegrityError("batch candidate metadata is incomplete")
            ordered = [by_id[item_id] for item_id in ids]
            if any(
                row.status != "claimed"
                or row.candidate_type != "customer_skill"
                or row.source_conversation_type != batch.source_conversation_type
                or row.sha256 is None
                for row in ordered
            ):
                raise ArtifactIntegrityError("batch candidate metadata does not match its manifest")
            manifest = SkillBatchServiceManifest(
                batch_id=batch.id,
                source_type=batch.source_conversation_type,
                candidates=ordered,
            )
            if manifest.sha256 != batch.manifest_sha256:
                raise ArtifactIntegrityError("batch manifest hash does not match")
            if f"work/batch-{batch_id}-manifest.json" not in batch.artifact_paths_json:
                raise ArtifactIntegrityError("batch manifest path is missing from metadata")
            try:
                stored_manifest = self.storage.read_work_artifact(f"batch-{batch_id}-manifest.json")
            except ArtifactStorageError as exc:
                raise ArtifactIntegrityError("frozen batch manifest is missing") from exc
            if stored_manifest != manifest.content:
                raise ArtifactIntegrityError("frozen batch manifest differs from candidate records")
            records = batch.validation_json.get("consolidated_artifacts", [])
            if not isinstance(records, list) or not records:
                raise ArtifactIntegrityError("consolidated artifact hashes are missing")
            recorded_parts = {
                record.get("relative_path") for record in records if isinstance(record, dict)
            }
            expected_parts = {
                path for path in batch.artifact_paths_json if "-consolidated-" in path
            }
            if recorded_parts != expected_parts:
                raise ArtifactIntegrityError("consolidated artifact metadata does not match paths")
            candidate_parts = batch.validation_json.get("candidate_parts")
            if (
                not isinstance(candidate_parts, dict)
                or len(candidate_parts) != expected_count
                or set(candidate_parts) != {str(candidate.id) for candidate in ordered}
                or batch.validation_json.get("accounted_candidate_count") != expected_count
            ):
                raise ArtifactIntegrityError("consolidated candidate accounting is incomplete")
            for record in records:
                if not isinstance(record, dict):
                    raise ArtifactIntegrityError("consolidated artifact metadata is malformed")
                path = record.get("relative_path")
                digest = record.get("sha256")
                if not isinstance(path, str) or not path.startswith("work/"):
                    raise ArtifactIntegrityError("consolidated artifact path is invalid")
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ArtifactIntegrityError("consolidated artifact hash is invalid")
                artifact_bytes = self.storage.read_work_artifact(Path(path).name, digest)
                if len(artifact_bytes) != record.get("size_bytes"):
                    raise ArtifactIntegrityError("consolidated artifact size does not match")
            return ordered

    def _read_and_validate_candidate(self, candidate: SkillCandidateFile) -> MergeCandidateInput:
        assert candidate.source_conversation_type is not None and candidate.sha256 is not None
        entry = CandidateManifestEntry(
            source_type=candidate.source_conversation_type,
            sequence=candidate.sequence,
            relative_path=candidate.relative_path,
            sha256=candidate.sha256,
        )
        markdown = self.storage.read_candidate(entry)
        match = _FRONT_MATTER.match(markdown)
        if match is None:
            raise ArtifactIntegrityError("candidate front matter is invalid")
        header = match.group("header")
        body = match.group("body")
        expected_fields = {
            "candidate_id": str(candidate.id),
            "candidate_type": "customer_skill",
            "source_conversation_type": candidate.source_conversation_type,
            "content_fingerprint": candidate.content_fingerprint,
        }
        for field, value in expected_fields.items():
            if value is None or not re.search(
                rf"(?m)^{re.escape(field)}:\s*[\"']?{re.escape(str(value))}[\"']?\s*$",
                header,
            ):
                raise ArtifactIntegrityError(f"candidate {field} does not match metadata")
        refs_match = _REF_PATTERN.search(header)
        if refs_match is None:
            raise ArtifactIntegrityError("candidate evidence references are missing")
        refs = re.findall(r"[\"'](ev_[A-Za-z0-9_-]{1,80})[\"']", refs_match.group("refs"))
        residue = re.sub(r"[\"']ev_[A-Za-z0-9_-]{1,80}[\"']", "", refs_match.group("refs"))
        if (
            not refs
            or residue.strip().strip("[] ,")
            or refs != candidate.evidence_refs_json
            or len(set(refs)) != len(refs)
        ):
            raise ArtifactIntegrityError("candidate evidence references do not match metadata")
        if not candidate.source_snapshot_ids_json:
            raise ArtifactIntegrityError("candidate snapshot lineage is missing")
        if "## Learning" not in body or "## Regression cases" not in body:
            raise ArtifactIntegrityError("candidate sections are incomplete")
        rendered_cases = [
            self._markdown_unescape(value.strip())
            for value in re.findall(r"(?m)^- Case:\s*(.+)$", body)
        ]
        rendered_expectations = [
            self._markdown_unescape(value.strip())
            for value in re.findall(r"(?m)^\s+Expected:\s*(.+)$", body)
        ]
        case_count = len(_REGRESSION_CASES.findall(body))
        expected_count = len(_REGRESSION_EXPECTATIONS.findall(body))
        if (
            case_count == 0
            or case_count != expected_count
            or case_count != len(candidate.regression_cases_json)
        ):
            raise ArtifactIntegrityError("candidate regression cases do not match metadata")
        try:
            regression_cases = [
                RegressionCase.model_validate(item).model_dump()
                for item in candidate.regression_cases_json
            ]
        except ValueError as exc:
            raise ArtifactIntegrityError("candidate regression case schema is invalid") from exc
        if rendered_cases != [item["case"] for item in regression_cases] or (
            rendered_expectations != [item["expected_behavior"] for item in regression_cases]
        ):
            raise ArtifactIntegrityError("candidate regression content does not match metadata")
        learning = _candidate_learning(markdown)
        match_fields = _LEARNING_FIELDS.match(learning)
        if match_fields is None:
            raise ArtifactIntegrityError("candidate learning fields are malformed")
        trigger = self._markdown_unescape(match_fields.group("trigger").strip())
        lesson = self._markdown_unescape(match_fields.group("lesson").strip())
        instruction = self._markdown_unescape(match_fields.group("instruction").strip())
        if any(
            "\n" in item or "\r" in item or "\x00" in item
            for item in (trigger, lesson, instruction)
        ):
            raise ArtifactIntegrityError("candidate learning fields must be single-line text")
        policy_values = [trigger, lesson, instruction]
        for case in regression_cases:
            policy_values.extend([case["case"], case["expected_behavior"]])
        if _contains_personal_data(policy_values):
            raise ArtifactIntegrityError("candidate violates privacy checks")
        if _contains_unsupported_claim(policy_values):
            raise ArtifactIntegrityError("candidate violates policy checks")
        return MergeCandidateInput(
            candidate_id=candidate.id,
            source_type=cast(SkillTarget, candidate.source_conversation_type),
            learning=learning,
            evidence_refs=refs,
            regression_cases=regression_cases,
        )

    def _capture_active_skills(self) -> dict[str, Any]:
        before: dict[str, bytes] = {}
        for key, filename in ACTIVE_SKILL_MANIFEST.items():
            if key == "skill_reviewer":
                continue
            path = self.skill_directory / filename
            if path.is_symlink() or not path.is_file():
                raise ProposalError("active_skill_file_unavailable")
            content = path.read_bytes()
            if len(content) > self.settings.skill_update_active_skill_max_bytes:
                raise ProposalError("active_skill_exceeds_configured_size")
            before[key] = content
        skills = load_skills(self.skill_directory)
        after = {
            key: (self.skill_directory / ACTIVE_SKILL_MANIFEST[key]).read_bytes() for key in before
        }
        if before != after:
            raise ActiveSkillConflict("active_skill_changed_during_snapshot")
        return {
            "bytes": before,
            "hashes": {key: hashlib.sha256(value).hexdigest() for key, value in before.items()},
            "instructions": {key: skills[key].instructions for key in before},
        }

    def _review_request(
        self,
        batch_id: uuid.UUID,
        review_candidates: list[MergeCandidateInput],
        active: dict[str, Any],
    ) -> SecondReviewRequest:
        with self.session_factory() as session:
            batch = session.get(SkillConsolidationBatch, batch_id)
            if batch is None:
                raise ProposalError("batch_metadata_missing")
            request = SecondReviewRequest(
                batch_id=batch_id,
                source_type=cast(SkillTarget, batch.source_conversation_type),
                manifest_sha256=batch.manifest_sha256,
                candidates=review_candidates,
                active_skill_hashes=active["hashes"],
                active_skills=active["instructions"],
            )
        request_size = len(request.model_dump_json().encode())
        if request_size > self.settings.skill_update_merge_model_input_max_bytes:
            raise ProposalError("second_review_input_too_large")
        return request

    def _parse_output(self, raw: object) -> SecondReviewOutput:
        if isinstance(raw, SecondReviewOutput):
            encoded = raw.model_dump_json().encode()
            if len(encoded) > self.settings.skill_update_model_output_max_bytes:
                raise SecondReviewFailed("second_review_output_too_large")
            return raw
        if isinstance(raw, str):
            if len(raw.encode()) > self.settings.skill_update_model_output_max_bytes:
                raise SecondReviewFailed("second_review_output_too_large")
            return SecondReviewOutput.model_validate_json(raw)
        if isinstance(raw, dict):
            encoded = json.dumps(raw, ensure_ascii=False).encode()
            if len(encoded) > self.settings.skill_update_model_output_max_bytes:
                raise SecondReviewFailed("second_review_output_too_large")
            return SecondReviewOutput.model_validate(raw)
        raise SecondReviewFailed("second_review_output_invalid")

    @staticmethod
    def _check_routings(
        output: SecondReviewOutput, candidates: list[SkillCandidateFile]
    ) -> dict[uuid.UUID, CandidateRouting]:
        routed: dict[uuid.UUID, CandidateRouting] = {}
        for review in output.reviews:
            if review.candidate_id in routed:
                raise SecondReviewFailed("second_review_duplicate_candidate")
            routed[review.candidate_id] = review
        expected = {candidate.id for candidate in candidates}
        if set(routed) != expected:
            raise SecondReviewFailed("second_review_candidate_accounting_mismatch")
        return routed

    def _build_proposals(
        self,
        candidates: list[SkillCandidateFile],
        review_candidates: list[MergeCandidateInput],
        routings: dict[uuid.UUID, CandidateRouting],
        active: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any], list[str]]:
        by_id = {item.candidate_id: item for item in review_candidates}
        additions: dict[
            str, list[tuple[SkillCandidateFile, MergeCandidateInput, CandidateRouting]]
        ] = {}
        outcomes: dict[str, dict[str, str]] = {}
        seen_instructions: list[str] = []
        for candidate in candidates:
            item = by_id[candidate.id]
            route = routings[candidate.id]
            candidate_key = str(candidate.id)
            if route.decision == "reject":
                outcomes[candidate_key] = {
                    "decision": "rejected",
                    "reason_code": "second_review_rejected",
                }
                continue
            assert route.target_skill_key is not None
            instruction = self._instruction_text(item.learning)
            all_active_segments = [
                segment.strip()
                for body in active["instructions"].values()
                for segment in re.split(r"(?<=[.!?])\s+|\n+", body)
                if segment.strip()
            ]
            if any(_is_semantic_duplicate(instruction, segment) for segment in all_active_segments):
                outcomes[candidate_key] = {
                    "decision": "rejected",
                    "reason_code": "duplicate_active_skill",
                }
                continue
            if any(_is_semantic_duplicate(instruction, earlier) for earlier in seen_instructions):
                outcomes[candidate_key] = {
                    "decision": "rejected",
                    "reason_code": "duplicate_in_batch",
                }
                continue
            seen_instructions.append(instruction)
            additions.setdefault(route.target_skill_key, []).append((candidate, item, route))
            outcomes[candidate_key] = {
                "decision": "accepted",
                "reason_code": "validated",
                "target_skill_key": route.target_skill_key,
            }

        proposals: dict[str, str] = {}
        for target, items in additions.items():
            base_text = self._increment_release(active["bytes"][target].decode("utf-8"))
            blocks: list[str] = []
            for _candidate, item, _route in items:
                blocks.append(self._format_active_rule(item.learning))
            suffix = "\n\n## Reviewed learnings\n\n" + "\n\n".join(blocks) + "\n"
            proposed = base_text.rstrip() + suffix
            if len(proposed.encode("utf-8")) > self.settings.skill_update_active_skill_max_bytes:
                raise ProposalError(f"proposed_{target}_skill_exceeds_configured_size")
            added_text = "\n".join(blocks)
            if _contains_personal_data([added_text]) or _contains_unsupported_claim([added_text]):
                raise ProposalError("proposed_skill_violates_privacy_or_policy")
            if (
                any(pattern.search(added_text) for pattern in _CLAIM_PATTERNS)
                or _EMAIL.search(added_text)
                or _URL.search(added_text)
            ):
                raise ProposalError("proposed_skill_contains_disallowed_content")
            proposals[target] = proposed
        if len(outcomes) != len(candidates):
            raise ProposalError("second_review_accounting_incomplete")
        return proposals, outcomes, sorted(proposals)

    def _persist_proposals(
        self,
        batch_id: uuid.UUID,
        worker_id: str,
        candidates: list[SkillCandidateFile],
        proposals: dict[str, str],
        outcomes: dict[str, Any],
        targets: list[str],
        base_hashes: dict[str, str],
        attempts: int,
        checked_at: datetime,
    ) -> dict[str, Any]:
        if self._capture_active_skills()["hashes"] != base_hashes:
            raise ActiveSkillConflict("active_skill_changed_before_proposal_persist")
        proposal_artifacts: dict[str, dict[str, Any]] = {}
        for target, content in sorted(proposals.items()):
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            name = f"batch-{batch_id}-proposal-{target}-{digest[:12]}.md"
            artifact = self.storage.write_work_artifact(name, content)
            proposal_artifacts[target] = {
                "relative_path": artifact.relative_path,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "accepted_candidate_ids": [
                    candidate_id
                    for candidate_id, outcome in outcomes.items()
                    if outcome.get("decision") == "accepted"
                    and outcome.get("target_skill_key") == target
                ],
            }
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None or batch.status != "validating" or batch.lease_owner != worker_id:
                raise ProposalError("batch_state_or_lease_changed_before_proposal_persist")
            if batch.lease_expires_at is not None and self._utc(
                batch.lease_expires_at
            ) <= datetime.now(UTC):
                raise ProposalError("batch_lease_expired_before_proposal_persist")
            candidate_rows = list(
                session.scalars(
                    select(SkillCandidateFile)
                    .where(SkillCandidateFile.id.in_([item.id for item in candidates]))
                    .with_for_update()
                )
            )
            if len(candidate_rows) != len(candidates) or any(
                row.status != "claimed" for row in candidate_rows
            ):
                raise ProposalError("batch_candidates_changed_before_proposal_persist")
            for row in candidate_rows:
                outcome = outcomes.get(str(row.id))
                if outcome is None:
                    raise ProposalError("candidate_validation_outcome_missing")
                row.target_skill_key = (
                    outcome.get("target_skill_key")
                    if outcome.get("decision") == "accepted"
                    else None
                )
            paths = [path for path in batch.artifact_paths_json if "-proposal-" not in path]
            paths.extend(artifact["relative_path"] for artifact in proposal_artifacts.values())
            batch.artifact_paths_json = paths
            batch.target_skill_keys_json = targets
            batch.base_skill_hashes_json = base_hashes
            batch.validation_json = {
                **dict(batch.validation_json),
                "second_review": {
                    "status": "passed",
                    "reviewed_at": checked_at.isoformat(),
                    "active_revalidation_attempts": attempts,
                    "manifest_sha256": batch.manifest_sha256,
                    "candidate_count": len(candidates),
                    "accounted_candidate_count": len(outcomes),
                    "outcomes": outcomes,
                    "proposal_artifacts": proposal_artifacts,
                },
            }
            return {
                "batch_id": str(batch_id),
                "status": batch.status,
                "target_skill_keys": targets,
                "accounted_candidate_count": len(outcomes),
                "accepted_candidate_count": sum(
                    outcome["decision"] == "accepted" for outcome in outcomes.values()
                ),
                "rejected_candidate_count": sum(
                    outcome["decision"] == "rejected" for outcome in outcomes.values()
                ),
                "proposal_artifacts": proposal_artifacts,
                "base_skill_hashes": base_hashes,
            }

    def _set_validating(self, batch_id: uuid.UUID, worker_id: str, now: datetime) -> None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is None or batch.status not in {"consolidating", "validating"}:
                raise ProposalError("batch_not_ready_for_second_review")
            if batch.lease_owner != worker_id:
                raise ProposalError("batch_lease_owner_mismatch")
            if batch.lease_expires_at is not None and self._utc(batch.lease_expires_at) <= now:
                raise ProposalError("batch_lease_expired")
            batch.status = "validating"

    @staticmethod
    def _instruction_text(learning: str) -> str:
        match = _LEARNING_FIELDS.match(learning)
        if match is None:
            raise ArtifactIntegrityError("candidate learning fields are malformed")
        return BatchProposalService._markdown_unescape(match.group("instruction").strip())

    @staticmethod
    def _markdown_unescape(value: str) -> str:
        unescaped = html.unescape(value)
        return re.sub(r"\\([\\`*_{}\[\]()#+.!>|-])", r"\1", unescaped)

    @classmethod
    def _format_active_rule(cls, learning: str) -> str:
        match = _LEARNING_FIELDS.match(learning)
        if match is None:
            raise ArtifactIntegrityError("candidate learning fields are malformed")
        trigger = cls._markdown_unescape(match.group("trigger").strip())
        lesson = cls._markdown_unescape(match.group("lesson").strip())
        instruction = cls._markdown_unescape(match.group("instruction").strip())
        if any(_contains_personal_data([value]) for value in (trigger, lesson, instruction)):
            raise ProposalError("candidate_learning_contains_personal_data")
        return f"### {lesson}\n\n**Trigger:** {trigger}\n\n**Instruction:** {instruction}"

    @staticmethod
    def _increment_release(content: str) -> str:
        match = _FRONT_MATTER.match(content)
        if match is None:
            raise ProposalError("active_skill_front_matter_invalid")
        release = re.search(r"(?m)^release:\s*([0-9]+)\s*$", match.group("header"))
        if release is None:
            raise ProposalError("active_skill_release_not_numeric")
        next_release = str(int(release.group(1)) + 1)
        header_start = match.start("header")
        start, end = release.span(1)
        start += header_start
        end += header_start
        return content[:start] + next_release + content[end:]

    def _mark_failed(self, batch_id: uuid.UUID, reason: str) -> None:
        with self.session_factory.begin() as session:
            batch = session.scalar(
                select(SkillConsolidationBatch)
                .where(SkillConsolidationBatch.id == batch_id)
                .with_for_update()
            )
            if batch is not None and batch.status in {"consolidating", "validating"}:
                batch.status = "failed"
                batch.validation_json = {
                    **dict(batch.validation_json),
                    "second_review": {
                        "status": "failed",
                        "reason_code": reason,
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                }

    @staticmethod
    def _reason_code(error: Exception) -> str:
        value = str(error).strip().lower()
        return re.sub(r"[^a-z0-9]+", "_", value).strip("_")[:120] or "proposal_validation_failed"

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SkillBatchServiceManifest:
    """Canonical manifest adapter used to independently recheck M6 batch state."""

    def __init__(
        self,
        *,
        batch_id: uuid.UUID,
        source_type: str,
        candidates: list[SkillCandidateFile],
    ) -> None:
        records = [
            {
                "candidate_id": str(candidate.id),
                "sequence": candidate.sequence,
                "relative_path": candidate.relative_path,
                "sha256": candidate.sha256,
            }
            for candidate in candidates
        ]
        self.content = json.dumps(
            {
                "manifest_version": "customer-skill-batch-v1",
                "batch_id": str(batch_id),
                "source_type": source_type,
                "candidates": records,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.sha256 = hashlib.sha256(self.content).hexdigest()
