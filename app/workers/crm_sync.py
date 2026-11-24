from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import (
    ClientContext,
    Contact,
    CrmLink,
    IntegrationEvent,
    PortfolioAsset,
    PropertyRequirement,
)
from app.integrations.zoho.client import CrmError, CrmPort, map_record, parse_mappings

logger = logging.getLogger("whatsapp_assistant.crm_worker")


class CrmSyncWorker:
    def __init__(
        self, session_factory: sessionmaker[Session], crm: CrmPort, settings: Settings
    ) -> None:
        self.session_factory = session_factory
        self.crm = crm
        self.settings = settings
        self.worker_id = str(uuid.uuid4())

    def process_one(self) -> bool:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            event = session.scalar(
                select(IntegrationEvent)
                .where(
                    IntegrationEvent.integration == "zoho",
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
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return False
            event.status = "processing"
            event.lease_owner = self.worker_id
            event.lease_expires_at = now + timedelta(seconds=60)
            event.attempt_count += 1
            event_id = event.id
            try:
                contact_id = uuid.UUID(str(event.payload_json["contact_id"]))
            except (KeyError, TypeError, ValueError):
                event.status = "dead_letter"
                event.last_error_code = "invalid_contact_reference"
                return True
        try:
            self._sync_contact(contact_id)
        except CrmError as exc:
            self._record_failure(event_id, exc.code, exc.retryable)
            return True
        except Exception:
            logger.exception("zoho_sync_failed event_id=%s", event_id)
            self._record_failure(event_id, "zoho_worker_error", True)
            return True
        with self.session_factory.begin() as session:
            event = session.get(IntegrationEvent, event_id)
            if event:
                event.status = "succeeded"
                event.processed_at = datetime.now(UTC)
                event.lease_owner = None
                event.lease_expires_at = None
                event.last_error_code = None
        return True

    def _sync_contact(self, contact_id: uuid.UUID) -> None:
        modules, field_mappings = parse_mappings(self.settings)
        if not self.settings.zoho_enabled and not modules and not field_mappings:
            modules = {
                "contact": "FakeContacts",
                "requirement": "FakeRequirements",
                "portfolio_asset": "FakePortfolioAssets",
            }
            field_mappings = {
                kind: {
                    "unique_field": "Assistant_Source_Id",
                    "fields": {"id": "Assistant_Source_Id", "contact_id": "Assistant_Contact_Id"},
                }
                for kind in modules
            }
        with self.session_factory() as session:
            contact = session.get(Contact, contact_id)
            if contact is None:
                raise CrmError("contact_missing")
            contact_data: dict[str, Any] = {
                "id": str(contact.id),
                "phone_e164": contact.phone_e164,
                "display_name": contact.display_name,
                "preferred_language": contact.preferred_language,
                "consent_status": contact.consent_status,
                "retention_state": contact.retention_state,
            }
            client_context = session.scalar(
                select(ClientContext).where(ClientContext.contact_id == contact_id)
            )
            if client_context is not None:
                contact_data["purpose_notes"] = client_context.purpose_notes
                contact_data["other_shared_context"] = client_context.other_shared_context
            requirements = list(
                session.scalars(
                    select(PropertyRequirement).where(PropertyRequirement.contact_id == contact_id)
                )
            )
            assets = list(
                session.scalars(
                    select(PortfolioAsset).where(PortfolioAsset.contact_id == contact_id)
                )
            )
            requirement_data: list[dict[str, Any]] = [
                {
                    "id": str(row.id),
                    "contact_id": str(row.contact_id),
                    "transaction_type": row.transaction_type,
                    "property_type": row.property_type,
                    "locations": row.locations,
                    "budget_min": str(row.budget_min) if row.budget_min is not None else None,
                    "budget_max": str(row.budget_max) if row.budget_max is not None else None,
                    "currency": row.currency,
                    "bedrooms_min": row.bedrooms_min,
                    "bedrooms_max": row.bedrooms_max,
                    "size_min": str(row.size_min) if row.size_min is not None else None,
                    "size_max": str(row.size_max) if row.size_max is not None else None,
                    "size_unit": row.size_unit,
                    "timing_text": row.timing_text,
                    "purpose": row.purpose,
                    "preferences": row.preferences,
                    "notes": row.notes,
                    "status": row.status,
                    "match_readiness": row.match_readiness,
                }
                for row in requirements
            ]
            asset_data: list[dict[str, Any]] = [
                {
                    "id": str(row.id),
                    "contact_id": str(contact_id),
                    "property_type": row.property_type,
                    "location": row.location,
                    "ownership_status": row.ownership_status,
                    "bedrooms": row.bedrooms,
                    "size": str(row.size) if row.size is not None else None,
                    "size_unit": row.size_unit,
                    "value": str(row.value) if row.value is not None else None,
                    "value_currency": row.value_currency,
                    "status": row.status,
                }
                for row in assets
            ]
        remote_contact_id = self._upsert_link(
            contact_id, None, None, "contact", contact_data, modules, field_mappings
        )
        for record in requirement_data:
            record["crm_contact_id"] = remote_contact_id
            self._upsert_link(
                contact_id,
                uuid.UUID(record["id"]),
                None,
                "requirement",
                record,
                modules,
                field_mappings,
            )
        for record in asset_data:
            record["crm_contact_id"] = remote_contact_id
            self._upsert_link(
                contact_id,
                None,
                uuid.UUID(record["id"]),
                "portfolio_asset",
                record,
                modules,
                field_mappings,
            )

    def _upsert_link(
        self,
        contact_id: uuid.UUID,
        requirement_id: uuid.UUID | None,
        asset_id: uuid.UUID | None,
        kind: str,
        source: dict[str, Any],
        modules: dict[str, Any],
        fields: dict[str, dict[str, str]],
    ) -> str:
        link_field = (
            "requirement_id"
            if requirement_id
            else "portfolio_asset_id"
            if asset_id
            else "contact_id"
        )
        link_id = requirement_id or asset_id or contact_id
        with self.session_factory.begin() as session:
            link = session.scalar(
                select(CrmLink)
                .where(
                    getattr(CrmLink, link_field) == link_id,
                    CrmLink.crm_system == "zoho",
                )
                .with_for_update()
            )
            if link is None:
                link = CrmLink(
                    contact_id=contact_id,
                    requirement_id=requirement_id,
                    portfolio_asset_id=asset_id,
                    crm_system="zoho",
                    sync_status="pending",
                )
                session.add(link)
                session.flush()
            link_id_db = link.id
        module = modules.get(kind)
        config = fields.get(kind)
        if isinstance(config, dict):
            mapping = {**config, "module": module}
        else:
            mapping = None
        if not isinstance(mapping, dict):
            raise CrmError(f"zoho_mapping_missing_{kind}")
        module_name, unique_field, payload = map_record(kind, source, {kind: mapping})
        try:
            remote_id = self.crm.upsert(
                module=module_name, unique_field=unique_field, record=payload
            )
        except CrmError as exc:
            with self.session_factory.begin() as session:
                link = session.get(CrmLink, link_id_db)
                if link:
                    link.sync_status = "failed"
                    link.last_error_code = exc.code
            raise
        with self.session_factory.begin() as session:
            link = session.get(CrmLink, link_id_db)
            if link:
                if kind == "contact":
                    link.external_contact_id = remote_id
                else:
                    link.external_record_id = remote_id
                link.sync_status = "synced"
                link.last_synced_at = datetime.now(UTC)
                link.last_error_code = None
        return remote_id

    def _record_failure(self, event_id: uuid.UUID, code: str, retryable: bool) -> None:
        with self.session_factory.begin() as session:
            event = session.get(IntegrationEvent, event_id)
            if event is None:
                return
            if retryable and event.attempt_count < 6:
                event.status = "retry_wait"
                event.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(900, 2**event.attempt_count * 5)
                )
            else:
                event.status = "dead_letter"
            event.last_error_code = code
            event.lease_owner = None
            event.lease_expires_at = None


def run_worker() -> None:
    from app.db.connection import make_engine, make_session_factory
    from app.integrations.zoho.client import FakeCrm, ZohoCrm

    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(message)s")
    engine = make_engine(settings)
    if settings.zoho_enabled:
        crm: CrmPort = ZohoCrm(settings)
    elif settings.app_env in {"development", "test"}:
        crm = FakeCrm()
    else:
        raise RuntimeError("fake CRM is restricted to local development")
    worker = CrmSyncWorker(make_session_factory(engine), crm, settings)
    logger.info(
        "crm_worker_started app_env=%s provider=%s enabled=%s",
        settings.app_env,
        "zoho" if settings.zoho_enabled else "fake",
        settings.zoho_enabled,
    )
    while True:
        if not worker.process_one():
            time.sleep(1)


if __name__ == "__main__":
    run_worker()
