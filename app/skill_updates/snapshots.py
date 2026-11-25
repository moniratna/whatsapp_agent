from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Contact, Conversation, Message, PropertyRequirement
from app.skill_updates.deidentify import deidentify_text
from app.skill_updates.selection import (
    SELECTION_POLICY_VERSION,
    SelectedConversation,
    source_type_for_intent_or_requirement,
)
from app.skill_updates.storage import ArtifactStorageError, CandidateStorage, StoredArtifact
from app.skills.loader import ACTIVE_SKILL_MANIFEST, load_skills

SNAPSHOT_VERSION = "deidentified-conversation-v2"
_VISIBLE_SENDERS = ("customer", "automated_agent", "admin")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _opaque_ref(kind: str, value: uuid.UUID) -> str:
    return f"{kind}_{hashlib.sha256(value.bytes).hexdigest()[:20]}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SnapshotMessage:
    message_ref: str
    direction: str
    sender_type: str
    message_type: str
    body_text: str | None
    created_at: str


@dataclass(frozen=True)
class SkillVersionPins:
    application_release: str
    skill_releases: dict[str, str]
    skill_hashes: dict[str, str]
    reviewer_release: str
    reviewer_hash: str

    @classmethod
    def capture(cls, skill_directory: Path, application_release: str) -> SkillVersionPins:
        directory = Path(skill_directory)
        before = {
            key: (directory / filename).read_bytes()
            for key, filename in ACTIVE_SKILL_MANIFEST.items()
        }
        skills = load_skills(directory)
        after = {
            key: (directory / filename).read_bytes()
            for key, filename in ACTIVE_SKILL_MANIFEST.items()
        }
        if before != after:
            raise RuntimeError("active skills changed while snapshot versions were pinned")
        hashes = {key: hashlib.sha256(raw).hexdigest() for key, raw in before.items()}
        return cls(
            application_release=application_release,
            skill_releases={key: skills[key].header.release for key in ACTIVE_SKILL_MANIFEST},
            skill_hashes=hashes,
            reviewer_release=skills["skill_reviewer"].header.release,
            reviewer_hash=hashes["skill_reviewer"],
        )


@dataclass(frozen=True)
class ConversationSnapshot:
    conversation_id: uuid.UUID
    conversation_ref: str
    source_type: str
    closed_at: str | None
    conversation_state: str
    snapshot_cutoff_at: str
    messages: tuple[SnapshotMessage, ...]
    pins: SkillVersionPins
    snapshot_sha256: str
    snapshot_id: str
    captured_at: datetime

    def payload(self) -> dict[str, Any]:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "conversation_ref": self.conversation_ref,
            "source_conversation_type": self.source_type,
            "closed_at": self.closed_at,
            "conversation_state": self.conversation_state,
            "snapshot_cutoff_at": self.snapshot_cutoff_at,
            "messages": [asdict(message) for message in self.messages],
            "application_release": self.pins.application_release,
            "skill_releases": self.pins.skill_releases,
            "skill_hashes": self.pins.skill_hashes,
            "reviewer_release": self.pins.reviewer_release,
            "reviewer_hash": self.pins.reviewer_hash,
        }


@dataclass(frozen=True)
class SnapshotArtifactMetadata:
    snapshot_id: str
    conversation_id: str
    conversation_ref: str
    source_type: str
    relative_path: str
    artifact_sha256: str
    snapshot_sha256: str
    captured_at: str
    expires_at: str
    application_release: str
    reviewer_release: str
    reviewer_hash: str

    def to_json(self) -> dict[str, str]:
        return asdict(self)


class SnapshotIneligible(ValueError):
    """The source conversation no longer satisfies snapshot eligibility."""


class ConversationSnapshotBuilder:
    def __init__(
        self,
        *,
        skill_directory: Path,
        application_release: str,
        lookback_days: int = 90,
        retention_days: int = 7,
        max_snapshot_bytes: int = 1_048_576,
    ) -> None:
        if not 1 <= lookback_days <= 365:
            raise ValueError("lookback_days must be between 1 and 365")
        if not 1 <= retention_days <= 30:
            raise ValueError("retention_days must be between 1 and 30")
        if max_snapshot_bytes < 1:
            raise ValueError("max_snapshot_bytes must be positive")
        self.lookback_days = lookback_days
        self.retention_days = retention_days
        self.max_snapshot_bytes = max_snapshot_bytes
        self.pins = SkillVersionPins.capture(skill_directory, application_release)

    @classmethod
    def from_settings(
        cls, settings: Any, *, skill_directory: Path = Path("skills")
    ) -> ConversationSnapshotBuilder:
        return cls(
            skill_directory=skill_directory,
            application_release=settings.app_release,
            lookback_days=settings.skill_update_lookback_days,
            retention_days=settings.skill_update_snapshot_retention_days,
            max_snapshot_bytes=settings.skill_update_snapshot_max_bytes,
        )

    def build(
        self,
        session: Session,
        selected: SelectedConversation,
        *,
        now: datetime | None = None,
    ) -> ConversationSnapshot:
        captured_at = _as_utc(now or datetime.now(UTC))
        eligible_after = captured_at - timedelta(days=self.lookback_days)
        selected_at = _as_utc(selected.selected_at)
        if selected_at < eligible_after or selected_at > captured_at:
            raise SnapshotIneligible("conversation is outside the eligible selection window")

        source = session.execute(
            select(Conversation, Contact)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(
                Conversation.id == selected.conversation_id,
                Conversation.state.in_({"closed", "active_agent"}),
            )
            .with_for_update()
        ).one_or_none()
        if source is None:
            raise SnapshotIneligible("conversation is no longer eligible for skill updates")
        conversation, contact = source
        cutoff = selected.snapshot_cutoff_at
        if cutoff is None:
            cutoff = (
                conversation.closed_at
                if conversation.state == "closed" and conversation.closed_at is not None
                else captured_at
            )
        cutoff_at = _as_utc(cutoff)
        if conversation.state == "closed" and conversation.closed_at is not None:
            cutoff_at = min(cutoff_at, _as_utc(conversation.closed_at))
        if selected.conversation_state == "closed":
            if conversation.state != "closed" or conversation.closed_at is None:
                raise SnapshotIneligible("conversation close state changed after selection")
            actual_selection_at = _as_utc(conversation.closed_at)
        else:
            if conversation.state not in {"active_agent", "closed"}:
                raise SnapshotIneligible("conversation is no longer customer eligible")
            last_customer_message_at = session.scalar(
                select(func.max(Message.created_at)).where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type == "customer",
                    Message.created_at <= cutoff_at,
                )
            )
            if last_customer_message_at is None:
                raise SnapshotIneligible(
                    "conversation has no customer message in the selection window"
                )
            actual_selection_at = _as_utc(last_customer_message_at)
        if actual_selection_at != selected_at:
            raise SnapshotIneligible("conversation selection timestamp changed")
        if contact.retention_state != "active" or contact.legal_hold:
            raise SnapshotIneligible("conversation retention, legal hold, or close state changed")
        if cutoff_at > captured_at or cutoff_at < selected_at:
            raise SnapshotIneligible("conversation snapshot cutoff is outside its selection window")
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
                Message.created_at <= cutoff_at,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        source_type = source_type_for_intent_or_requirement(
            conversation.active_intent, transaction_type, agent_skill_key
        )
        if source_type != selected.source_type:
            raise SnapshotIneligible("conversation intent changed after selection")

        messages = list(
            session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type.in_(_VISIBLE_SENDERS),
                    Message.created_at <= cutoff_at,
                )
                .order_by(Message.created_at, Message.id)
            )
        )
        if not any(message.sender_type == "customer" for message in messages):
            raise SnapshotIneligible("conversation has no customer-visible message history")

        snapshot_messages = tuple(
            SnapshotMessage(
                message_ref=_opaque_ref("ev", message.id),
                direction=message.direction,
                sender_type=message.sender_type,
                message_type=message.message_type,
                body_text=deidentify_text(message.body_text),
                created_at=_as_utc(message.created_at).isoformat(),
            )
            for message in messages
        )
        conversation_ref = _opaque_ref("conv", conversation.id)
        payload = {
            "snapshot_version": SNAPSHOT_VERSION,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "conversation_ref": conversation_ref,
            "source_conversation_type": source_type,
            "closed_at": (
                _as_utc(conversation.closed_at).isoformat()
                if conversation.closed_at is not None
                else None
            ),
            "conversation_state": conversation.state,
            "snapshot_cutoff_at": cutoff_at.isoformat(),
            "messages": [asdict(message) for message in snapshot_messages],
            "application_release": self.pins.application_release,
            "skill_releases": self.pins.skill_releases,
            "skill_hashes": self.pins.skill_hashes,
            "reviewer_release": self.pins.reviewer_release,
            "reviewer_hash": self.pins.reviewer_hash,
        }
        snapshot_sha256 = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
        snapshot_id = hashlib.sha256(
            f"{conversation_ref}:{snapshot_sha256}".encode()
        ).hexdigest()[:32]
        return ConversationSnapshot(
            conversation_id=conversation.id,
            conversation_ref=conversation_ref,
            source_type=source_type,
            closed_at=(
                _as_utc(conversation.closed_at).isoformat()
                if conversation.closed_at is not None
                else None
            ),
            conversation_state=conversation.state,
            snapshot_cutoff_at=cutoff_at.isoformat(),
            messages=snapshot_messages,
            pins=self.pins,
            snapshot_sha256=snapshot_sha256,
            snapshot_id=snapshot_id,
            captured_at=captured_at,
        )

    def store(
        self, snapshot: ConversationSnapshot, storage: CandidateStorage
    ) -> SnapshotArtifactMetadata:
        payload = {
            "snapshot": snapshot.payload(),
            "snapshot_sha256": snapshot.snapshot_sha256,
        }
        content = _canonical_json(payload)
        encoded = content.encode()
        if len(encoded) > self.max_snapshot_bytes:
            raise ArtifactStorageError("de-identified snapshot exceeds its configured byte limit")
        filename = f"snapshot_{snapshot.snapshot_id}.json"
        try:
            existing = storage.read_work_artifact(filename)
        except ArtifactStorageError as exc:
            if "missing" not in str(exc):
                raise
            stored = storage.write_work_artifact(filename, content)
        else:
            if existing != encoded:
                raise ArtifactStorageError("existing snapshot artifact content does not match ID")
            stored = StoredArtifact(
                relative_path=f"work/{filename}",
                sha256=hashlib.sha256(encoded).hexdigest(),
                size_bytes=len(encoded),
            )
        expires_at = snapshot.captured_at + timedelta(days=self.retention_days)
        return SnapshotArtifactMetadata(
            snapshot_id=snapshot.snapshot_id,
            conversation_id=str(snapshot.conversation_id),
            conversation_ref=snapshot.conversation_ref,
            source_type=snapshot.source_type,
            relative_path=stored.relative_path,
            artifact_sha256=stored.sha256,
            snapshot_sha256=snapshot.snapshot_sha256,
            captured_at=snapshot.captured_at.isoformat(),
            expires_at=expires_at.isoformat(),
            application_release=self.pins.application_release,
            reviewer_release=self.pins.reviewer_release,
            reviewer_hash=self.pins.reviewer_hash,
        )


def delete_snapshot_artifact(
    storage: CandidateStorage,
    metadata: dict[str, Any],
    *,
    now: datetime | None = None,
    force: bool = False,
) -> bool:
    """Delete one explicitly recorded snapshot after expiry or a source deletion.

    `force=True` is reserved for consent/deletion workflows. No directory scan or
    wildcard removal is used, and the stored file hash must still match.
    """
    expires_at = _as_utc(datetime.fromisoformat(str(metadata["expires_at"])))
    if not force and _as_utc(now or datetime.now(UTC)) < expires_at:
        return False
    relative = Path(str(metadata["relative_path"]))
    if len(relative.parts) != 2 or relative.parts[0] != "work":
        raise ArtifactStorageError("snapshot metadata has an invalid work artifact path")
    storage.delete_work_artifact(relative.parts[1], str(metadata["artifact_sha256"]))
    return True
