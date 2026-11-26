"""Send one inactivity reminder per contact after an unanswered agent reply."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from threading import Event

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.conversations.outbound import enqueue_outbound_message
from app.db.connection import make_engine, make_session_factory
from app.db.models import AgentRun, Contact, Conversation, HandoverCase, Message
from app.config.settings import Settings

logger = logging.getLogger("whatsapp_assistant.reminder_worker")
REMINDER_DELAY = timedelta(minutes=10)
REMINDER_TEXT = "Just checking in. If you'd like to continue, reply here whenever you're ready."


class InactivityReminderWorker:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def process_due(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        sent = 0
        with self.session_factory.begin() as session:
            conversations = list(
                session.scalars(
                    select(Conversation)
                    .where(
                        Conversation.state == "active_agent",
                        Conversation.last_outbound_at.is_not(None),
                        Conversation.last_outbound_at <= now - REMINDER_DELAY,
                    )
                    .order_by(Conversation.last_outbound_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            for conversation in conversations:
                contact = session.scalar(
                    select(Contact)
                    .where(Contact.id == conversation.contact_id)
                    .with_for_update()
                )
                if (
                    contact is None
                    or contact.inactivity_reminder_sent_at is not None
                    or contact.consent_status == "opted_out"
                    or contact.retention_state != "active"
                ):
                    continue
                last_message = session.scalar(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
                if (
                    last_message is None
                    or last_message.direction != "outbound"
                    or last_message.sender_type != "automated_agent"
                ):
                    continue
                active_handover = session.scalar(
                    select(HandoverCase.id).where(
                        HandoverCase.conversation_id == conversation.id,
                        HandoverCase.status.in_(("open", "assigned", "admin_owned")),
                    )
                )
                if active_handover is not None:
                    continue
                # Guard against a recent inbound even if timestamp bookkeeping lags.
                newer_inbound = session.scalar(
                    select(Message.id)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.direction == "inbound",
                        Message.created_at > last_message.created_at,
                    )
                    .limit(1)
                )
                if newer_inbound is not None:
                    continue
                run = AgentRun(
                    conversation_id=conversation.id,
                    trigger_message_id=last_message.id,
                    application_release="inactivity-reminder",
                    model_provider="system",
                    rollout_mode="active",
                    status="completed",
                    decision_type="inactivity_reminder",
                )
                session.add(run)
                session.flush()
                reminder, _ = enqueue_outbound_message(
                    session,
                    conversation_id=conversation.id,
                    sender_type="automated_agent",
                    body_text=REMINDER_TEXT,
                    idempotency_key=f"inactivity-reminder:{contact.id}",
                    agent_run_id=run.id,
                    skill_key="inactivity_reminder",
                    application_release="inactivity-reminder",
                )
                run.response_message_id = reminder.id
                contact.inactivity_reminder_sent_at = now
                sent += 1
        return sent

    def run(self, stop_event: Event) -> None:
        """Poll until the hosting application asks this worker to stop."""
        logger.info("inactivity_reminder_worker_started delay_minutes=10 max_per_contact=1")
        while not stop_event.is_set():
            try:
                if self.process_due() == 0:
                    stop_event.wait(5)
            except Exception:
                logger.exception("inactivity_reminder_poll_error")
                stop_event.wait(5)


def run_worker() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(message)s")
    worker = InactivityReminderWorker(make_session_factory(make_engine(settings)))
    worker.run(Event())


if __name__ == "__main__":
    run_worker()
