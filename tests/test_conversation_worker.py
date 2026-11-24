from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.db.models import (
    AgentRunDraft,
    Contact,
    ContextFact,
    Conversation,
    IntegrationEvent,
    Message,
    PropertyRequirement,
)
from app.integrations.bedrock.provider import AssistantDecision, FakeLlm, ProposedFact
from app.skills.loader import Skill
from app.workers.conversation import ConversationWorker


class ExtractingLlm(FakeLlm):
    def decide(
        self,
        *,
        skill: Skill,
        inbound: str,
        transcript: list[dict[str, str]],
        context: list[dict[str, str]] | None = None,
    ) -> AssistantDecision:
        del skill, inbound, transcript, context
        return AssistantDecision(
            decision="answer",
            response_text="What timing works for you?",
            intent="buy",
            confidence=0.96,
            proposed_facts=[
                ProposedFact(
                    entity_type="requirement",
                    field_key="budget_max",
                    value=2000000,
                    original_text="around AED 2m",
                    confidence=0.97,
                ),
                ProposedFact(
                    entity_type="requirement",
                    field_key="currency",
                    value="AED",
                    original_text="AED 2m",
                    confidence=0.98,
                ),
                ProposedFact(
                    entity_type="requirement",
                    field_key="locations",
                    value="Dubai Marina",
                    original_text="in Dubai Marina",
                    confidence=0.97,
                ),
            ],
        )


def test_shadow_processing_creates_private_draft_without_customer_message(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        contact = Contact(
            provider_account_id="test",
            phone_e164="+971500000000",
            consent_status="opted_in",
            retention_state="active",
        )
        session.add(contact)
        session.flush()
        conversation = Conversation(
            contact_id=contact.id,
            channel="whatsapp",
            provider_account_id="test",
            state="active_agent",
        )
        session.add(conversation)
        session.flush()
        message = Message(
            conversation_id=conversation.id,
            provider_account_id="test",
            provider_message_id="in-1",
            direction="inbound",
            sender_type="customer",
            message_type="text",
            body_text="I want to buy an apartment",
            delivery_status=None,
        )
        session.add(message)
        session.flush()
        session.add(
            IntegrationEvent(
                integration="app",
                direction="outbound",
                event_type="conversation.process_message",
                external_event_id="process:in-1",
                aggregate_type="conversation",
                aggregate_id=conversation.id,
                payload_json={"message_id": str(message.id)},
                status="pending",
            )
        )
    skills = Path(__file__).parents[1] / "skills"
    worker = ConversationWorker(
        factory,
        FakeLlm(),
        Settings(app_env="test", database_url="sqlite://"),
        skill_directory=skills,
    )
    assert worker.process_one() is True
    with factory() as session:
        assert session.scalar(select(AgentRunDraft)) is not None
        assert session.scalar(select(Message).where(Message.direction == "outbound")) is None
        event = session.scalar(
            select(IntegrationEvent).where(IntegrationEvent.integration == "app")
        )
        assert event is not None and event.status == "succeeded"
    engine.dispose()


def test_high_confidence_facts_update_projection_with_provenance(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        contact = Contact(
            provider_account_id="test",
            phone_e164="+971500000001",
            consent_status="opted_in",
            retention_state="active",
        )
        session.add(contact)
        session.flush()
        conversation = Conversation(
            contact_id=contact.id,
            channel="whatsapp",
            provider_account_id="test",
            state="active_agent",
        )
        session.add(conversation)
        session.flush()
        message = Message(
            conversation_id=conversation.id,
            provider_account_id="test",
            provider_message_id="in-2",
            direction="inbound",
            sender_type="customer",
            message_type="text",
            body_text="Buying an apartment around AED 2m in Dubai Marina",
        )
        session.add(message)
        session.flush()
        session.add(
            IntegrationEvent(
                integration="app",
                direction="outbound",
                event_type="conversation.process_message",
                external_event_id="process:in-2",
                aggregate_type="conversation",
                aggregate_id=conversation.id,
                payload_json={"message_id": str(message.id)},
                status="pending",
            )
        )
    worker = ConversationWorker(
        factory,
        ExtractingLlm(),
        Settings(app_env="test", database_url="sqlite://"),
        skill_directory=Path(__file__).parents[1] / "skills",
    )
    assert worker.process_one() is True
    with factory() as session:
        requirement = session.scalar(select(PropertyRequirement))
        assert requirement is not None
        assert requirement.transaction_type == "buy"
        assert str(requirement.budget_max) == "2000000.00"
        assert requirement.currency == "AED"
        assert requirement.locations[0]["label"] == "Dubai Marina"
        facts = list(
            session.scalars(select(ContextFact).where(ContextFact.entity_type == "requirement"))
        )
        assert {fact.field_key for fact in facts} == {
            "transaction_type",
            "budget_max",
            "currency",
            "locations",
        }
        assert all(
            fact.source_type == "agent_extraction" and fact.source_message_id == message.id
            for fact in facts
        )
    engine.dispose()
