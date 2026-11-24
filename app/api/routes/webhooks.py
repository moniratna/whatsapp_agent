import hashlib
import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings, get_settings
from app.integrations.openwa.schemas import OpenWaEnvelope
from app.integrations.openwa.webhook_service import accept_openwa_event

logger = logging.getLogger("whatsapp_assistant.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/open-wa", status_code=status.HTTP_202_ACCEPTED)
async def receive_openwa_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
    openwa_signature: Annotated[str | None, Header(alias="X-OpenWA-Signature")] = None,
) -> dict[str, str | bool]:
    expected_secret = settings.openwa_webhook_secret.get_secret_value()
    if not settings.openwa_enabled or not expected_secret:
        raise HTTPException(status_code=503, detail="open-wa webhook is not configured")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.webhook_max_bytes:
                raise HTTPException(status_code=413, detail="webhook payload is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
    raw_body = bytearray()
    async for chunk in request.stream():
        raw_body.extend(chunk)
        if len(raw_body) > settings.webhook_max_bytes:
            raise HTTPException(status_code=413, detail="webhook payload is too large")

    shared_secret_valid = bool(webhook_secret) and hmac.compare_digest(
        webhook_secret or "", expected_secret
    )
    expected_signature = "sha256=" + hmac.new(
        expected_secret.encode("utf-8"), bytes(raw_body), hashlib.sha256
    ).hexdigest()
    signature_valid = bool(openwa_signature) and hmac.compare_digest(
        openwa_signature or "", expected_signature
    )
    if not (shared_secret_valid or signature_valid):
        raise HTTPException(status_code=401, detail="invalid webhook credentials")

    try:
        envelope = OpenWaEnvelope.model_validate_json(raw_body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid open-wa event envelope") from exc

    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        try:
            inserted = accept_openwa_event(session, envelope)
        except Exception as exc:
            session.rollback()
            request_id = getattr(request.state, "request_id", "unknown")
            logger.exception(
                "openwa_webhook_persist_failed request_id=%s session_id=%s event=%s",
                request_id,
                envelope.session_id,
                envelope.event,
            )
            raise HTTPException(
                status_code=503, detail="webhook event could not be persisted"
            ) from exc
    logger.info(
        "openwa_webhook_accepted event=%s session_id=%s duplicate=%s",
        envelope.event,
        envelope.session_id,
        not inserted,
    )
    return {"accepted": True, "duplicate": not inserted}
