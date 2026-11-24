import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.integrations.zoho.client import CrmError, FakeCrm, ZohoCrm, map_record


def test_fake_crm_upsert_is_idempotent() -> None:
    crm = FakeCrm()
    first = crm.upsert(
        module="Contacts",
        unique_field="Assistant_Source_Id",
        record={"Assistant_Source_Id": "c-1", "Phone": "+971500000000"},
    )
    second = crm.upsert(
        module="Contacts",
        unique_field="Assistant_Source_Id",
        record={"Assistant_Source_Id": "c-1", "Phone": "+971500000001"},
    )
    assert first == second
    assert len(crm.records) == 1


def test_mapping_requires_explicit_unique_field() -> None:
    with pytest.raises(CrmError, match="unique_field_unmapped"):
        map_record(
            "contact",
            {"id": "local"},
            {
                "contact": {
                    "module": "Contacts",
                    "unique_field": "Assistant_Source_Id",
                    "fields": {"phone_e164": "Phone"},
                }
            },
        )


def test_zoho_refresh_and_upsert_use_documented_auth_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "accounts.zoho.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "temporary-token",
                    "expires_in": 3600,
                    "api_domain": "https://www.zohoapis.com",
                },
            )
        return httpx.Response(
            200, json={"data": [{"status": "success", "details": {"id": "remote-1"}}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        app_env="test",
        zoho_enabled=True,
        zoho_client_id=SecretStr("client"),
        zoho_client_secret=SecretStr("secret"),
        zoho_refresh_token=SecretStr("refresh"),
    )
    crm = ZohoCrm(settings, client=client)
    record_id = crm.upsert(
        module="Contacts",
        unique_field="Assistant_Source_Id",
        record={"Assistant_Source_Id": "local-1"},
    )
    assert record_id == "remote-1"
    assert requests[0].url.path == "/oauth/v2/token"
    assert requests[1].url.path == "/crm/v8/Contacts/upsert"
    assert requests[1].headers["Authorization"] == "Zoho-oauthtoken temporary-token"
