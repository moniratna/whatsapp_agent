from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OpenWaMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    sender: str | None = Field(default=None, alias="from")
    body: str | None = None
    caption: str | None = None
    from_me: bool = Field(default=False, alias="fromMe")
    is_group: bool = Field(default=False, alias="isGroup")
    sender_phone: str | None = Field(default=None, alias="senderPhone")
    message_type: str | None = Field(default=None, alias="type")
    timestamp: int | float | None = Field(default=None, alias="t")
    mimetype: str | None = None
    filename: str | None = None
    media_key: str | None = Field(default=None, alias="mediaKey")


class OpenWaEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    webhook_id: str | None = Field(default=None, alias="webhookId")
    session_id: str = Field(alias="sessionId", min_length=1, max_length=128)
    event: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] | None = None
    timestamp: int | float | str | None = None
    idempotency_key: str | None = Field(default=None, alias="idempotencyKey")

    @model_validator(mode="after")
    def normalize_data_envelope(self) -> "OpenWaEnvelope":
        # Newer OpenWA webhooks put event content under `data`; older Easy API
        # webhooks use `payload.message`. Keep one internal representation.
        if not self.payload and isinstance(self.data, dict):
            if self.event == "message.received":
                self.payload = (
                    self.data if isinstance(self.data.get("message"), dict)
                    else {"message": self.data}
                )
            else:
                self.payload = self.data
        return self

    @field_validator("session_id")
    @classmethod
    def trim_session_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("sessionId cannot be blank")
        return value


class NormalizedInboundMessage(BaseModel):
    provider_account_id: str
    provider_message_id: str
    sender_phone_e164: str
    body_text: str | None
    message_type: str
    provider_timestamp_ms: int | None
    attachment: dict[str, Any] | None = None
    interactive_payload: dict[str, Any] | None = None
    sender_chat_id: str
