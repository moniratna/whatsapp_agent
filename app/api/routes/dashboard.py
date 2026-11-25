"""Small presentation API for the Next.js dashboard."""

from collections import Counter
from datetime import UTC, datetime, timedelta
import hmac
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import DbSession
from app.auth.identity import AdminPrincipal, ensure_admin_user
from app.config.settings import Settings, get_settings
from app.db.models import (
    AuditEvent,
    AdminUser,
    Contact,
    ContextFact,
    Conversation,
    CrmLink,
    HandoverCase,
    Message,
    PortfolioAsset,
    PropertyRequirement,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
def dashboard_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    key: Annotated[str | None, Header(alias="X-Dashboard-Key")] = None,
) -> AdminPrincipal:
    configured = settings.dashboard_api_key.get_secret_value()
    if not configured or not key or not hmac.compare_digest(configured, key):
        raise HTTPException(status_code=401, detail="dashboard API key required")
    return AdminPrincipal("dashboard:static-login", "admin", "dashboard@local.invalid", "Dashboard Admin")


ReadAdmin = Annotated[AdminPrincipal, Depends(dashboard_admin)]
WriteAdmin = Annotated[AdminPrincipal, Depends(dashboard_admin)]
ACTIVE_HANDOVERS = ("open", "assigned", "admin_owned")


def _audit_read(session: Session, principal: AdminPrincipal, action: str, entity_type: str, entity_id: str) -> None:
    session.add(AuditEvent(actor_type="admin", actor_id=principal.subject_id,
        action=action, entity_type=entity_type, entity_id=entity_id))
    session.commit()


def _customer(session: Session, contact: Contact) -> dict[str, Any]:
    requirements = list(session.scalars(select(PropertyRequirement).where(
        PropertyRequirement.contact_id == contact.id, PropertyRequirement.status == "active"
    ).order_by(PropertyRequirement.updated_at.desc())))
    requirement = requirements[0] if requirements else None
    conversations = list(session.scalars(select(Conversation).where(
        Conversation.contact_id == contact.id
    ).order_by(Conversation.updated_at.desc())))
    conversation = conversations[0] if conversations else None
    messages = list(session.scalars(select(Message).where(
        Message.conversation_id.in_([item.id for item in conversations])
    ).order_by(Message.created_at.desc()).limit(100))) if conversations else []
    messages.reverse()
    analyzed_sentiments = Counter(
        message.sentiment for message in messages
        if message.direction == "inbound" and message.sentiment
    )
    analyzed_count = sum(analyzed_sentiments.values())
    sentiment_breakdown = {
        label: round(analyzed_sentiments[label] * 100 / analyzed_count) if analyzed_count else 0
        for label in ("positive", "neutral", "negative", "frustrated")
    }
    latest_sentiment = next(
        (message.sentiment for message in reversed(messages)
         if message.direction == "inbound" and message.sentiment),
        None,
    )
    crm_links = list(session.scalars(select(CrmLink).where(CrmLink.contact_id == contact.id)))
    portfolio = list(session.scalars(select(PortfolioAsset).where(PortfolioAsset.contact_id == contact.id)))
    facts = list(session.scalars(select(ContextFact).where(
        ContextFact.contact_id == contact.id, ContextFact.superseded_at.is_(None),
        ContextFact.confidence.is_not(None)
    )))
    confidence = round(sum(float(f.confidence) for f in facts) / len(facts) * 100) if facts else None
    location_data = (requirement.locations or []) if requirement else []
    locations = [str(item.get("name") or item.get("location") or item.get("label") or "")
                 for item in location_data if isinstance(item, dict)]
    locations = [item for item in locations if item]
    phone = contact.phone_e164
    name = contact.display_name or phone
    intent = ("Buy property" if requirement.transaction_type == "buy" else "Sell property") if requirement else "Unknown"
    crm_statuses = [link.sync_status for link in crm_links]
    crm = "Synced" if "synced" in crm_statuses else ("Failed" if "failed" in crm_statuses else "Pending")
    result: dict[str, Any] = {
        "id": str(contact.id), "name": name, "phone": phone, "email": None,
        "initials": "".join(part[0] for part in name.split()[:2]).upper() or "?",
        "color": "a2", "intent": intent, "location": ", ".join(locations) or "Not specified",
        "budget": (f"{requirement.currency or ''} {requirement.budget_max:,.0f}".strip()
                   if requirement and requirement.budget_max is not None else "Not specified"),
        "sentiment": latest_sentiment.title() if latest_sentiment else "Unknown",
        "confidence": confidence, "crm": crm,
        "lastActive": conversation.updated_at.isoformat() if conversation else None,
        "joined": contact.created_at.isoformat() if contact.created_at else None,
        "propertyType": requirement.property_type if requirement and requirement.property_type else "Not specified",
        "bedrooms": (str(requirement.bedrooms_min) if requirement and requirement.bedrooms_min is not None else "Not specified"),
        "purpose": requirement.purpose if requirement and requirement.purpose else "Not specified",
        "timeline": requirement.timing_text if requirement and requirement.timing_text else "Not specified",
        "payment": "Not specified", "tags": [requirement.match_readiness] if requirement else [],
        "buyIntent": 100 if requirement and requirement.transaction_type == "buy" else 0,
        "growthIntent": None, "sentimentBreakdown": sentiment_breakdown,
        "turns": [{"direction": message.direction, "source": {
                       "customer": "CUSTOMER", "automated_agent": "AI", "admin": "HUMAN", "system": "SYSTEM"
                   }.get(message.sender_type, "SYSTEM"),
                   "content": message.body_text or "", "sentiment": message.sentiment,
                   "confidence": float(message.sentiment_confidence) * 100 if message.sentiment_confidence is not None else None,
                   "time": message.created_at.isoformat()} for message in messages if message.body_text],
        "notes": requirement.notes if requirement else None,
        "crmExternalId": next((link.external_contact_id for link in crm_links if link.external_contact_id), None),
        "crmLastSyncedAt": next((link.last_synced_at.isoformat() for link in crm_links if link.last_synced_at), None),
        "transactionType": requirement.transaction_type if requirement else None,
        "country": None, "preferredLocations": locations, "preferredCommunities": [],
        "locationConfirmed": bool(locations), "emirate": None, "completionStatus": None,
        "qualificationStatus": requirement.match_readiness if requirement else "incomplete",
        "budgetMin": float(requirement.budget_min) if requirement and requirement.budget_min is not None else None,
        "budgetMax": float(requirement.budget_max) if requirement and requirement.budget_max is not None else None,
        "budgetCurrency": requirement.currency if requirement else None,
        "portfolio": [{"location": str(asset.location.get("name", "Not specified")) if isinstance(asset.location, dict) else "Not specified",
                       "propertyType": asset.property_type, "estimatedValue": float(asset.value) if asset.value is not None else None,
                       "currency": asset.value_currency, "status": asset.status, "intent": asset.ownership_status}
                      for asset in portfolio],
    }
    return result


@router.get("/customers")
def customers(session: DbSession, principal: ReadAdmin,
              q: str | None = None, intent: str | None = None,
              skip: int = Query(default=0, ge=0), limit: int = Query(default=25, ge=1, le=100)) -> dict[str, Any]:
    query = select(Contact).where(Contact.retention_state == "active")
    contacts = list(session.scalars(query.order_by(Contact.updated_at.desc())))
    rows = [_customer(session, contact) for contact in contacts]
    if q:
        search = q.casefold()
        rows = [row for row in rows if any(search in row[field].casefold() for field in ("name", "phone", "location"))]
    if intent and intent.lower() not in {"all", "unknown"}:
        aliases = {"buy property": "buy", "investment": "buy", "rent property": "rent", "sell property": "sell"}
        selected_intent = aliases.get(intent.lower(), intent.lower())
        rows = [row for row in rows if row["intent"].lower() == selected_intent]
    result = {"items": rows[skip:skip + limit], "total": len(rows), "skip": skip, "limit": limit}
    _audit_read(session, principal, "dashboard.customer_list_read", "customer_directory", "all")
    return result


@router.get("/customers/{contact_id}")
def customer(contact_id: UUID, session: DbSession, principal: ReadAdmin) -> dict[str, Any]:
    contact = session.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="customer not found")
    result = _customer(session, contact)
    _audit_read(session, principal, "dashboard.customer_profile_read", "contact", str(contact_id))
    return result


@router.get("/summary")
def summary(session: DbSession, principal: ReadAdmin,
            days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    contacts = list(session.scalars(select(Contact).where(Contact.retention_state == "active")))
    total = len(contacts)
    since = datetime.now(UTC) - timedelta(days=days)
    new_count = sum(1 for c in contacts if c.created_at and c.created_at.date() >= since.date())
    qualified = int(session.scalar(select(func.count()).select_from(PropertyRequirement).where(
        PropertyRequirement.status == "active", PropertyRequirement.match_readiness == "ready"
    )) or 0)
    synced = int(session.scalar(select(func.count(func.distinct(CrmLink.contact_id))).where(CrmLink.sync_status == "synced")) or 0)
    intents = Counter()
    for req in session.scalars(select(PropertyRequirement).where(PropertyRequirement.status == "active")):
        intents[req.transaction_type] += 1
    confidences = [float(value) for value in session.scalars(select(ContextFact.confidence).where(
        ContextFact.superseded_at.is_(None), ContextFact.confidence.is_not(None)
    ))]
    average_confidence = round(sum(confidences) * 100 / len(confidences)) if confidences else None
    sentiment_counts = Counter(session.scalars(select(Message.sentiment).where(
        Message.direction == "inbound", Message.sentiment.is_not(None)
    )))
    analyzed_sentiments = sum(sentiment_counts.values())
    result = {"totalCustomers": total, "qualifiedLeads": qualified,
            "positiveSentimentPct": round(sentiment_counts["positive"] * 100 / analyzed_sentiments) if analyzed_sentiments else None,
            "crmSynced": synced, "crmSyncPct": round(synced * 100 / total) if total else 0,
            "customerGrowthPct": None, "qualifiedGrowthPct": None, "newCustomers": new_count,
            "buyIntentPct": round(intents["buy"] * 100 / sum(intents.values())) if sum(intents.values()) else 0,
            "growthIntentPct": None, "averageConfidence": average_confidence,
            "sentimentCounts": dict(sentiment_counts),
            "intentCounts": dict(intents)}
    _audit_read(session, principal, "dashboard.summary_read", "dashboard", "summary")
    return result


@router.get("/reports")
def reports(session: DbSession, principal: ReadAdmin,
            days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    contacts = list(session.scalars(select(Contact).where(Contact.retention_state == "active")))
    by_day = Counter(c.created_at.date().isoformat() for c in contacts if c.created_at)
    requirements = list(session.scalars(select(PropertyRequirement).where(PropertyRequirement.status == "active")))
    intent = Counter(r.transaction_type for r in requirements)
    sentiment_counts = Counter(session.scalars(select(Message.sentiment).where(
        Message.direction == "inbound", Message.sentiment.is_not(None)
    )))
    analyzed_sentiments = sum(sentiment_counts.values())
    sentiment_distribution = [
        {"label": label.title(), "count": sentiment_counts[label],
         "percentage": round(sentiment_counts[label] * 100 / analyzed_sentiments)}
        for label in ("positive", "neutral", "negative", "frustrated")
        if sentiment_counts[label]
    ]
    confidence_values = [float(value) for value in session.scalars(select(ContextFact.confidence).where(
        ContextFact.superseded_at.is_(None), ContextFact.confidence.is_not(None)
    ))]
    confidence_buckets = Counter(min(4, int(value * 5)) for value in confidence_values)
    confidence_distribution = [
        {"label": f"{index * 20}–{(index + 1) * 20}%", "count": confidence_buckets[index],
         "percentage": round(confidence_buckets[index] * 100 / len(confidence_values))}
        for index in range(5) if confidence_values
    ]
    total = max(1, sum(intent.values()))
    breakdown = lambda counts: [{"label": key, "count": count, "percentage": round(count * 100 / total)} for key, count in counts.items()]
    result = {"customerGrowth": [{"label": key, "count": value} for key, value in sorted(by_day.items())[-days:]],
            "intentDistribution": breakdown(intent), "sentimentDistribution": sentiment_distribution,
            "investmentPurpose": breakdown(Counter(r.purpose for r in requirements if r.purpose)),
            "confidenceDistribution": confidence_distribution,
            "averageConfidence": round(sum(confidence_values) * 100 / len(confidence_values)) if confidence_values else None,
            "positiveSentimentPct": round(sentiment_counts["positive"] * 100 / analyzed_sentiments) if analyzed_sentiments else None,
            "buyIntentPct": round(intent.get("buy", 0) * 100 / total),
            "growthIntentPct": None, "newCustomers": sum(1 for c in contacts if c.created_at and c.created_at.date() >= (datetime.now(UTC) - timedelta(days=days)).date()),
            "totalCustomers": len(contacts)}
    _audit_read(session, principal, "dashboard.reports_read", "dashboard", "reports")
    return result


@router.get("/review-tasks")
def review_tasks(session: DbSession, principal: ReadAdmin,
                 status: str = "OPEN", limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    query = (
        select(HandoverCase, Conversation, Contact)
        .join(Conversation, Conversation.id == HandoverCase.conversation_id)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(HandoverCase.status.in_(ACTIVE_HANDOVERS))
        .order_by(HandoverCase.opened_at.desc())
        .limit(limit)
    )
    results = []
    for handover, conversation, contact in session.execute(query):
        assignee = session.get(AdminUser, handover.assigned_admin_id) if handover.assigned_admin_id else None
        last_message = session.scalar(select(Message).where(Message.conversation_id == conversation.id, Message.direction == "inbound").order_by(Message.created_at.desc()).limit(1))
        requirements = list(session.scalars(select(PropertyRequirement).where(PropertyRequirement.contact_id == contact.id, PropertyRequirement.status == "active")))
        results.append({"id": str(conversation.id), "clientId": str(contact.id), "name": contact.display_name or contact.phone_e164,
                        "phone": contact.phone_e164, "status": handover.status.upper(), "reason": handover.reason_summary or handover.reason_code,
                        "assignedTo": assignee.display_name if assignee else None, "createdAt": handover.opened_at.isoformat(),
                        "latestCustomerMessage": last_message.body_text if last_message else None,
                        "requirements": [{"transaction_type": r.transaction_type, "property_type": r.property_type,
                                          "budget_min": r.budget_min, "budget_max": r.budget_max, "locations": r.locations} for r in requirements]})
    _audit_read(session, principal, "dashboard.review_queue_read", "handover_queue", "active")
    return {"items": results}


@router.post("/review-tasks/{conversation_id}/take")
def take_task(conversation_id: UUID, session: DbSession, principal: WriteAdmin) -> dict[str, str]:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.state == "closed":
        raise HTTPException(status_code=404, detail="active conversation not found")
    admin = ensure_admin_user(session, principal)
    handover = session.scalar(select(HandoverCase).where(HandoverCase.conversation_id == conversation_id, HandoverCase.status.in_(ACTIVE_HANDOVERS)))
    if handover is None:
        handover = HandoverCase(conversation_id=conversation_id, status="admin_owned", reason_code="manual", reason_summary="Admin takeover", assigned_admin_id=admin.id)
        session.add(handover)
    else:
        handover.status = "admin_owned"
        handover.assigned_admin_id = admin.id
    conversation.state = "admin_owned"
    conversation.assigned_admin_id = admin.id
    conversation.version += 1
    session.add(AuditEvent(actor_type="admin", actor_id=principal.subject_id,
        action="dashboard.handover_taken", entity_type="conversation",
        entity_id=str(conversation.id), after_json={"state": "admin_owned"}))
    session.commit()
    return {"status": "admin_owned"}


@router.post("/review-tasks/{conversation_id}/reply")
def reply_task(conversation_id: UUID, body: dict[str, Any], session: DbSession, principal: WriteAdmin) -> dict[str, str]:
    from app.conversations.outbound import enqueue_outbound_message
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise HTTPException(status_code=422, detail="text must contain 1 to 4000 characters")
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.state == "closed":
        raise HTTPException(status_code=404, detail="active conversation not found")
    admin = ensure_admin_user(session, principal)
    if conversation.state != "admin_owned":
        conversation.state = "admin_owned"
        conversation.version += 1
    conversation.assigned_admin_id = admin.id
    handover = session.scalar(select(HandoverCase).where(HandoverCase.conversation_id == conversation_id, HandoverCase.status.in_(ACTIVE_HANDOVERS)))
    if handover is None:
        handover = HandoverCase(conversation_id=conversation_id, status="admin_owned", reason_code="manual", reason_summary="Admin reply", assigned_admin_id=admin.id)
        session.add(handover)
    else:
        handover.status = "admin_owned"
        handover.assigned_admin_id = admin.id
    message, _ = enqueue_outbound_message(session, conversation_id=conversation_id,
        sender_type="admin", sender_admin_id=admin.id, body_text=text,
        idempotency_key=f"dashboard-{conversation_id}-{datetime.now(UTC).timestamp()}")
    session.add(AuditEvent(actor_type="admin", actor_id=principal.subject_id,
        action="admin.message_queued", entity_type="message", entity_id=str(message.id),
        after_json={"conversation_id": str(conversation_id), "delivery_status": "queued"}))
    session.commit()
    return {"status": "queued"}


@router.post("/review-tasks/{conversation_id}/resume-ai")
def resume_task(conversation_id: UUID, session: DbSession, principal: WriteAdmin) -> dict[str, str]:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    contact = session.get(Contact, conversation.contact_id)
    if contact is None or contact.consent_status == "opted_out":
        raise HTTPException(status_code=409, detail="contact consent does not allow automation")
    cases = list(session.scalars(select(HandoverCase).where(HandoverCase.conversation_id == conversation_id, HandoverCase.status.in_(ACTIVE_HANDOVERS))))
    for case in cases:
        case.status = "handed_back"
        case.handback_at = datetime.now(UTC)
        case.resolved_at = case.handback_at
        session.add(AuditEvent(actor_type="admin", actor_id=principal.subject_id,
            action="handover.handed_back", entity_type="handover", entity_id=str(case.id),
            after_json={"status": "handed_back"}))
    conversation.state = "active_agent"
    conversation.assigned_admin_id = None
    conversation.version += 1
    session.commit()
    return {"status": "active_agent"}
