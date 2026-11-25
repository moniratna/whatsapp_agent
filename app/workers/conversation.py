from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.conversations.outbound import enqueue_outbound_message
from app.db.models import (
    AdminContextNote,
    AgentRun,
    AgentRunDraft,
    AiRolloutAllowlist,
    AuditEvent,
    ClientContext,
    Contact,
    ContextFact,
    Conversation,
    HandoverCase,
    IntegrationEvent,
    Message,
    PortfolioAsset,
    PropertyRequirement,
    RuntimeControl,
)
from app.integrations.bedrock.provider import LlmPort
from app.skills.loader import load_skills, select_skill

logger = logging.getLogger("whatsapp_assistant.conversation_worker")


def _handover_reason_fields(reason: str | None) -> tuple[str, str | None]:
    value = (reason or "").strip()
    if value and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        return value, None
    return "model_handover", value[:2000] or None


def _requirement_missing_core_fields(requirement: PropertyRequirement) -> list[str]:
    missing: list[str] = []
    if not requirement.purpose:
        missing.append("purpose")
    if not requirement.locations:
        missing.append("location")
    if not requirement.property_type:
        missing.append("property type")
    bedroom_preferences = requirement.preferences or {}
    if (
        requirement.bedrooms_min is None
        and requirement.bedrooms_max is None
        and not any(
            key in bedroom_preferences
            for key in ("bedroom_configuration", "bedrooms", "bhk", "rooms")
        )
    ):
        missing.append("bedroom configuration")
    if requirement.budget_min is None and requirement.budget_max is None:
        missing.append("budget")
    if not requirement.timing_text:
        missing.append("timing")
    practical_preferences = {
        key: value
        for key, value in (requirement.preferences or {}).items()
        if key not in {"bedroom_configuration", "bedrooms", "bhk", "rooms"}
    }
    if not practical_preferences and not requirement.notes:
        missing.append("amenities or practical preferences")
    return missing


def _extract_explicit_budget(text: str) -> dict[str, str]:
    """Parse a clear numeric budget from common customer wording."""
    marker = re.search(r"\b(?:budget|price range|spend)\b", text, re.IGNORECASE)
    segment = text[marker.end() :] if marker else text
    segment = re.split(r"[;\n.!?]", segment, maxsplit=1)[0]
    number = r"\d[\d,]*(?:\.\d+)?"
    unit = r"crores?|cr|lakhs?|lacs?|lac|l|millions?|mn|thousands?|k"
    range_match = re.search(
        rf"({number})\s*(?:-|to)\s*({number})\s*({unit})\b",
        segment,
        re.IGNORECASE,
    )
    if range_match:
        low, high = Decimal(range_match.group(1).replace(",", "")), Decimal(
            range_match.group(2).replace(",", "")
        )
        suffix = range_match.group(3).casefold()
        multiplier = (
            Decimal(100_000)
            if suffix.startswith(("lakh", "lac")) or suffix == "l"
            else Decimal(10_000_000)
            if suffix.startswith(("crore", "cr"))
            else Decimal(1_000_000)
            if suffix.startswith(("million", "mn"))
            else Decimal(1_000)
        )
        return {
            "budget_min": str(low * multiplier),
            "budget_max": str(high * multiplier),
        }
    amount_match = re.search(rf"({number})\s*({unit})\b", segment, re.IGNORECASE)
    if amount_match:
        amount = Decimal(amount_match.group(1).replace(",", ""))
        suffix = amount_match.group(2).casefold()
        multiplier = (
            Decimal(100_000)
            if suffix.startswith(("lakh", "lac")) or suffix == "l"
            else Decimal(10_000_000)
            if suffix.startswith(("crore", "cr"))
            else Decimal(1_000_000)
            if suffix.startswith(("million", "mn"))
            else Decimal(1_000)
        )
        return {"budget_max": str(amount * multiplier)}
    # A plain number is safe only when the customer also gave an explicit currency.
    if re.search(r"(?:₹|\b(?:INR|USD|EUR|GBP|AED|CAD|AUD)\b)", segment, re.IGNORECASE):
        amount_match = re.search(number, segment)
        if amount_match:
            amount = Decimal(amount_match.group().replace(",", ""))
            return {"budget_max": str(amount)}
    return {}


def _is_contextual_reply(text: str, previous_assistant_message: str = "") -> bool:
    """Keep direct answers in context while routing unknown questions to review."""
    normalized = text.casefold().strip()
    words = re.findall(r"[a-z0-9']+", normalized)
    if not words or "?" in normalized:
        return False
    if words[0] in {
        "what", "why", "how", "when", "where", "who", "can", "could", "would",
        "will", "is", "are", "do", "does", "did", "should",
    }:
        return False
    unsupported_markers = (
        "guarantee", "worth", "value", "price", "return", "forecast", "predict", "appreciation",
        "profit", "roi", "available", "availability", "recommend", "legal", "lawyer", "tax", "mortgage",
    )
    if any(marker in normalized for marker in unsupported_markers):
        return False
    return len(words) <= 4 or previous_assistant_message.rstrip().endswith("?")


def _is_unverified_claim_request(text: str) -> bool:
    normalized = text.casefold()
    markers = (
        "guarantee", "worth", "future value", "appreciation", "forecast", "predict",
        "roi", "return on investment", "investment return", "profit",
    )
    return any(marker in normalized for marker in markers)


class ConversationWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LlmPort,
        settings: Settings,
        *,
        skill_directory: Path | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.llm = llm
        self.settings = settings
        self.skill_directory = skill_directory or Path("skills")
        self.worker_id = worker_id or str(uuid.uuid4())

    def claim_one(self, now: datetime | None = None) -> uuid.UUID | None:
        now = now or datetime.now(UTC)
        with self.session_factory.begin() as session:
            events = list(
                session.scalars(
                    select(IntegrationEvent)
                    .where(
                        IntegrationEvent.integration == "app",
                        IntegrationEvent.event_type == "conversation.process_message",
                        IntegrationEvent.direction == "outbound",
                        or_(
                            IntegrationEvent.status.in_(("pending", "retry_wait")),
                            (IntegrationEvent.status == "processing")
                            & (IntegrationEvent.lease_expires_at <= now),
                        ),
                        or_(
                            IntegrationEvent.next_attempt_at.is_(None),
                            IntegrationEvent.next_attempt_at <= now,
                        ),
                    )
                    .order_by(IntegrationEvent.created_at, IntegrationEvent.id)
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            )
            for event in events:
                if event.aggregate_id is not None:
                    earlier = session.scalar(
                        select(IntegrationEvent.id)
                        .where(
                            IntegrationEvent.integration == "app",
                            IntegrationEvent.event_type == event.event_type,
                            IntegrationEvent.aggregate_id == event.aggregate_id,
                            IntegrationEvent.id != event.id,
                            IntegrationEvent.created_at < event.created_at,
                            IntegrationEvent.status.in_(("pending", "processing", "retry_wait")),
                        )
                        .limit(1)
                    )
                    if earlier:
                        continue
                event.status = "processing"
                event.attempt_count += 1
                event.lease_owner = self.worker_id
                event.lease_expires_at = now + timedelta(seconds=90)
                logger.info(
                    "conversation_event_claimed event_id=%s attempt=%s worker_id=%s",
                    event.id,
                    event.attempt_count,
                    self.worker_id,
                )
                return event.id
        return None

    def process_one(self) -> bool:
        event_id = self.claim_one()
        if event_id is None:
            return False
        loaded_context = self._load_context(event_id)
        if loaded_context is None:
            logger.warning("conversation_context_unavailable event_id=%s", event_id)
            return True
        event, message, contact, conversation, transcript, known_context, known_fact_ids = (
            loaded_context
        )
        settings_mode = self.settings.ai_mode
        with self.session_factory() as session:
            control = session.get(RuntimeControl, 1)
            mode = control.ai_mode if control else settings_mode
            kill_switch = control.kill_switch if control else self.settings.ai_kill_switch
        logger.info(
            "conversation_processing_started event_id=%s message_id=%s conversation_id=%s "
            "mode=%s kill_switch=%s provider=%s",
            event_id,
            message.id,
            conversation.id,
            mode,
            kill_switch,
            "bedrock" if self.settings.bedrock_enabled else "fake",
        )
        if mode == "off":
            logger.info("conversation_processing_skipped event_id=%s reason=rollout_off", event_id)
            self._finish_without_model(event_id, "cancelled", "rollout_off")
            return True
        try:
            skills = load_skills(self.skill_directory)
            inbound_text = (message.body_text or "").strip()
            skill = (
                skills["unknown"]
                if _is_unverified_claim_request(inbound_text)
                else select_skill(inbound_text, skills)
            )
            active_requirement_context = next(
                (
                    item
                    for item in known_context
                    if item.get("kind") == "active_property_requirement"
                ),
                None,
            )
            active_transaction = next(
                (
                    item.get("transaction_type")
                    for item in known_context
                    if item.get("kind") == "active_property_requirement"
                ),
                None,
            )
            previous_assistant_message = next(
                (
                    turn.get("text", "")
                    for turn in reversed(transcript[:-1])
                    if turn.get("role") == "assistant"
                ),
                "",
            )
            contextual_reply = _is_contextual_reply(
                inbound_text, previous_assistant_message
            )
            if skill.header.key != "unknown":
                if active_transaction == "buy":
                    skill = skills["buyer"]
                elif active_transaction == "sell":
                    skill = skills["seller"]
            elif contextual_reply:
                # Direct answers continue the brief, but unknown questions
                # stay on the handover path even during an active buying/selling chat.
                if active_transaction == "buy":
                    skill = skills["buyer"]
                elif active_transaction == "sell":
                    skill = skills["seller"]
            if skill.header.key == "unknown" and contextual_reply:
                for turn in reversed(transcript[:-1]):
                    if turn.get("role") != "user":
                        continue
                    prior_skill = select_skill(turn.get("text", ""), skills)
                    if prior_skill.header.key != "unknown":
                        skill = prior_skill
                        break
            started_at = datetime.now(UTC)
            is_acknowledgement = inbound_text.casefold().strip("!.,? ") in {
                "yes",
                "yeah",
                "yep",
                "yup",
                "ok",
                "okay",
                "thanks",
                "thank you",
                "got it",
            }
            last_assistant_text = next(
                (
                    turn.get("text", "").casefold()
                    for turn in reversed(transcript[:-1])
                    if turn.get("role") == "assistant"
                ),
                "",
            )
            assistant_already_closed = any(
                phrase in last_assistant_text
                for phrase in (
                    "key details for your search",
                    "our team will connect with you shortly",
                    "thanks, i have the key details",
                )
            )
            if (
                (
                    active_requirement_context
                    and active_requirement_context.get("intake_complete")
                    or assistant_already_closed
                )
                and is_acknowledgement
            ):
                from app.integrations.bedrock.provider import AssistantDecision

                decision = AssistantDecision(
                    decision="answer",
                    response_text=(
                        "Thanks! Our team will connect with you shortly to discuss the next steps."
                    ),
                    intent=active_transaction
                    or {"buyer": "buy", "seller": "sell", "portfolio": "portfolio"}.get(
                        skill.header.intent, "general"
                    ),
                    confidence=0.95,
                )
            else:
                decision = self.llm.decide(
                    skill=skill,
                    inbound=message.body_text or "[message has no text; human review required]",
                    transcript=transcript,
                    context=known_context,
                )
            from app.integrations.bedrock.provider import AssistantDecision

            if skill.header.key == "unknown" and decision.decision != "handover":
                decision = decision.model_copy(update={
                    "decision": "handover",
                    "response_text": "",
                    "handover_reason": "unknown_question",
                    "intent": "unknown",
                    "confidence": min(decision.confidence, 0.2),
                    "proposed_facts": [],
                })
            expected_intent = {"buyer": "buy", "seller": "sell"}.get(skill.header.intent)
            if (
                isinstance(decision, AssistantDecision)
                and expected_intent is not None
                and decision.intent not in {expected_intent, "mixed"}
            ):
                decision = decision.model_copy(update={"intent": expected_intent})
            elapsed_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            logger.info(
                "conversation_model_completed event_id=%s skill=%s decision=%s "
                "confidence=%.3f latency_ms=%s",
                event_id,
                skill.header.key,
                decision.decision,
                decision.confidence,
                elapsed_ms,
            )
        except Exception:
            logger.exception(
                "conversation_processing_failed event_id=%s message_id=%s",
                event_id,
                message.id,
            )
            self._retry_or_dead_letter(event_id, "conversation_provider_error")
            return True
        self._persist_decision(
            event_id=event_id,
            message=message,
            contact=contact,
            conversation=conversation,
            transcript=transcript,
            known_context=known_context,
            known_fact_ids=known_fact_ids,
            skill_key=skill.header.key,
            decision=decision,
            mode=mode,
            kill_switch=kill_switch,
            elapsed_ms=elapsed_ms,
            interactive_payload=(
                {
                    "type": "list",
                    "body": (
                        "Hi, I’m Varun. I help with property enquiries. Are you looking to "
                        "buy, rent, invest, sell, or discuss a property you own?"
                    ),
                    "title": "Property enquiry",
                    "buttonLabel": "Choose an option",
                    "sections": [
                        {
                            "title": "I’m looking to…",
                            "rows": [
                                {"id": "buy", "title": "Buy"},
                                {"id": "rent", "title": "Rent"},
                                {"id": "invest", "title": "Invest"},
                                {"id": "sell", "title": "Sell"},
                                {"id": "own_property", "title": "Discuss my property"},
                            ],
                        }
                    ],
                }
                if self.settings.openwa_interactive_enabled
                and skill.header.key == "general"
                # The current inbound greeting is included as the last transcript item.
                # Only show the introductory menu when this is the first message in
                # the conversation; returning users should continue from their history.
                and len(transcript) == 1
                and (message.body_text or "").strip().casefold().strip("!.,? ")
                in {"hi", "hello", "hey", "hii", "hiii"}
                else None
            ),
        )
        return True

    def _load_context(
        self, event_id: uuid.UUID
    ) -> (
        tuple[
            IntegrationEvent,
            Message,
            Contact,
            Conversation,
            list[dict[str, str]],
            list[dict[str, str]],
            list[str],
        ]
        | None
    ):
        with self.session_factory.begin() as session:
            event = session.scalar(
                select(IntegrationEvent)
                .where(
                    IntegrationEvent.id == event_id, IntegrationEvent.lease_owner == self.worker_id
                )
                .with_for_update()
            )
            if event is None:
                logger.error("conversation_event_missing event_id=%s", event_id)
                return None
            try:
                message_id = uuid.UUID(str(event.payload_json["message_id"]))
            except (KeyError, TypeError, ValueError):
                event.status = "dead_letter"
                event.last_error_code = "invalid_message_reference"
                logger.error(
                    "conversation_event_failed event_id=%s reason=invalid_message_reference",
                    event_id,
                )
                return None
            message = session.get(Message, message_id)
            if message is None:
                event.status = "dead_letter"
                event.last_error_code = "inbound_message_missing"
                logger.error(
                    "conversation_event_failed event_id=%s reason=inbound_message_missing",
                    event_id,
                )
                return None
            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.id == message.conversation_id)
                .with_for_update()
            )
            if conversation is None:
                event.status = "dead_letter"
                event.last_error_code = "conversation_missing"
                logger.error(
                    "conversation_event_failed event_id=%s reason=conversation_missing", event_id
                )
                return None
            contact = session.get(Contact, conversation.contact_id)
            if contact is None:
                event.status = "dead_letter"
                event.last_error_code = "contact_missing"
                logger.error(
                    "conversation_event_failed event_id=%s reason=contact_missing", event_id
                )
                return None
            if contact.consent_status == "opted_out" or conversation.state != "active_agent":
                event.status = "cancelled"
                event.last_error_code = (
                    "contact_opted_out"
                    if contact.consent_status == "opted_out"
                    else "conversation_not_agent_owned"
                )
                event.processed_at = datetime.now(UTC)
                event.lease_owner = None
                event.lease_expires_at = None
                logger.info(
                    "conversation_processing_cancelled event_id=%s conversation_id=%s reason=%s",
                    event_id,
                    conversation.id,
                    event.last_error_code,
                )
                return None
            messages = list(
                session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(40)
                )
            )
            messages.reverse()
            transcript = [
                {
                    "role": "assistant" if item.direction == "outbound" else "user",
                    "text": item.body_text or f"[{item.message_type} message]",
                }
                for item in messages
            ]
            facts = list(
                session.scalars(
                    select(ContextFact)
                    .where(
                        ContextFact.contact_id == contact.id, ContextFact.superseded_at.is_(None)
                    )
                    .order_by(ContextFact.created_at.desc())
                    .limit(50)
                )
            )
            known_fact_ids = [str(fact.id) for fact in facts]
            known_context = [
                {
                    "kind": "accepted_context_fact",
                    "entity_type": fact.entity_type,
                    "field_key": fact.field_key,
                    "value_json": json.dumps(fact.value_json, ensure_ascii=False),
                    "confidence": (
                        str(fact.confidence) if fact.confidence is not None else "admin_verified"
                    ),
                }
                for fact in facts
            ]
            active_requirement = session.scalar(
                select(PropertyRequirement)
                .where(
                    PropertyRequirement.contact_id == contact.id,
                    PropertyRequirement.status == "active",
                    exists().where(
                        ContextFact.entity_type == "requirement",
                        ContextFact.entity_id == PropertyRequirement.id,
                        ContextFact.superseded_at.is_(None),
                    ),
                )
                .order_by(PropertyRequirement.updated_at.desc())
                .limit(1)
            )
            if active_requirement is not None:
                requirement_context = {
                    "kind": "active_property_requirement",
                    "transaction_type": active_requirement.transaction_type,
                    "purpose": active_requirement.purpose,
                    "locations": active_requirement.locations,
                    "property_type": active_requirement.property_type,
                    "budget_min": (
                        str(active_requirement.budget_min)
                        if active_requirement.budget_min is not None
                        else None
                    ),
                    "budget_max": (
                        str(active_requirement.budget_max)
                        if active_requirement.budget_max is not None
                        else None
                    ),
                    "currency": active_requirement.currency,
                    "bedrooms_min": active_requirement.bedrooms_min,
                    "bedrooms_max": active_requirement.bedrooms_max,
                    "timing_text": active_requirement.timing_text,
                    "preferences": active_requirement.preferences,
                    "notes": active_requirement.notes,
                    "match_readiness": active_requirement.match_readiness,
                }
                missing_core = _requirement_missing_core_fields(active_requirement)
                if active_requirement.match_readiness == "ready":
                    missing_core = []
                requirement_context["missing_core_fields"] = missing_core
                requirement_context["intake_complete"] = not missing_core
                known_context.append(requirement_context)
            client_context = session.scalar(
                select(ClientContext).where(ClientContext.contact_id == contact.id)
            )
            if client_context is not None:
                if client_context.purpose_notes:
                    known_context.append(
                        {"kind": "client_context", "purpose_notes": client_context.purpose_notes}
                    )
                if client_context.other_shared_context:
                    known_context.append(
                        {
                            "kind": "customer_shared_context",
                            "value_json": json.dumps(
                                client_context.other_shared_context, ensure_ascii=False
                            ),
                        }
                    )
            private_notes = list(
                session.scalars(
                    select(AdminContextNote)
                    .where(
                        AdminContextNote.conversation_id == conversation.id,
                        AdminContextNote.superseded_at.is_(None),
                    )
                    .order_by(AdminContextNote.created_at.desc())
                    .limit(10)
                )
            )
            known_context.extend(
                {"kind": "private_admin_note", "text": note.note_text} for note in private_notes
            )
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id=self.worker_id,
                    action="conversation.model_context_read",
                    entity_type="conversation",
                    entity_id=str(conversation.id),
                    after_json={
                        "message_count": len(messages),
                        "fact_count": len(facts),
                        "private_note_count": len(private_notes),
                    },
                )
            )
            # Keep detached records usable after this read transaction closes.
            session.expunge(event)
            session.expunge(message)
            session.expunge(contact)
            session.expunge(conversation)
            return (
                event,
                message,
                contact,
                conversation,
                transcript,
                known_context,
                known_fact_ids,
            )

    def _persist_decision(
        self,
        *,
        event_id: uuid.UUID,
        message: Message,
        contact: Contact,
        conversation: Conversation,
        transcript: list[dict[str, str]],
        known_context: list[dict[str, str]],
        known_fact_ids: list[str],
        skill_key: str,
        decision: object,
        mode: str,
        kill_switch: bool,
        elapsed_ms: int,
        interactive_payload: dict[str, Any] | None = None,
    ) -> None:
        from app.integrations.bedrock.provider import AssistantDecision

        if not isinstance(decision, AssistantDecision):
            logger.error("conversation_decision_invalid event_id=%s", event_id)
            self._retry_or_dead_letter(event_id, "invalid_decision_type")
            return
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            event = session.scalar(
                select(IntegrationEvent).where(IntegrationEvent.id == event_id).with_for_update()
            )
            locked_conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation.id).with_for_update()
            )
            locked_contact = session.scalar(
                select(Contact).where(Contact.id == contact.id).with_for_update()
            )
            if event is None or locked_conversation is None or locked_contact is None:
                logger.error(
                    "conversation_decision_not_persisted event_id=%s reason=record_missing",
                    event_id,
                )
                return
            if event.status != "processing" or event.lease_owner != self.worker_id:
                logger.warning(
                    "conversation_decision_not_persisted event_id=%s reason=lease_lost status=%s",
                    event_id,
                    event.status,
                )
                return
            if decision.intent in {"buy", "sell", "portfolio", "general", "mixed"}:
                locked_conversation.active_intent = decision.intent
            active_handover = session.scalar(
                select(HandoverCase.id).where(
                    HandoverCase.conversation_id == locked_conversation.id,
                    HandoverCase.status.in_(("open", "assigned", "admin_owned")),
                )
            )
            if (
                locked_conversation.state != "active_agent"
                or locked_contact.consent_status == "opted_out"
                or active_handover is not None
            ):
                event.status = "cancelled"
                event.last_error_code = (
                    "contact_opted_out"
                    if locked_contact.consent_status == "opted_out"
                    else "conversation_human_owned"
                )
                event.processed_at = now
                event.lease_owner = None
                event.lease_expires_at = None
                logger.info(
                    "conversation_decision_cancelled event_id=%s conversation_id=%s reason=%s",
                    event_id,
                    locked_conversation.id,
                    event.last_error_code,
                )
                return
            facts_digest = hashlib.sha256(
                json.dumps(
                    {"transcript": transcript, "known_context": known_context}, sort_keys=True
                ).encode()
            ).hexdigest()
            run = AgentRun(
                conversation_id=locked_conversation.id,
                trigger_message_id=message.id,
                application_release=self.settings.app_release,
                model_provider="bedrock" if self.settings.bedrock_enabled else "fake",
                model_name=self.settings.bedrock_model_id or "deterministic-fake",
                rollout_mode=mode,
                known_fact_ids_json=known_fact_ids,
                requested_field_keys_json=[],
                decision_context_hash=facts_digest,
                latency_ms=elapsed_ms,
            )
            session.add(run)
            session.flush()
            message.sentiment = decision.sentiment
            message.sentiment_confidence = Decimal(str(decision.sentiment_confidence))
            fact_conflict = self._apply_proposed_facts(
                session,
                locked_contact,
                locked_conversation,
                message,
                decision,
                now,
            )
            active_requirement = session.scalar(
                select(PropertyRequirement)
                .where(
                    PropertyRequirement.contact_id == locked_contact.id,
                    PropertyRequirement.status == "active",
                    exists().where(
                        ContextFact.entity_type == "requirement",
                        ContextFact.entity_id == PropertyRequirement.id,
                        ContextFact.superseded_at.is_(None),
                    ),
                )
                .order_by(PropertyRequirement.updated_at.desc())
                .limit(1)
                .with_for_update()
            )
            if (
                active_requirement is not None
                and not _requirement_missing_core_fields(active_requirement)
            ):
                active_requirement.match_readiness = "ready"
            if (
                decision.decision == "handover"
                or decision.confidence < 0.65
                or locked_contact.consent_status == "opted_out"
                or fact_conflict
            ):
                case = session.scalar(
                    select(HandoverCase)
                    .where(
                        HandoverCase.conversation_id == locked_conversation.id,
                        HandoverCase.status.in_(("open", "assigned", "admin_owned")),
                    )
                    .with_for_update()
                )
                if case is None:
                    if locked_contact.consent_status == "opted_out":
                        handover_reason_code, handover_reason_summary = "opt_out", None
                    elif fact_conflict:
                        handover_reason_code, handover_reason_summary = "context_conflict", None
                    else:
                        handover_reason_code, handover_reason_summary = _handover_reason_fields(
                            decision.handover_reason or "low_confidence"
                        )
                    case = HandoverCase(
                        conversation_id=locked_conversation.id,
                        reason_code=handover_reason_code,
                        reason_summary=handover_reason_summary,
                        trigger_message_id=message.id,
                        status="open",
                    )
                    session.add(case)
                locked_conversation.state = "admin_owned"
                locked_conversation.version += 1
                run.status = "handed_over"
                run.decision_type = "handover"
                run.handover_reason = case.reason_code
                ack_allowed = (
                    locked_contact.consent_status != "opted_out"
                    and not kill_switch
                    and mode in {"active", "canary"}
                )
                if ack_allowed and mode == "canary":
                    allowlisted = session.scalar(
                        select(AiRolloutAllowlist.id).where(
                            AiRolloutAllowlist.contact_id == locked_contact.id,
                            or_(
                                AiRolloutAllowlist.expires_at.is_(None),
                                AiRolloutAllowlist.expires_at > now,
                            ),
                        )
                    )
                    ack_allowed = allowlisted is not None
                if ack_allowed:
                    acknowledgement, _ = enqueue_outbound_message(
                        session,
                        conversation_id=locked_conversation.id,
                        sender_type="automated_agent",
                        agent_run_id=run.id,
                        body_text="I’ll ask a team member to check this and get back to you.",
                        idempotency_key=f"agent-handover-ack:{run.id}",
                    )
                    acknowledgement.message_type = "handover_ack"
                    run.response_message_id = acknowledgement.id
                    logger.info(
                        "conversation_handover_ack_queued event_id=%s run_id=%s message_id=%s",
                        event_id,
                        run.id,
                        acknowledgement.id,
                    )
                logger.info(
                    "conversation_handover_created event_id=%s run_id=%s conversation_id=%s "
                    "reason=%s confidence=%.3f",
                    event_id,
                    run.id,
                    locked_conversation.id,
                    case.reason_code,
                    decision.confidence,
                )
            else:
                eligible = mode in {"active", "canary"}
                if mode == "canary":
                    allow = session.scalar(
                        select(AiRolloutAllowlist.id).where(
                            AiRolloutAllowlist.contact_id == locked_contact.id,
                            or_(
                                AiRolloutAllowlist.expires_at.is_(None),
                                AiRolloutAllowlist.expires_at > now,
                            ),
                        )
                    )
                    eligible = allow is not None
                if eligible and locked_conversation.state == "active_agent":
                    response, _ = enqueue_outbound_message(
                        session,
                        conversation_id=locked_conversation.id,
                        sender_type="automated_agent",
                        agent_run_id=run.id,
                        body_text=(
                            "Hi, I’m Varun. I help with property enquiries. Are you looking to "
                            "buy, rent, invest, sell, or discuss a property you own?"
                            if interactive_payload
                            else decision.response_text
                        ),
                        idempotency_key=f"agent:{run.id}",
                        skill_key=skill_key,
                        application_release=self.settings.app_release,
                        interactive_payload=interactive_payload,
                    )
                    run.response_message_id = response.id
                    run.status = "completed"
                    run.decision_type = "answer"
                    logger.info(
                        "conversation_reply_enqueued event_id=%s run_id=%s message_id=%s "
                        "conversation_id=%s mode=%s",
                        event_id,
                        run.id,
                        response.id,
                        locked_conversation.id,
                        mode,
                    )
                    if interactive_payload is not None:
                        logger.info(
                            "conversation_property_menu_queued event_id=%s message_id=%s",
                            event_id,
                            response.id,
                        )
                else:
                    session.add(
                        AgentRunDraft(
                            agent_run_id=run.id,
                            draft_text=decision.response_text
                            or "This message needs a human review.",
                            status="pending_admin",
                            expires_at=now + timedelta(hours=24),
                        )
                    )
                    run.status = "completed"
                    run.decision_type = "draft"
                    draft_reason = (
                        "shadow_mode"
                        if mode not in {"active", "canary"}
                        else "canary_not_allowlisted"
                    )
                    logger.info(
                        "conversation_reply_saved_as_draft event_id=%s run_id=%s "
                        "conversation_id=%s mode=%s reason=%s",
                        event_id,
                        run.id,
                        locked_conversation.id,
                        mode,
                        draft_reason,
                    )
            run.skill_key = skill_key
            run.completed_at = now
            event.status = "succeeded"
            event.processed_at = now
            event.lease_owner = None
            event.lease_expires_at = None

    @staticmethod
    def _apply_proposed_facts(
        session: Session,
        contact: Contact,
        conversation: Conversation,
        message: Message,
        decision: object,
        now: datetime,
    ) -> bool:
        from app.integrations.bedrock.provider import AssistantDecision, ProposedFact

        if not isinstance(decision, AssistantDecision):
            return False
        explicit_budget = _extract_explicit_budget(message.body_text or "")
        proposed_facts = list(decision.proposed_facts)
        if explicit_budget:
            # Prefer the direct amount parsed from the customer's own wording
            # over a less precise model extraction for this same message.
            proposed_facts = [
                fact
                for fact in proposed_facts
                if not (
                    fact.entity_type == "requirement"
                    and fact.field_key in explicit_budget
                )
            ]
            proposed_facts.extend(
                ProposedFact(
                    entity_type="requirement",
                    field_key=field_key,
                    value=value,
                    original_text=message.body_text or "",
                    confidence=0.99,
                )
                for field_key, value in explicit_budget.items()
            )
        active_transaction = session.scalar(
            select(PropertyRequirement.transaction_type)
            .where(
                PropertyRequirement.contact_id == contact.id,
                PropertyRequirement.status == "active",
                exists().where(
                    ContextFact.entity_type == "requirement",
                    ContextFact.entity_id == PropertyRequirement.id,
                    ContextFact.superseded_at.is_(None),
                ),
            )
            .order_by(PropertyRequirement.updated_at.desc())
            .limit(1)
        )
        requirement: PropertyRequirement | None = None
        asset: PortfolioAsset | None = None
        intent_map = {"buy": "buy", "sell": "sell"}
        allowed_fields = {
            "requirement": {
                "property_type",
                "locations",
                "budget_min",
                "budget_max",
                "currency",
                "bedrooms_min",
                "bedrooms_max",
                "size_min",
                "size_max",
                "size_unit",
                "timing_text",
                "purpose",
                "preferences",
                "notes",
            },
            "portfolio_asset": {
                "property_type",
                "location",
                "ownership_status",
                "bedrooms",
                "size",
                "size_unit",
                "value",
                "value_currency",
                "portfolio_notes",
            },
        }
        conflict = False
        for proposed in proposed_facts:
            if (
                proposed.confidence < 0.75
                or proposed.field_key not in allowed_fields[proposed.entity_type]
            ):
                continue
            projection: Any
            if proposed.entity_type == "requirement":
                transaction = intent_map.get(decision.intent) or active_transaction
                if transaction is None:
                    continue
                if requirement is None:
                    requirement = session.scalar(
                        select(PropertyRequirement)
                        .where(
                            PropertyRequirement.contact_id == contact.id,
                            PropertyRequirement.transaction_type == transaction,
                            PropertyRequirement.status == "active",
                            exists().where(
                                ContextFact.entity_type == "requirement",
                                ContextFact.entity_id == PropertyRequirement.id,
                                ContextFact.superseded_at.is_(None),
                            ),
                        )
                        .order_by(PropertyRequirement.updated_at.desc())
                        .limit(1)
                        .with_for_update()
                    )
                    if requirement is None:
                        requirement = PropertyRequirement(
                            contact_id=contact.id,
                            conversation_id=conversation.id,
                            transaction_type=transaction,
                            locations=[],
                            preferences={},
                            status="active",
                            match_readiness="incomplete",
                        )
                        session.add(requirement)
                        session.flush()
                        session.add(
                            ContextFact(
                                contact_id=contact.id,
                                entity_type="requirement",
                                entity_id=requirement.id,
                                field_key="transaction_type",
                                value_json={
                                    "value": transaction,
                                    "original_text": message.body_text or "",
                                },
                                confidence=Decimal(str(decision.confidence)),
                                source_type="agent_extraction",
                                source_message_id=message.id,
                                valid_from=now,
                            )
                        )
                entity_type, entity_id, projection = "requirement", requirement.id, requirement
            else:
                if asset is None:
                    asset = PortfolioAsset(contact_id=contact.id, status="unknown")
                    session.add(asset)
                    session.flush()
                entity_type, entity_id, projection = "portfolio_asset", asset.id, asset
            field_key = proposed.field_key
            value = proposed.value
            if field_key in {"budget_min", "budget_max", "size_min", "size_max", "size", "value"}:
                try:
                    value = Decimal(str(value))
                except (InvalidOperation, ValueError):
                    continue
                if value < 0:
                    continue
            if field_key in {"bedrooms_min", "bedrooms_max", "bedrooms"}:
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                    continue
            if field_key in {"currency", "value_currency"}:
                if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
                    continue
                value = value.upper()
            if field_key in {
                "property_type",
                "size_unit",
                "timing_text",
                "purpose",
                "notes",
                "ownership_status",
                "portfolio_notes",
            } and not isinstance(value, str):
                continue
            if field_key == "locations" and isinstance(value, str):
                value = [{"label": value, "source_text": proposed.original_text}]
            if field_key == "locations" and not isinstance(value, list):
                continue
            if field_key == "location" and isinstance(value, str):
                value = {"label": value, "source_text": proposed.original_text}
            if field_key == "location" and not isinstance(value, dict):
                continue
            if field_key == "preferences" and not isinstance(value, dict):
                continue
            if field_key == "preferences":
                value = {**(projection.preferences or {}), **value}
            if field_key in {"budget_min", "budget_max", "size_min", "size_max"}:
                other_field = {
                    "budget_min": "budget_max",
                    "budget_max": "budget_min",
                    "size_min": "size_max",
                    "size_max": "size_min",
                }[field_key]
                other = getattr(projection, other_field)
                invalid_range = other is not None and (
                    (field_key.endswith("_min") and value > other)
                    or (field_key.endswith("_max") and value < other)
                )
                if invalid_range:
                    conflict = True
                    continue
            if field_key in {"bedrooms_min", "bedrooms_max"}:
                other_field = "bedrooms_max" if field_key == "bedrooms_min" else "bedrooms_min"
                other = getattr(projection, other_field)
                if other is not None and (
                    (field_key.endswith("_min") and value > other)
                    or (field_key.endswith("_max") and value < other)
                ):
                    conflict = True
                    continue
            current = session.scalar(
                select(ContextFact)
                .where(
                    ContextFact.entity_type == entity_type,
                    ContextFact.entity_id == entity_id,
                    ContextFact.field_key == field_key,
                    ContextFact.superseded_at.is_(None),
                )
                .with_for_update()
            )
            fact_value = {
                "value": str(value) if isinstance(value, Decimal) else value,
                "original_text": proposed.original_text,
            }
            if current is not None:
                current_value = current.value_json
                stored_value = (
                    current_value.get("value") if isinstance(current_value, dict) else current_value
                )
                if current.source_type == "admin":
                    values_match = (
                        str(stored_value) == str(value)
                        if isinstance(value, Decimal)
                        else stored_value == value
                    )
                    conflict = conflict or not values_match
                    continue
                values_match = (
                    str(stored_value) == str(value)
                    if isinstance(value, Decimal)
                    else stored_value == value
                )
                if values_match:
                    continue
                current.superseded_at = now
            if hasattr(projection, field_key):
                setattr(projection, field_key, value)
            session.add(
                ContextFact(
                    contact_id=contact.id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    field_key=field_key,
                    value_json=fact_value,
                    confidence=Decimal(str(proposed.confidence)),
                    source_type="agent_extraction",
                    source_message_id=message.id,
                    valid_from=now,
                )
            )
        return conflict

    def _finish_without_model(self, event_id: uuid.UUID, status: str, error: str) -> None:
        with self.session_factory.begin() as session:
            event = session.get(IntegrationEvent, event_id)
            if event:
                event.status = status
                event.last_error_code = error
                event.processed_at = datetime.now(UTC)
                event.lease_owner = None
                event.lease_expires_at = None

    def _retry_or_dead_letter(self, event_id: uuid.UUID, error: str) -> None:
        with self.session_factory.begin() as session:
            event = session.get(IntegrationEvent, event_id)
            if event:
                event.status = "dead_letter" if event.attempt_count >= 5 else "retry_wait"
                event.last_error_code = error
                event.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 2**event.attempt_count)
                )
                event.lease_owner = None
                event.lease_expires_at = None
                logger.error(
                    "conversation_event_retry_scheduled event_id=%s reason=%s status=%s attempts=%s",
                    event_id,
                    error,
                    event.status,
                    event.attempt_count,
                )


def run_worker() -> None:
    from app.db.connection import make_engine, make_session_factory
    from app.integrations.bedrock.provider import BedrockLlm, FakeLlm

    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(message)s")
    engine = make_engine(settings)
    if settings.bedrock_enabled:
        provider: LlmPort = BedrockLlm(settings)
    elif settings.app_env in {"development", "test"}:
        provider = FakeLlm()
    else:
        raise RuntimeError("fake LLM is restricted to local development")
    worker = ConversationWorker(make_session_factory(engine), provider, settings)
    logger.info(
        "conversation_worker_started app_env=%s provider=%s model=%s settings_mode=%s "
        "interactive_enabled=%s",
        settings.app_env,
        "bedrock" if settings.bedrock_enabled else "fake",
        settings.bedrock_model_id or "deterministic-fake",
        settings.ai_mode,
        settings.openwa_interactive_enabled,
    )
    while True:
        if not worker.process_one():
            import time

            time.sleep(1)


if __name__ == "__main__":
    run_worker()
