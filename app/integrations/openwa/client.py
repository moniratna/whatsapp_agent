import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import Settings


class OpenWaError(RuntimeError):
    def __init__(self, code: str, outcome: str = "rejected", retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.outcome = outcome
        self.retryable = retryable


class OpenWaPort(Protocol):
    async def send_text(self, chat_id: str, body: str) -> str: ...


@dataclass(frozen=True)
class OpenWaHealth:
    http_available: bool
    connected: bool | None
    session_ready: bool | None


class OpenWaClient:
    """HTTP adapter for open-wa Easy API; provider payloads stay within this module."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.openwa_base_url.rstrip("/")
        self.api_key = settings.openwa_api_key.get_secret_value()
        self.timeout = 8.0
        self._client = client or httpx.AsyncClient(timeout=self.timeout)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(self, chat_id: str, body: str) -> str:
        if not self.api_key:
            raise OpenWaError("openwa_api_key_missing")
        try:
            response = await self._client.post(
                f"{self.base_url}/api/messages/sendText",
                headers={"X-API-Key": self.api_key},
                json={"to": chat_id, "content": body},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise OpenWaError("openwa_send_outcome_unknown", outcome="ambiguous") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error_code = "openwa_request_rejected"
            retryable = response.status_code == 429 or response.status_code >= 500
            try:
                result: Any = response.json()
                message = str(result.get("error", "")).lower() if isinstance(result, dict) else ""
                if "unknown" in message or "timeout" in message:
                    raise OpenWaError("openwa_send_outcome_unknown", outcome="ambiguous") from exc
            except ValueError:
                pass
            raise OpenWaError(error_code, retryable=retryable) from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise OpenWaError("openwa_invalid_send_response", outcome="ambiguous") from exc
        if not isinstance(result, dict) or result.get("success") is not True:
            raise OpenWaError("openwa_send_rejected")
        provider_message_id = result.get("data")
        if not isinstance(provider_message_id, str) or not provider_message_id:
            raise OpenWaError("openwa_send_missing_message_id", outcome="ambiguous")
        return provider_message_id

    async def health(self) -> OpenWaHealth:
        try:
            response = await self._client.get(f"{self.base_url}/health", timeout=3.0)
            response.raise_for_status()
            payload: Any = response.json()
            if not isinstance(payload, dict):
                return OpenWaHealth(http_available=True, connected=None, session_ready=None)
            session = payload.get("session")
            session_ready = session.get("ready") if isinstance(session, dict) else None
            return OpenWaHealth(
                http_available=True,
                connected=(
                    payload.get("connected") if isinstance(payload.get("connected"), bool) else None
                ),
                session_ready=session_ready if isinstance(session_ready, bool) else None,
            )
        except (httpx.HTTPError, ValueError):
            return OpenWaHealth(http_available=False, connected=None, session_ready=None)


class FakeOpenWaClient:
    """Local transport fake; returns synthetic provider IDs and never contacts WhatsApp."""

    async def send_text(self, chat_id: str, body: str) -> str:
        del chat_id, body
        return f"local-{uuid.uuid4()}"

    async def close(self) -> None:
        return None
