from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import Settings


class CrmError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class CrmPort(Protocol):
    def upsert(self, *, module: str, unique_field: str, record: dict[str, Any]) -> str: ...


class FakeCrm:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}

    def upsert(self, *, module: str, unique_field: str, record: dict[str, Any]) -> str:
        value = record.get(unique_field)
        if not isinstance(value, str) or not value:
            raise CrmError("crm_unique_field_missing")
        key = (module, unique_field, value)
        existing = self.records.get(key)
        record_id = existing[0] if existing else f"fake-{len(self.records) + 1}"
        self.records[key] = (record_id, dict(record))
        return record_id


@dataclass(frozen=True)
class ZohoFieldMap:
    module: str
    unique_field: str
    field_map: dict[str, str]


class ZohoCrm:
    """Zoho CRM API v8 client; module and field names must be explicitly configured."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        if not settings.zoho_enabled:
            raise RuntimeError("zoho_not_enabled")
        if not all(
            value.get_secret_value()
            for value in (
                settings.zoho_client_id,
                settings.zoho_client_secret,
                settings.zoho_refresh_token,
            )
        ):
            raise RuntimeError("zoho_oauth_credentials_missing")
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0))
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._api_domain = settings.zoho_api_domain.rstrip("/")
        self._expires_at = 0.0

    def _token(self) -> str:
        import time

        with self._lock:
            if self._access_token and self._expires_at > time.monotonic() + 60:
                return self._access_token
            try:
                response = self.client.post(
                    f"{self.settings.zoho_accounts_url.rstrip('/')}/oauth/v2/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self.settings.zoho_client_id.get_secret_value(),
                        "client_secret": self.settings.zoho_client_secret.get_secret_value(),
                        "refresh_token": self.settings.zoho_refresh_token.get_secret_value(),
                    },
                )
            except httpx.HTTPError as exc:
                raise CrmError("zoho_token_transport_error", retryable=True) from exc
            if response.status_code >= 500 or response.status_code == 429:
                raise CrmError(f"zoho_token_http_{response.status_code}", retryable=True)
            if response.status_code >= 400:
                raise CrmError(f"zoho_token_http_{response.status_code}")
            try:
                data = response.json()
                token = data["access_token"]
                expires = int(data.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError) as exc:
                raise CrmError("zoho_token_response_invalid") from exc
            if not isinstance(token, str) or not token:
                raise CrmError("zoho_token_response_invalid")
            self._access_token = token
            self._expires_at = time.monotonic() + max(60, expires)
            api_domain = data.get("api_domain")
            if isinstance(api_domain, str) and api_domain.startswith("https://"):
                self._api_domain = api_domain.rstrip("/")
            return token

    def upsert(self, *, module: str, unique_field: str, record: dict[str, Any]) -> str:
        if not module or not unique_field:
            raise CrmError("zoho_mapping_missing")
        body = {"data": [record], "duplicate_check_fields": [unique_field]}
        for attempt in range(2):
            token = self._token()
            try:
                response = self.client.post(
                    f"{self._api_domain}/crm/v8/{module}/upsert",
                    headers={"Authorization": f"Zoho-oauthtoken {token}"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise CrmError("zoho_upsert_transport_error", retryable=True) from exc
            if response.status_code == 401 and attempt == 0:
                self._access_token = None
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise CrmError(f"zoho_upsert_http_{response.status_code}", retryable=True)
            if response.status_code >= 400:
                raise CrmError(f"zoho_upsert_http_{response.status_code}")
            try:
                row = response.json()["data"][0]
                if row.get("status") != "success" or not row.get("details", {}).get("id"):
                    code = str(row.get("code", "zoho_upsert_rejected"))[:128]
                    raise CrmError(code, retryable=code in {"RECORD_LOCKED", "LIMIT_EXCEEDED"})
                return str(row["details"]["id"])
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise CrmError("zoho_upsert_response_invalid") from exc
        raise CrmError("zoho_authentication_failed")


def parse_mappings(settings: Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        modules = json.loads(settings.zoho_module_mapping_json)
        fields = json.loads(settings.zoho_field_mapping_json)
    except json.JSONDecodeError as exc:
        raise CrmError("zoho_mapping_json_invalid") from exc
    if not isinstance(modules, dict) or not isinstance(fields, dict):
        raise CrmError("zoho_mapping_json_invalid")
    return modules, fields


def map_record(
    kind: str, source: dict[str, Any], mappings: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    config = mappings.get(kind)
    if not isinstance(config, dict):
        raise CrmError(f"zoho_mapping_missing_{kind}")
    module = config.get("module")
    unique_field = config.get("unique_field")
    fields = config.get("fields")
    if (
        not isinstance(module, str)
        or not module
        or not isinstance(unique_field, str)
        or not isinstance(fields, dict)
    ):
        raise CrmError(f"zoho_mapping_invalid_{kind}")
    record: dict[str, Any] = {}
    for source_key, target_key in fields.items():
        if isinstance(source_key, str) and isinstance(target_key, str) and source_key in source:
            value = source[source_key]
            if value is not None:
                record[target_key] = value
    if unique_field not in record:
        raise CrmError(f"zoho_unique_field_unmapped_{kind}")
    return module, unique_field, record
