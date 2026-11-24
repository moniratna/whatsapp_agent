from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.db.models import Conversation, HandoverCase, Message, TranscriptExport

_PHONE = re.compile(r"(?<!\w)\+?[0-9][0-9 ()-]{7,}[0-9](?!\w)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_INTRODUCED_NAME = re.compile(
    r"(?i)(\b(?:my name is|this is|i am|i'm|name is)\s+)"
    r"[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+){0,2}"
)
_TITLED_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)?\b")
_ADDRESS = re.compile(
    r"(?i)\b(?:flat|apartment|apt|villa|building|tower|street|road|unit|plot|house)\s+"
    r"(?:no\.?\s*)?[A-Za-z0-9-]+(?:\s+[A-Za-z0-9-]+){0,4}"
)


def _deidentify(text: str | None) -> str | None:
    if text is None:
        return None
    text = _EMAIL.sub("[EMAIL]", text)
    text = _URL.sub("[URL]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _INTRODUCED_NAME.sub(r"\1[NAME]", text)
    text = _TITLED_NAME.sub("[NAME]", text)
    return _ADDRESS.sub("[ADDRESS]", text)


def _deidentify_json(value: object) -> object:
    if isinstance(value, str):
        return _deidentify(value) or ""
    if isinstance(value, list):
        return [_deidentify_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _deidentify_json(item) for key, item in value.items()}
    return value


def _pseudonym(value: uuid.UUID) -> str:
    return hashlib.sha256(value.bytes).hexdigest()[:16]


class TranscriptExportWorker:
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.worker_id = str(uuid.uuid4())

    def process_one(self) -> bool:
        self._expire_old()
        with self.session_factory.begin() as session:
            export = session.scalar(
                select(TranscriptExport)
                .where(
                    (TranscriptExport.status == "queued")
                    | (
                        (TranscriptExport.status == "running")
                        & (TranscriptExport.lease_expires_at <= datetime.now(UTC))
                    )
                )
                .order_by(TranscriptExport.requested_at, TranscriptExport.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if export is None:
                return False
            export.status = "running"
            export.lease_owner = self.worker_id
            export.lease_expires_at = datetime.now(UTC) + timedelta(hours=1)
            export_id = export.id
            filters = dict(export.filters_json)
            deidentified = export.deidentified
        try:
            artifact, row_count, checksum = self._write(export_id, filters, deidentified)
        except Exception:
            with self.session_factory.begin() as session:
                export = session.get(TranscriptExport, export_id)
                if export:
                    export.status = "failed"
                    export.error_code = "export_generation_failed"
                    export.lease_owner = None
                    export.lease_expires_at = None
            return True
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            export = session.get(TranscriptExport, export_id)
            if export:
                export.status = "ready"
                export.artifact_uri = str(artifact)
                export.row_count = row_count
                export.checksum = checksum
                export.ready_at = now
                export.expires_at = now + timedelta(hours=self.settings.export_retention_hours)
                export.lease_owner = None
                export.lease_expires_at = None
        return True

    def _expire_old(self) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            expired = list(
                session.scalars(
                    select(TranscriptExport)
                    .where(
                        TranscriptExport.status == "ready",
                        TranscriptExport.expires_at <= now,
                    )
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            )
            locations = [item.artifact_uri for item in expired if item.artifact_uri]
            for item in expired:
                item.status = "expired"
                item.artifact_uri = None
        for location in locations:
            if location and location.startswith("s3://"):
                bucket, _, key = location[5:].partition("/")
                if bucket == self.settings.exports_s3_bucket and key:
                    boto3.client("s3").delete_object(Bucket=bucket, Key=key)
            elif location:
                path = Path(location).resolve()
                root = Path(self.settings.exports_directory).resolve()
                if path.parent == root:
                    path.unlink(missing_ok=True)

    def _write(
        self, export_id: uuid.UUID, filters: dict[str, object], deidentified: bool
    ) -> tuple[Path | str, int, str]:
        query = (
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        conversation_ids = filters.get("conversation_ids")
        if isinstance(conversation_ids, list) and conversation_ids:
            try:
                parsed_ids = [uuid.UUID(str(value)) for value in conversation_ids]
            except ValueError as exc:
                raise ValueError("invalid_conversation_filter") from exc
            query = query.where(Message.conversation_id.in_(parsed_ids))
        from_date = filters.get("from_date")
        if isinstance(from_date, str):
            query = query.where(Message.created_at >= datetime.fromisoformat(from_date))
        to_date = filters.get("to_date")
        if isinstance(to_date, str):
            query = query.where(Message.created_at <= datetime.fromisoformat(to_date))
        skill_key = filters.get("skill_key")
        if isinstance(skill_key, str):
            query = query.where(Message.skill_key == skill_key)
        handover_reason = filters.get("handover_reason")
        if isinstance(handover_reason, str):
            query = query.join(HandoverCase, HandoverCase.conversation_id == Conversation.id).where(
                HandoverCase.reason_code == handover_reason
            )
        directory = Path(self.settings.exports_directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        artifact = directory / f"{export_id}.jsonl"
        digest = hashlib.sha256()
        row_count = 0
        with self.session_factory() as session, artifact.open("w", encoding="utf-8") as stream:
            os.chmod(artifact, 0o600)
            for message, conversation in session.execute(query).unique():
                body_text = message.body_text
                contact_id = conversation.contact_id
                conversation_id = conversation.id
                if deidentified:
                    body_text = _deidentify(body_text)
                    interactive_payload = _deidentify_json(message.interactive_payload)
                    contact_key: str = _pseudonym(contact_id)
                    conversation_key: str = _pseudonym(conversation_id)
                else:
                    contact_key = str(contact_id)
                    conversation_key = str(conversation_id)
                    interactive_payload = message.interactive_payload
                row = {
                    "conversation_ref": conversation_key,
                    "contact_ref": contact_key,
                    "direction": message.direction,
                    "sender_type": message.sender_type,
                    "message_type": message.message_type,
                    "body_text": body_text,
                    "interactive_payload": interactive_payload,
                    "delivery_status": message.delivery_status,
                    "skill_key": message.skill_key,
                    "application_release": message.application_release,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                }
                line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                stream.write(line)
                digest.update(line.encode("utf-8"))
                row_count += 1
        if self.settings.exports_s3_bucket:
            key = f"{self.settings.exports_s3_prefix.strip('/')}/{export_id}.jsonl"
            boto3.client("s3").upload_file(
                str(artifact),
                self.settings.exports_s3_bucket,
                key,
                ExtraArgs={"ContentType": "application/x-ndjson", "ServerSideEncryption": "AES256"},
            )
            artifact.unlink(missing_ok=True)
            return (
                f"s3://{self.settings.exports_s3_bucket}/{key}",
                row_count,
                digest.hexdigest(),
            )
        return artifact, row_count, digest.hexdigest()


def run_worker() -> None:
    import time

    from app.db.connection import make_engine, make_session_factory

    settings = Settings()
    worker = TranscriptExportWorker(make_session_factory(make_engine(settings)), settings)
    while True:
        if not worker.process_one():
            time.sleep(1)


if __name__ == "__main__":
    run_worker()
