from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.models import Contact, Conversation, Message, PropertyRequirement, SkillUpdateRun

SELECTION_POLICY_VERSION = "closed-active-or-admin-owned-v4"
SOURCE_TYPES = frozenset({"buyer", "seller", "portfolio", "general", "unknown"})


def source_type_for_intent(intent: str | None) -> str:
    normalized = (intent or "").strip().casefold()
    if normalized in {"buy", "buyer", "rent", "renter"}:
        return "buyer"
    if normalized in {"sell", "seller"}:
        return "seller"
    if normalized in {"portfolio", "general", "unknown"}:
        return normalized
    if normalized == "mixed":
        return "general"
    return "unknown"


def source_type_for_intent_or_requirement(
    intent: str | None,
    transaction_type: str | None,
    skill_key: str | None = None,
) -> str:
    source_type = source_type_for_intent(intent)
    if source_type != "unknown":
        return source_type
    source_type = {"buy": "buyer", "rent": "buyer", "sell": "seller"}.get(transaction_type or "")
    if source_type:
        return source_type
    normalized_skill = (skill_key or "").strip().casefold()
    return normalized_skill if normalized_skill in SOURCE_TYPES else "unknown"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class SelectionCursor:
    selected_at: datetime
    conversation_id: uuid.UUID

    def to_json(self) -> dict[str, str]:
        return {
            "selected_at": _as_utc(self.selected_at).isoformat(),
            "conversation_id": str(self.conversation_id),
        }

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> SelectionCursor | None:
        if value is None:
            return None
        try:
            return cls(
                selected_at=_as_utc(
                    datetime.fromisoformat(
                        str(value["selected_at"] if "selected_at" in value else value["closed_at"])
                    )
                ),
                conversation_id=uuid.UUID(str(value["conversation_id"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid skill update selection cursor") from exc


@dataclass(frozen=True)
class SelectedConversation:
    conversation_id: uuid.UUID
    selected_at: datetime
    source_type: str
    conversation_state: str = "closed"
    snapshot_cutoff_at: datetime | None = None


@dataclass(frozen=True)
class SelectionPage:
    conversations: tuple[SelectedConversation, ...]
    window_start: datetime
    window_end: datetime
    cursor_in: SelectionCursor | None
    cursor_out: SelectionCursor | None
    limit: int
    has_more: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_version": SELECTION_POLICY_VERSION,
            "window_start": _as_utc(self.window_start).isoformat(),
            "window_end": _as_utc(self.window_end).isoformat(),
            "cursor_in": self.cursor_in.to_json() if self.cursor_in else None,
            "cursor_out": self.cursor_out.to_json() if self.cursor_out else None,
            "limit": self.limit,
            "selected_count": len(self.conversations),
            "has_more": self.has_more,
        }


def select_eligible_conversations(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    cursor: SelectionCursor | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    limit: int | None = None,
) -> SelectionPage:
    """Select eligible closed, active-agent, or admin-owned conversations.

    The caller persists `page.to_json()` on the run and reuses the same window
    and cursor on retry. Contacts with active retention and no legal hold are
    eligible regardless of messaging consent; customer-visible messages are required.
    """
    requested_limit = settings.skill_update_max_conversations_per_run if limit is None else limit
    if (
        not isinstance(requested_limit, int)
        or isinstance(requested_limit, bool)
        or requested_limit < 1
        or requested_limit > settings.skill_update_max_conversations_per_run
    ):
        raise ValueError("selection page limit is outside configured bounds")
    as_of = _as_utc(now or datetime.now(UTC))
    end = _as_utc(window_end) if window_end else as_of
    start = (
        _as_utc(window_start)
        if window_start
        else end - timedelta(days=settings.skill_update_lookback_days)
    )
    if start > end:
        raise ValueError("selection window start must not be after its end")

    active_activity_at = (
        select(func.max(Message.created_at))
        .where(
            Message.conversation_id == Conversation.id,
            Message.sender_type == "customer",
            Message.created_at <= end,
        )
        .scalar_subquery()
    )
    latest_visible_message_at = (
        select(func.max(Message.created_at))
        .where(
            Message.conversation_id == Conversation.id,
            Message.sender_type.in_({"customer", "automated_agent", "admin"}),
            Message.created_at <= end,
        )
        .scalar_subquery()
    )
    active_transaction_type = (
        select(PropertyRequirement.transaction_type)
        .where(
            PropertyRequirement.conversation_id == Conversation.id,
            PropertyRequirement.status == "active",
        )
        .order_by(PropertyRequirement.updated_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    last_agent_skill = (
        select(Message.skill_key)
        .where(
            Message.conversation_id == Conversation.id,
            Message.sender_type == "automated_agent",
            Message.skill_key.is_not(None),
            Message.created_at
            <= case((Conversation.state == "closed", Conversation.closed_at), else_=end),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    selection_at = case(
        (Conversation.state == "closed", Conversation.closed_at),
        else_=active_activity_at,
    )
    message_cutoff = case(
        (Conversation.state == "closed", Conversation.closed_at),
        else_=end,
    )
    customer_message_exists = (
        select(Message.id)
        .where(
            Message.conversation_id == Conversation.id,
            Message.sender_type == "customer",
            Message.created_at <= message_cutoff,
        )
        .exists()
    )
    statement = (
        select(
            Conversation.id,
            selection_at,
            Conversation.active_intent,
            Conversation.state,
            Conversation.closed_at,
            latest_visible_message_at,
            active_transaction_type,
            last_agent_skill,
        )
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(
            Conversation.state.in_({"closed", "active_agent", "admin_owned"}),
            selection_at >= start,
            selection_at <= end,
            Contact.retention_state == "active",
            Contact.legal_hold.is_(False),
            customer_message_exists,
        )
    )
    if cursor is not None:
        cursor_time = _as_utc(cursor.selected_at)
        statement = statement.where(
            or_(
                selection_at > cursor_time,
                and_(
                    selection_at == cursor_time,
                    Conversation.id > cursor.conversation_id,
                ),
            )
        )
    statement = (
        statement.order_by(selection_at, Conversation.id).limit(requested_limit + 1)
    )
    rows = list(session.execute(statement))
    has_more = len(rows) > requested_limit
    rows = rows[:requested_limit]
    selected_rows: list[SelectedConversation] = []
    for (
        conversation_id,
        selected_at,
        intent,
        conversation_state,
        closed_at,
        latest_message_at,
        transaction_type,
        agent_skill_key,
    ) in rows:
        conversation_id = cast(uuid.UUID, conversation_id)
        selected_at = cast(datetime | None, selected_at)
        intent = cast(str | None, intent)
        conversation_state = cast(str, conversation_state)
        closed_at = cast(datetime | None, closed_at)
        latest_message_at = cast(datetime | None, latest_message_at)
        if selected_at is None:
            continue
        selected_rows.append(
            SelectedConversation(
                conversation_id=conversation_id,
                selected_at=_as_utc(selected_at),
                source_type=source_type_for_intent_or_requirement(
                    intent, transaction_type, cast(str | None, agent_skill_key)
                ),
                conversation_state=conversation_state,
                snapshot_cutoff_at=(
                    _as_utc(closed_at)
                    if closed_at
                    else _as_utc(latest_message_at) if latest_message_at else end
                ),
            )
        )
    selected = tuple(selected_rows)
    cursor_out = (
        SelectionCursor(selected[-1].selected_at, selected[-1].conversation_id)
        if selected
        else cursor
    )
    return SelectionPage(selected, start, end, cursor, cursor_out, requested_limit, has_more)


def persist_selection_page(session: Session, run_id: uuid.UUID, page: SelectionPage) -> None:
    run = session.get(SkillUpdateRun, run_id)
    if run is None:
        raise ValueError("skill update run does not exist")
    selection = dict(run.selection_json)
    selection.update(page.to_json())
    run.selection_json = selection


def persist_snapshot_metadata(
    session: Session, run_id: uuid.UUID, metadata: dict[str, Any]
) -> None:
    """Persist only snapshot identity, hash, path, and expiry on the run row."""
    run = session.get(SkillUpdateRun, run_id)
    if run is None:
        raise ValueError("skill update run does not exist")
    selection = dict(run.selection_json)
    snapshots = {
        str(item["snapshot_id"]): dict(item)
        for item in selection.get("snapshot_artifacts", [])
        if isinstance(item, dict) and item.get("snapshot_id")
    }
    snapshot_id = str(metadata["snapshot_id"])
    prior = snapshots.get(snapshot_id)
    if prior is not None:
        old_expiry = datetime.fromisoformat(str(prior["expires_at"]))
        new_expiry = datetime.fromisoformat(str(metadata["expires_at"]))
        expiry = min(_as_utc(old_expiry), _as_utc(new_expiry)).isoformat()
        metadata = {**metadata, "expires_at": expiry}
    snapshots[snapshot_id] = dict(metadata)
    selection["snapshot_artifacts"] = sorted(
        snapshots.values(), key=lambda item: str(item["snapshot_id"])
    )
    run.selection_json = selection
    run.conversation_count = len(snapshots)
