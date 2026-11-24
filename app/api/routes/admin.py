import base64
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.api.dependencies import DbSession
from app.api.schemas.admin import (
    AdminMessageRequest,
    ConsentCorrectionRequest,
    ContextCorrectionRequest,
    ContextNoteRequest,
    ConversationCloseRequest,
    HandbackRequest,
    HandoverRequest,
    InteractiveMessageRequest,
    PortfolioAssetPatchRequest,
    PortfolioAssetRequest,
    ProposalDecisionRequest,
    RequirementCreateRequest,
    RequirementPatchRequest,
    RolloutAllowlistRequest,
    RolloutControlRequest,
    SkillReviewRequest,
    TranscriptExportRequest,
)
from app.auth.identity import AdminPrincipal, ensure_admin_user, require_roles
from app.config.settings import Settings, get_settings
from app.conversations.outbound import (
    DuplicateOutboundRequest,
    enqueue_outbound_message,
    get_outbound_by_idempotency_key,
)
from app.db.models import (
    AdminContextNote,
    AdminUser,
    AiRolloutAllowlist,
    AuditEvent,
    ClientContext,
    Contact,
    ContextFact,
    Conversation,
    CrmLink,
    HandoverCase,
    IntegrationEvent,
    Message,
    PortfolioAsset,
    PropertyRequirement,
    RuntimeControl,
    SkillImprovementProposal,
    SkillReviewRun,
    TranscriptExport,
)

router = APIRouter(prefix="/api", tags=["admin"])
ReadPrincipal = Annotated[
    AdminPrincipal, Depends(require_roles("admin", "supervisor", "read_only"))
]
WritePrincipal = Annotated[AdminPrincipal, Depends(require_roles("admin", "supervisor"))]
SupervisorPrincipal = Annotated[AdminPrincipal, Depends(require_roles("supervisor"))]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)]

_ACTIVE_HANDOVER = ("open", "assigned", "admin_owned")


def _encode(value: Any) -> dict[str, Any]:
    encoded = jsonable_encoder(value)
    if not isinstance(encoded, dict):
        raise TypeError("API response must be an object")
    return cast(dict[str, Any], encoded)


def _audit(
    session: Session,
    request: Request,
    principal: AdminPrincipal,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str,
    *,
    reason: str | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_type="admin",
            actor_id=principal.subject_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            reason=reason,
            after_json=after,
            request_id=getattr(request.state, "request_id", None),
        )
    )


def _cursor_encode(created_at: datetime, message_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{message_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _cursor_decode(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        timestamp, identifier = raw.split("|", maxsplit=1)
        created_at = datetime.fromisoformat(timestamp)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at, uuid.UUID(identifier)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid transcript cursor") from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _record_admin_facts(
    session: Session,
    *,
    contact_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    changes: dict[str, Any],
    admin_user: AdminUser,
) -> None:
    now = datetime.now(UTC)
    for field_key, value in changes.items():
        previous = session.scalar(
            select(ContextFact)
            .where(
                ContextFact.entity_type == entity_type,
                ContextFact.entity_id == entity_id,
                ContextFact.field_key == field_key,
                ContextFact.superseded_at.is_(None),
            )
            .with_for_update()
        )
        if previous is not None:
            previous.superseded_at = now
        session.add(
            ContextFact(
                contact_id=contact_id,
                entity_type=entity_type,
                entity_id=entity_id,
                field_key=field_key,
                value_json=_json_value(value),
                confidence=Decimal("1.000"),
                source_type="admin",
                created_by_admin_id=admin_user.id,
                valid_from=now,
            )
        )


def _message_view(message: Message) -> dict[str, Any]:
    return _encode(
        {
            "id": message.id,
            "direction": message.direction,
            "sender_type": message.sender_type,
            "sender_admin_id": message.sender_admin_id,
            "message_type": message.message_type,
            "body_text": message.body_text,
            "interactive_payload": message.interactive_payload,
            "reply_to_message_id": message.reply_to_message_id,
            "provider_timestamp": message.provider_timestamp,
            "delivery_status": message.delivery_status,
            "skill_key": message.skill_key,
            "application_release": message.application_release,
            "created_at": message.created_at,
        }
    )


def _activate_handover(
    session: Session,
    conversation: Conversation,
    admin_user: AdminUser,
    *,
    reason_code: str,
    reason_summary: str | None,
) -> HandoverCase:
    handover = session.scalar(
        select(HandoverCase)
        .where(
            HandoverCase.conversation_id == conversation.id,
            HandoverCase.status.in_(_ACTIVE_HANDOVER),
        )
        .with_for_update()
    )
    if handover is None:
        handover = HandoverCase(
            conversation_id=conversation.id,
            status="admin_owned",
            reason_code=reason_code,
            reason_summary=reason_summary,
            assigned_admin_id=admin_user.id,
        )
        session.add(handover)
    else:
        handover.status = "admin_owned"
        handover.assigned_admin_id = admin_user.id
        if reason_summary:
            handover.reason_summary = reason_summary
    conversation.state = "admin_owned"
    conversation.assigned_admin_id = admin_user.id
    conversation.version += 1
    session.flush()
    return handover


@router.get("/conversations")
def list_conversations(
    session: DbSession,
    principal: ReadPrincipal,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    state_filter: Annotated[str | None, Query(alias="state")] = None,
    handover_status: str | None = None,
    assigned_to_me: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    query = select(Conversation, Contact).join(Contact, Contact.id == Conversation.contact_id)
    if state_filter:
        if state_filter not in {"active_agent", "admin_owned", "closed"}:
            raise HTTPException(status_code=400, detail="invalid conversation state")
        query = query.where(Conversation.state == state_filter)
    active_case_exists = exists().where(
        HandoverCase.conversation_id == Conversation.id,
        HandoverCase.status.in_(_ACTIVE_HANDOVER),
    )
    if handover_status == "active":
        query = query.where(active_case_exists)
    elif handover_status == "none":
        query = query.where(~active_case_exists)
    elif handover_status:
        if handover_status not in _ACTIVE_HANDOVER:
            raise HTTPException(status_code=400, detail="invalid handover status")
        query = query.where(
            exists().where(
                HandoverCase.conversation_id == Conversation.id,
                HandoverCase.status == handover_status,
            )
        )
    if assigned_to_me:
        user = ensure_admin_user(session, principal)
        query = query.where(Conversation.assigned_admin_id == user.id)
    rows = list(
        session.execute(
            query.order_by(Conversation.updated_at.desc(), Conversation.id).limit(limit)
        )
    )
    now = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    for conversation, contact in rows:
        last_message = session.scalar(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        handover = session.scalar(
            select(HandoverCase)
            .where(
                HandoverCase.conversation_id == conversation.id,
                HandoverCase.status.in_(_ACTIVE_HANDOVER),
            )
            .limit(1)
        )
        opened = handover.opened_at if handover else None
        if opened is not None and opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        age_seconds = int((now - opened).total_seconds()) if opened else None
        results.append(
            {
                "id": conversation.id,
                "contact_id": contact.id,
                "contact_phone_e164": contact.phone_e164,
                "contact_display_name": contact.display_name,
                "state": conversation.state,
                "assigned_admin_id": conversation.assigned_admin_id,
                "handover": (
                    {
                        "id": handover.id,
                        "status": handover.status,
                        "reason_code": handover.reason_code,
                        "opened_at": handover.opened_at,
                        "age_seconds": age_seconds,
                        "overdue": age_seconds is not None
                        and age_seconds > settings.response_sla_minutes * 60,
                    }
                    if handover
                    else None
                ),
                "last_message": _message_view(last_message) if last_message else None,
                "updated_at": conversation.updated_at,
            }
        )
    _audit(session, request, principal, "conversation.queue_read", "conversation_queue", "queue")
    session.commit()
    return {"data": results, "limit": limit}


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: uuid.UUID,
    session: DbSession,
    principal: ReadPrincipal,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, Any]:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    contact = session.get(Contact, conversation.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    handover = session.scalar(
        select(HandoverCase)
        .where(
            HandoverCase.conversation_id == conversation_id,
            HandoverCase.status.in_(_ACTIVE_HANDOVER),
        )
        .limit(1)
    )
    facts_query = select(ContextFact).where(ContextFact.contact_id == contact.id)
    requirements = list(
        session.scalars(
            select(PropertyRequirement)
            .where(PropertyRequirement.contact_id == contact.id)
            .order_by(PropertyRequirement.created_at.desc())
        )
    )
    assets = list(
        session.scalars(
            select(PortfolioAsset)
            .where(PortfolioAsset.contact_id == contact.id)
            .order_by(PortfolioAsset.created_at.desc())
        )
    )
    crm_links = list(session.scalars(select(CrmLink).where(CrmLink.contact_id == contact.id)))
    before = _cursor_decode(cursor)
    messages_query = select(Message).where(Message.conversation_id == conversation_id)
    if before:
        before_time, before_id = before
        messages_query = messages_query.where(
            or_(
                Message.created_at < before_time,
                and_(Message.created_at == before_time, Message.id < before_id),
            )
        )
    messages = list(
        session.scalars(
            messages_query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
        )
    )
    has_more = len(messages) > limit
    page = messages[:limit]
    next_cursor = _cursor_encode(page[-1].created_at, page[-1].id) if has_more and page else None
    page.reverse()
    client_context = session.scalar(
        select(ClientContext).where(ClientContext.contact_id == contact.id)
    )
    context_facts = list(session.scalars(facts_query.order_by(ContextFact.created_at.desc())))
    audit_entity_id = uuid.uuid4()
    _audit(
        session,
        request,
        principal,
        "conversation.transcript_read",
        "conversation",
        conversation_id,
        after={"message_count": len(page), "audit_access_id": str(audit_entity_id)},
    )
    session.commit()
    return _encode(
        {
            "id": conversation.id,
            "contact": {
                "id": contact.id,
                "phone_e164": contact.phone_e164,
                "display_name": contact.display_name,
                "preferred_language": contact.preferred_language,
                "consent_status": contact.consent_status,
            },
            "state": conversation.state,
            "assigned_admin_id": conversation.assigned_admin_id,
            "version": conversation.version,
            "handover": handover,
            "client_context": client_context,
            "requirements": requirements,
            "portfolio_assets": assets,
            "context_facts": context_facts,
            "crm_links": crm_links,
            "messages": [_message_view(message) for message in page],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
    )


@router.get("/contacts/{contact_id}/context")
def get_contact_context(
    contact_id: uuid.UUID,
    session: DbSession,
    principal: ReadPrincipal,
    request: Request,
) -> dict[str, Any]:
    contact = session.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    context = session.scalar(select(ClientContext).where(ClientContext.contact_id == contact_id))
    requirements = list(
        session.scalars(
            select(PropertyRequirement)
            .where(PropertyRequirement.contact_id == contact_id)
            .order_by(PropertyRequirement.created_at.desc())
        )
    )
    assets = list(
        session.scalars(
            select(PortfolioAsset)
            .where(PortfolioAsset.contact_id == contact_id)
            .order_by(PortfolioAsset.created_at.desc())
        )
    )
    facts = list(
        session.scalars(
            select(ContextFact)
            .where(ContextFact.contact_id == contact_id)
            .order_by(ContextFact.created_at.desc())
        )
    )
    links = list(session.scalars(select(CrmLink).where(CrmLink.contact_id == contact_id)))
    _audit(session, request, principal, "contact.context_read", "contact", contact_id)
    session.commit()
    return _encode(
        {
            "contact_id": contact_id,
            "contact": contact,
            "client_context": context,
            "requirements": requirements,
            "portfolio_assets": assets,
            "provenance": facts,
            "crm_links": links,
        }
    )


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
def send_admin_message(
    conversation_id: uuid.UUID,
    body: AdminMessageRequest,
    idempotency_key: IdempotencyHeader,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    try:
        with session.begin():
            admin_user = ensure_admin_user(session, principal)
            conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            if conversation.state == "closed":
                raise HTTPException(status_code=409, detail="conversation is closed")
            _activate_handover(
                session,
                conversation,
                admin_user,
                reason_code="manual",
                reason_summary="Admin reply",
            )
            message, _ = enqueue_outbound_message(
                session,
                conversation_id=conversation_id,
                sender_type="admin",
                sender_admin_id=admin_user.id,
                body_text=body.body_text,
                idempotency_key=idempotency_key,
            )
            _audit(
                session,
                request,
                principal,
                "admin.message_queued",
                "message",
                message.id,
                after={"conversation_id": str(conversation_id), "delivery_status": "queued"},
            )
    except DuplicateOutboundRequest:
        existing, _event = get_outbound_by_idempotency_key(session, idempotency_key)
        if existing is None or existing.conversation_id != conversation_id:
            raise HTTPException(
                status_code=409, detail="idempotency key was used for another request"
            ) from None
        if existing.body_text != body.body_text or existing.sender_type != "admin":
            raise HTTPException(
                status_code=409, detail="idempotency key was used with different content"
            ) from None
        return {"id": existing.id, "status": existing.delivery_status, "duplicate": True}
    return {"id": message.id, "status": message.delivery_status, "duplicate": False}


@router.post("/conversations/{conversation_id}/handovers", status_code=status.HTTP_201_CREATED)
def create_or_takeover_handover(
    conversation_id: uuid.UUID,
    body: HandoverRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        conversation = session.scalar(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        if conversation.state == "closed":
            raise HTTPException(status_code=409, detail="conversation is closed")
        assigned_admin = admin_user
        if body.assigned_admin_subject:
            requested_admin = session.scalar(
                select(AdminUser).where(
                    AdminUser.subject_id == body.assigned_admin_subject,
                    AdminUser.is_active.is_(True),
                )
            )
            if requested_admin is None:
                raise HTTPException(status_code=404, detail="assigned administrator not found")
            assigned_admin = requested_admin
        if not body.take_over:
            handover = session.scalar(
                select(HandoverCase).where(
                    HandoverCase.conversation_id == conversation.id,
                    HandoverCase.status.in_(_ACTIVE_HANDOVER),
                )
            )
            if handover is None:
                handover = HandoverCase(
                    conversation_id=conversation.id,
                    status="assigned" if body.assigned_admin_subject else "open",
                    reason_code=body.reason_code,
                    reason_summary=body.reason_summary,
                    assigned_admin_id=assigned_admin.id if body.assigned_admin_subject else None,
                )
                session.add(handover)
        else:
            handover = _activate_handover(
                session,
                conversation,
                assigned_admin,
                reason_code=body.reason_code,
                reason_summary=body.reason_summary,
            )
        if not body.take_over and body.assigned_admin_subject:
            handover.assigned_admin_id = assigned_admin.id
            handover.status = "assigned"
        if not body.take_over:
            was_owned = conversation.state == "admin_owned"
            conversation.state = "admin_owned"
            if not was_owned:
                conversation.version += 1
        _audit(
            session,
            request,
            principal,
            "handover.opened_or_assigned",
            "handover",
            handover.id,
            reason=body.reason_summary,
            after={"status": handover.status, "reason_code": handover.reason_code},
        )
    return {
        "id": handover.id,
        "conversation_id": conversation_id,
        "status": handover.status,
        "reason_code": handover.reason_code,
        "assigned_admin_id": handover.assigned_admin_id,
    }


@router.get("/handovers/{handover_id}/context-notes")
def list_context_notes(
    handover_id: uuid.UUID,
    session: DbSession,
    principal: ReadPrincipal,
    request: Request,
) -> dict[str, Any]:
    handover = session.get(HandoverCase, handover_id)
    if handover is None:
        raise HTTPException(status_code=404, detail="handover not found")
    notes = list(
        session.scalars(
            select(AdminContextNote)
            .where(AdminContextNote.handover_case_id == handover_id)
            .order_by(AdminContextNote.created_at)
        )
    )
    _audit(session, request, principal, "private_context.read", "handover", handover_id)
    session.commit()
    return _encode({"handover_id": handover_id, "notes": notes})


@router.post("/handovers/{handover_id}/context-notes", status_code=status.HTTP_201_CREATED)
def add_context_note(
    handover_id: uuid.UUID,
    body: ContextNoteRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        handover = session.get(HandoverCase, handover_id)
        if handover is None or handover.status not in _ACTIVE_HANDOVER:
            raise HTTPException(status_code=404, detail="active handover not found")
        note = AdminContextNote(
            conversation_id=handover.conversation_id,
            handover_case_id=handover.id,
            admin_id=admin_user.id,
            note_text=body.note_text,
        )
        session.add(note)
        session.flush()
        _audit(
            session,
            request,
            principal,
            "private_context.added",
            "admin_context_note",
            note.id,
            after={"handover_id": str(handover.id)},
        )
    return {"id": note.id, "handover_id": handover_id, "created_at": note.created_at}


@router.post("/handovers/{handover_id}/handback")
def handback_handover(
    handover_id: uuid.UUID,
    body: HandbackRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        handover = session.scalar(
            select(HandoverCase).where(HandoverCase.id == handover_id).with_for_update()
        )
        if handover is None or handover.status not in _ACTIVE_HANDOVER:
            raise HTTPException(status_code=409, detail="handover is not active")
        conversation = session.scalar(
            select(Conversation)
            .where(Conversation.id == handover.conversation_id)
            .with_for_update()
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        now = datetime.now(UTC)
        handover.status = "handed_back"
        handover.handback_at = now
        handover.resolved_at = now
        conversation.state = "active_agent"
        conversation.assigned_admin_id = None
        conversation.version += 1
        if body.summary:
            session.add(
                AdminContextNote(
                    conversation_id=conversation.id,
                    handover_case_id=handover.id,
                    admin_id=admin_user.id,
                    note_text=f"Hand-back summary: {body.summary}",
                )
            )
        _audit(
            session,
            request,
            principal,
            "handover.handed_back",
            "handover",
            handover.id,
            reason=body.summary,
            after={"status": "handed_back", "conversation_version": conversation.version},
        )
    return {"id": handover_id, "status": handover.status, "conversation_state": conversation.state}


@router.patch("/contacts/{contact_id}/context")
def correct_client_context(
    contact_id: uuid.UUID,
    body: ContextCorrectionRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="no context fields supplied")
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        contact = session.scalar(select(Contact).where(Contact.id == contact_id).with_for_update())
        if contact is None:
            raise HTTPException(status_code=404, detail="contact not found")
        context = session.scalar(
            select(ClientContext).where(ClientContext.contact_id == contact_id).with_for_update()
        )
        if context is None:
            context = ClientContext(contact_id=contact_id, other_shared_context={})
            session.add(context)
            session.flush()
        for field_key, value in changes.items():
            if field_key == "other_shared_context" and value is None:
                value = {}
                changes[field_key] = value
            setattr(context, field_key, value)
        session.flush()
        _record_admin_facts(
            session,
            contact_id=contact_id,
            entity_type="client_context",
            entity_id=context.id,
            changes=changes,
            admin_user=admin_user,
        )
        _audit(
            session,
            request,
            principal,
            "contact.context_corrected",
            "contact",
            contact_id,
            after={"fields": list(changes)},
        )
    return _encode({"contact_id": contact_id, "client_context": context})


@router.patch("/contacts/{contact_id}/consent")
def correct_contact_consent(
    contact_id: uuid.UUID,
    body: ConsentCorrectionRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        contact = session.scalar(select(Contact).where(Contact.id == contact_id).with_for_update())
        if contact is None:
            raise HTTPException(status_code=404, detail="contact not found")
        if contact.consent_status != body.expected_status:
            raise HTTPException(
                status_code=409, detail="contact consent changed; refresh and retry"
            )
        source = session.scalar(
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == body.source_message_id,
                Message.direction == "inbound",
                Message.sender_type == "customer",
                Conversation.contact_id == contact_id,
            )
        )
        if source is None or not source[0].body_text:
            raise HTTPException(status_code=422, detail="consent must reference a customer message")
        command = source[0].body_text.strip().casefold()
        opt_out_commands = {"stop", "stop all", "unsubscribe", "cancel", "end", "quit"}
        opt_in_commands = {"start", "unstop"}
        approved_commands = (
            opt_in_commands if body.consent_status == "opted_in" else opt_out_commands
        )
        if command not in approved_commands:
            raise HTTPException(
                status_code=422, detail="source message is not an explicit consent command"
            )
        user = ensure_admin_user(session, principal)
        previous = contact.consent_status
        contact.consent_status = body.consent_status
        contact.consent_updated_at = datetime.now(UTC)
        conversation = source[1]
        if body.consent_status == "opted_out":
            conversation.state = "admin_owned"
            conversation.version += 1
            active_case = session.scalar(
                select(HandoverCase)
                .where(
                    HandoverCase.conversation_id == conversation.id,
                    HandoverCase.status.in_(_ACTIVE_HANDOVER),
                )
                .with_for_update()
            )
            if active_case is None:
                session.add(
                    HandoverCase(
                        conversation_id=conversation.id,
                        status="open",
                        reason_code="opt_out",
                        trigger_message_id=source[0].id,
                    )
                )
        _audit(
            session,
            request,
            principal,
            "contact.consent_updated",
            "contact",
            contact_id,
            reason=body.reason,
            after={
                "previous_status": previous,
                "consent_status": body.consent_status,
                "source_message_id": str(source[0].id),
                "updated_by": str(user.id),
            },
        )
    return _encode({"contact_id": contact_id, "consent_status": contact.consent_status})


@router.post("/contacts/{contact_id}/requirements", status_code=status.HTTP_201_CREATED)
def create_requirement(
    contact_id: uuid.UUID,
    body: RequirementCreateRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    values = body.model_dump()
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        if session.get(Contact, contact_id) is None:
            raise HTTPException(status_code=404, detail="contact not found")
        requirement = PropertyRequirement(contact_id=contact_id, **values)
        session.add(requirement)
        session.flush()
        _record_admin_facts(
            session,
            contact_id=contact_id,
            entity_type="requirement",
            entity_id=requirement.id,
            changes=values,
            admin_user=admin_user,
        )
        _audit(
            session,
            request,
            principal,
            "requirement.created",
            "requirement",
            requirement.id,
            after={"transaction_type": requirement.transaction_type},
        )
    return _encode({"id": requirement.id, "contact_id": contact_id, "requirement": requirement})


@router.patch("/requirements/{requirement_id}")
def correct_requirement(
    requirement_id: uuid.UUID,
    body: RequirementPatchRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="no requirement fields supplied")
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        requirement = session.scalar(
            select(PropertyRequirement)
            .where(PropertyRequirement.id == requirement_id)
            .with_for_update()
        )
        if requirement is None:
            raise HTTPException(status_code=404, detail="requirement not found")
        merged = {field: getattr(requirement, field) for field in changes}
        merged.update(changes)
        for field, empty_value in (("locations", []), ("preferences", {})):
            if field in changes and changes[field] is None:
                changes[field] = empty_value
                merged[field] = empty_value
        _validate_requirement_ranges(merged)
        for field, value in changes.items():
            setattr(requirement, field, value)
        session.flush()
        _record_admin_facts(
            session,
            contact_id=requirement.contact_id,
            entity_type="requirement",
            entity_id=requirement.id,
            changes=changes,
            admin_user=admin_user,
        )
        _audit(
            session,
            request,
            principal,
            "requirement.corrected",
            "requirement",
            requirement.id,
            after={"fields": list(changes)},
        )
    return _encode({"id": requirement_id, "requirement": requirement})


@router.post("/contacts/{contact_id}/portfolio-assets", status_code=status.HTTP_201_CREATED)
def create_portfolio_asset(
    contact_id: uuid.UUID,
    body: PortfolioAssetRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    values = body.model_dump()
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        if session.get(Contact, contact_id) is None:
            raise HTTPException(status_code=404, detail="contact not found")
        asset = PortfolioAsset(contact_id=contact_id, **values)
        session.add(asset)
        session.flush()
        _record_admin_facts(
            session,
            contact_id=contact_id,
            entity_type="portfolio_asset",
            entity_id=asset.id,
            changes=values,
            admin_user=admin_user,
        )
        _audit(
            session,
            request,
            principal,
            "portfolio_asset.created",
            "portfolio_asset",
            asset.id,
            after={"status": asset.status},
        )
    return _encode({"id": asset.id, "contact_id": contact_id, "portfolio_asset": asset})


@router.patch("/portfolio-assets/{asset_id}")
def correct_portfolio_asset(
    asset_id: uuid.UUID,
    body: PortfolioAssetPatchRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="no portfolio fields supplied")
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        asset = session.scalar(
            select(PortfolioAsset).where(PortfolioAsset.id == asset_id).with_for_update()
        )
        if asset is None:
            raise HTTPException(status_code=404, detail="portfolio asset not found")
        for field, value in changes.items():
            setattr(asset, field, value)
        session.flush()
        _record_admin_facts(
            session,
            contact_id=asset.contact_id,
            entity_type="portfolio_asset",
            entity_id=asset.id,
            changes=changes,
            admin_user=admin_user,
        )
        _audit(
            session,
            request,
            principal,
            "portfolio_asset.corrected",
            "portfolio_asset",
            asset.id,
            after={"fields": list(changes)},
        )
    return _encode({"id": asset_id, "portfolio_asset": asset})


@router.post(
    "/conversations/{conversation_id}/interactive-messages",
    status_code=status.HTTP_202_ACCEPTED,
)
def send_admin_interactive_fallback(
    conversation_id: uuid.UUID,
    body: InteractiveMessageRequest,
    idempotency_key: IdempotencyHeader,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    choices = "\n".join(f"{option.id}: {option.label}" for option in body.options)
    fallback = f"{body.prompt}\n{choices}\n\n{body.fallback_text}"
    queued = send_admin_message(
        conversation_id,
        AdminMessageRequest(body_text=fallback),
        idempotency_key,
        session,
        principal,
        request,
    )
    return {"message": queued, "type": "text_fallback", "interactive_supported": False}


@router.get("/contacts/{contact_id}/crm-status")
def get_crm_status(
    contact_id: uuid.UUID,
    session: DbSession,
    principal: ReadPrincipal,
    request: Request,
) -> dict[str, Any]:
    if session.get(Contact, contact_id) is None:
        raise HTTPException(status_code=404, detail="contact not found")
    links = list(
        session.scalars(
            select(CrmLink)
            .where(CrmLink.contact_id == contact_id)
            .order_by(CrmLink.updated_at.desc())
        )
    )
    _audit(session, request, principal, "crm.status_read", "contact", contact_id)
    session.commit()
    return _encode({"contact_id": contact_id, "links": links})


@router.post("/operations/crm-sync/{contact_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_crm_sync(
    contact_id: uuid.UUID,
    idempotency_key: IdempotencyHeader,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        contact = session.scalar(select(Contact).where(Contact.id == contact_id).with_for_update())
        if contact is None:
            raise HTTPException(status_code=404, detail="contact not found")
        bind = session.get_bind()
        insert_fn = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
        statement: Any = insert_fn(IntegrationEvent).values(
            integration="zoho",
            direction="outbound",
            event_type="zoho.sync_contact",
            external_event_id=idempotency_key,
            aggregate_type="contact",
            aggregate_id=contact_id,
            payload_json={"contact_id": str(contact_id)},
            status="pending",
        )
        event_id = session.execute(
            statement.on_conflict_do_nothing(
                index_elements=["integration", "external_event_id"],
                index_where=(
                    (IntegrationEvent.direction == "outbound")
                    & IntegrationEvent.external_event_id.is_not(None)
                ),
            ).returning(IntegrationEvent.id)
        ).scalar_one_or_none()
        if event_id is None:
            return {"contact_id": contact_id, "status": "already_queued", "duplicate": True}
        link = session.scalar(
            select(CrmLink)
            .where(
                CrmLink.contact_id == contact_id,
                CrmLink.requirement_id.is_(None),
                CrmLink.crm_system == "zoho",
            )
            .with_for_update()
        )
        if link is None:
            link = CrmLink(contact_id=contact_id, crm_system="zoho", sync_status="pending")
            session.add(link)
        else:
            link.sync_status = "pending"
            link.last_error_code = None
        _audit(
            session,
            request,
            principal,
            "crm.sync_retry_queued",
            "contact",
            contact_id,
            after={"event_id": str(event_id), "requested_by": str(admin_user.id)},
        )
    return {"contact_id": contact_id, "status": "queued", "event_id": event_id}


@router.post("/operations/transcript-exports", status_code=status.HTTP_202_ACCEPTED)
def create_transcript_export(
    body: TranscriptExportRequest,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    if body.from_date and body.to_date and body.from_date > body.to_date:
        raise HTTPException(status_code=422, detail="from_date must not exceed to_date")
    filters = _json_value(body.model_dump(mode="json", exclude={"deidentified"}))
    with session.begin():
        user = ensure_admin_user(session, principal)
        export = TranscriptExport(
            requested_by_admin_id=user.id,
            filters_json=filters,
            deidentified=body.deidentified,
            status="queued",
        )
        session.add(export)
        session.flush()
        _audit(
            session,
            request,
            principal,
            "transcript_export.queued",
            "transcript_export",
            export.id,
            reason="deidentified" if body.deidentified else "identifiable supervisor export",
            after={"deidentified": body.deidentified, "filters": filters},
        )
    return _encode({"id": export.id, "status": export.status, "deidentified": export.deidentified})


@router.get("/operations/transcript-exports/{export_id}")
def get_transcript_export(
    export_id: uuid.UUID,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    export = session.get(TranscriptExport, export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="transcript export not found")
    _audit(
        session, request, principal, "transcript_export.status_read", "transcript_export", export.id
    )
    session.commit()
    return _encode(
        {
            "id": export.id,
            "status": export.status,
            "deidentified": export.deidentified,
            "row_count": export.row_count,
            "checksum": export.checksum,
            "error_code": export.error_code,
            "requested_at": export.requested_at,
            "expires_at": export.expires_at,
        }
    )


@router.get("/operations/transcript-exports/{export_id}/download")
def download_transcript_export(
    export_id: uuid.UUID,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    export = session.get(TranscriptExport, export_id)
    if export is None or export.status != "ready" or export.artifact_uri is None:
        raise HTTPException(status_code=404, detail="transcript export is not available")
    if export.expires_at is not None and export.expires_at <= datetime.now(UTC):
        export.status = "expired"
        session.commit()
        raise HTTPException(status_code=410, detail="transcript export has expired")
    _audit(
        session, request, principal, "transcript_export.downloaded", "transcript_export", export.id
    )
    session.commit()
    if export.artifact_uri.startswith("s3://"):
        expected_prefix = (
            f"s3://{settings.exports_s3_bucket}/{settings.exports_s3_prefix.strip('/')}/"
        )
        if not settings.exports_s3_bucket or not export.artifact_uri.startswith(expected_prefix):
            raise HTTPException(status_code=404, detail="transcript export artifact is missing")
        bucket, _, key = export.artifact_uri[5:].partition("/")
        try:
            import boto3  # type: ignore[import-untyped]

            response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise HTTPException(
                status_code=404, detail="transcript export artifact is missing"
            ) from exc
        return StreamingResponse(
            response["Body"].iter_chunks(chunk_size=64 * 1024),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{export.id}.jsonl"'},
        )
    artifact = Path(export.artifact_uri).resolve()
    root = Path(settings.exports_directory).resolve()
    if artifact.parent != root or not artifact.is_file():
        raise HTTPException(status_code=404, detail="transcript export artifact is missing")
    return FileResponse(artifact, media_type="application/x-ndjson", filename=f"{export.id}.jsonl")


@router.post("/operations/skill-review-runs", status_code=status.HTTP_202_ACCEPTED)
def create_skill_review_run(
    body: SkillReviewRequest,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    if body.from_date > body.to_date:
        raise HTTPException(status_code=422, detail="from_date must not exceed to_date")
    user = ensure_admin_user(session, principal)
    run = SkillReviewRun(
        mode="manual",
        requested_by_admin_id=user.id,
        cohort_filters_json={
            "from_date": body.from_date.isoformat(),
            "to_date": body.to_date.isoformat(),
            "min_distinct_conversations": body.min_distinct_conversations,
        },
        reviewer_skill_key="skill_reviewer",
        application_release=get_settings().app_release,
        status="queued",
    )
    session.add(run)
    session.flush()
    _audit(
        session,
        request,
        principal,
        "skill_review.queued",
        "skill_review_run",
        run.id,
        after={"from_date": body.from_date.isoformat(), "to_date": body.to_date.isoformat()},
    )
    session.commit()
    return _encode(run)


@router.get("/operations/skill-review-runs/{run_id}")
def get_skill_review_run(
    run_id: uuid.UUID,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    run = session.get(SkillReviewRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="skill review run not found")
    _audit(session, request, principal, "skill_review.status_read", "skill_review_run", run.id)
    proposals = list(
        session.scalars(
            select(SkillImprovementProposal)
            .where(SkillImprovementProposal.skill_review_run_id == run_id)
            .order_by(SkillImprovementProposal.created_at)
        )
    )
    session.commit()
    return _encode({"run": run, "proposals": proposals})


@router.get("/operations/skill-improvement-proposals")
def list_skill_proposals(
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
    proposal_status: Annotated[str | None, Query(alias="status")] = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    query = select(SkillImprovementProposal).order_by(
        SkillImprovementProposal.created_at.desc(), SkillImprovementProposal.id
    )
    if proposal_status is not None:
        valid_statuses = {
            "pending_review",
            "approved",
            "rejected",
            "implemented",
            "deployed",
            "superseded",
        }
        if proposal_status not in valid_statuses:
            raise HTTPException(status_code=400, detail="invalid proposal status")
        query = query.where(SkillImprovementProposal.status == proposal_status)
    proposals = list(session.scalars(query.limit(limit)))
    _audit(
        session,
        request,
        principal,
        "skill_proposals.list_read",
        "skill_improvement_proposal",
        "collection",
        after={"count": len(proposals), "status": proposal_status},
    )
    session.commit()
    return _encode({"items": proposals, "count": len(proposals)})


@router.get("/operations/skill-improvement-proposals/{proposal_id}")
def get_skill_proposal(
    proposal_id: uuid.UUID,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    proposal = session.get(SkillImprovementProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="skill proposal not found")
    _audit(
        session,
        request,
        principal,
        "skill_proposal.read",
        "skill_improvement_proposal",
        proposal.id,
    )
    session.commit()
    return _encode(proposal)


@router.post("/operations/skill-improvement-proposals/{proposal_id}/decision")
def decide_skill_proposal(
    proposal_id: uuid.UUID,
    body: ProposalDecisionRequest,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        user = ensure_admin_user(session, principal)
        proposal = session.scalar(
            select(SkillImprovementProposal)
            .where(SkillImprovementProposal.id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise HTTPException(status_code=404, detail="skill proposal not found")
        if proposal.status != "pending_review":
            raise HTTPException(status_code=409, detail="skill proposal was already reviewed")
        proposal.status = "approved" if body.decision == "approve" else "rejected"
        proposal.reviewed_by_admin_id = user.id
        proposal.decision_reason = body.reason
        proposal.reviewed_at = datetime.now(UTC)
        _audit(
            session,
            request,
            principal,
            "skill_proposal.decision",
            "skill_improvement_proposal",
            proposal.id,
            reason=body.reason,
            after={"decision": body.decision},
        )
    return _encode(proposal)


@router.get("/operations/runtime-controls")
def get_runtime_controls(session: DbSession, principal: SupervisorPrincipal) -> dict[str, Any]:
    control = session.get(RuntimeControl, 1)
    if control is None:
        raise HTTPException(status_code=503, detail="runtime controls are not initialized")
    return _encode(control)


@router.patch("/operations/runtime-controls")
def update_runtime_controls(
    body: RolloutControlRequest,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        control = session.scalar(
            select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update()
        )
        if control is None:
            raise HTTPException(status_code=503, detail="runtime controls are not initialized")
        if control.version != body.expected_version:
            raise HTTPException(
                status_code=409, detail="runtime controls changed; refresh and retry"
            )
        before = {"ai_mode": control.ai_mode, "kill_switch": control.kill_switch}
        control.ai_mode = body.ai_mode
        control.kill_switch = body.kill_switch
        control.changed_by_admin_id = admin_user.id
        control.reason = body.reason
        control.version += 1
        control.updated_at = datetime.now(UTC)
        _audit(
            session,
            request,
            principal,
            "runtime_controls.updated",
            "runtime_controls",
            "1",
            reason=body.reason,
            after={**before, "ai_mode": control.ai_mode, "kill_switch": control.kill_switch},
        )
    return _encode(control)


@router.post("/operations/ai-rollout-allowlist/{contact_id}", status_code=status.HTTP_201_CREATED)
def allow_contact_for_canary(
    contact_id: uuid.UUID,
    body: RolloutAllowlistRequest,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    if body.expires_at is not None:
        if body.expires_at.tzinfo is None or body.expires_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=422, detail="allowlist expiry must be a future UTC timestamp"
            )
    with session.begin():
        admin_user = ensure_admin_user(session, principal)
        if session.get(Contact, contact_id) is None:
            raise HTTPException(status_code=404, detail="contact not found")
        entry = session.scalar(
            select(AiRolloutAllowlist)
            .where(AiRolloutAllowlist.contact_id == contact_id)
            .with_for_update()
        )
        if entry is None:
            entry = AiRolloutAllowlist(
                contact_id=contact_id,
                enabled_by_admin_id=admin_user.id,
                reason=body.reason,
                expires_at=body.expires_at,
            )
            session.add(entry)
        else:
            entry.enabled_by_admin_id = admin_user.id
            entry.reason = body.reason
            entry.expires_at = body.expires_at
        session.flush()
        _audit(
            session,
            request,
            principal,
            "canary_contact.enabled",
            "contact",
            contact_id,
            reason=body.reason,
            after={"expires_at": body.expires_at.isoformat() if body.expires_at else None},
        )
    return _encode(entry)


@router.delete("/operations/ai-rollout-allowlist/{contact_id}")
def remove_contact_from_canary(
    contact_id: uuid.UUID,
    session: DbSession,
    principal: SupervisorPrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        entry = session.scalar(
            select(AiRolloutAllowlist)
            .where(AiRolloutAllowlist.contact_id == contact_id)
            .with_for_update()
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="canary allowlist entry not found")
        entry.expires_at = datetime.now(UTC)
        _audit(
            session,
            request,
            principal,
            "canary_contact.disabled",
            "contact",
            contact_id,
        )
    return {"contact_id": contact_id, "enabled": False}


@router.post("/conversations/{conversation_id}/close")
def close_conversation(
    conversation_id: uuid.UUID,
    body: ConversationCloseRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        conversation = session.scalar(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        now = datetime.now(UTC)
        active_cases = list(
            session.scalars(
                select(HandoverCase)
                .where(
                    HandoverCase.conversation_id == conversation_id,
                    HandoverCase.status.in_(_ACTIVE_HANDOVER),
                )
                .with_for_update()
            )
        )
        for case in active_cases:
            case.status = "resolved"
            case.resolved_at = now
        conversation.state = "closed"
        conversation.closed_at = now
        conversation.assigned_admin_id = None
        conversation.version += 1
        _audit(
            session,
            request,
            principal,
            "conversation.closed",
            "conversation",
            conversation.id,
            reason=body.reason,
            after={"state": "closed", "version": conversation.version},
        )
    return {"id": conversation_id, "state": "closed"}


@router.post("/operations/conversations/{conversation_id}/resume")
def resume_conversation(
    conversation_id: uuid.UUID,
    body: ConversationCloseRequest,
    session: DbSession,
    principal: WritePrincipal,
    request: Request,
) -> dict[str, Any]:
    with session.begin():
        conversation = session.scalar(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        if conversation.state == "closed":
            raise HTTPException(status_code=409, detail="closed conversations cannot be resumed")
        has_active_handover = session.scalar(
            select(
                exists().where(
                    HandoverCase.conversation_id == conversation_id,
                    HandoverCase.status.in_(_ACTIVE_HANDOVER),
                )
            )
        )
        if has_active_handover:
            raise HTTPException(
                status_code=409, detail="active handover requires explicit hand-back"
            )
        contact = session.get(Contact, conversation.contact_id)
        if contact is None or contact.consent_status == "opted_out":
            raise HTTPException(status_code=409, detail="contact consent does not allow automation")
        conversation.state = "active_agent"
        conversation.assigned_admin_id = None
        conversation.version += 1
        _audit(
            session,
            request,
            principal,
            "conversation.resumed",
            "conversation",
            conversation_id,
            reason=body.reason,
            after={"state": "active_agent", "version": conversation.version},
        )
    return {"id": conversation_id, "state": "active_agent"}


@router.get("/operations/open-wa-status")
async def openwa_status(
    principal: ReadPrincipal,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    del principal
    if not settings.openwa_enabled:
        return {"enabled": False, "http_available": False, "connected": None, "session_ready": None}
    from app.integrations.openwa.client import OpenWaClient

    client = OpenWaClient(settings)
    try:
        health = await client.health()
    finally:
        await client.close()
    return {
        "enabled": True,
        "http_available": health.http_available,
        "connected": health.connected,
        "session_ready": health.session_ready,
    }


def _validate_requirement_ranges(values: dict[str, Any]) -> None:
    for minimum, maximum in (
        ("budget_min", "budget_max"),
        ("size_min", "size_max"),
        ("bedrooms_min", "bedrooms_max"),
        ("timing_from", "timing_to"),
    ):
        if values.get(minimum) is not None and values.get(maximum) is not None:
            if values[minimum] > values[maximum]:
                raise HTTPException(status_code=422, detail=f"{minimum} must not exceed {maximum}")
