from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config.settings import Settings


def test_skill_update_rollout_flags_are_independently_off_and_ordered(tmp_path: Path) -> None:
    defaults = Settings(_env_file=None)
    assert not defaults.skill_updates_enabled
    assert not defaults.skill_update_extraction_enabled
    assert not defaults.skill_update_candidate_writes_enabled
    assert not defaults.skill_update_consolidation_enabled
    assert not defaults.skill_update_auto_merge_enabled
    assert not defaults.skill_reviewer_updates_enabled

    storage = tmp_path / "skill-updates"
    storage.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="EXTRACTION_ENABLED is required"):
        Settings(
            _env_file=None,
            app_env="test",
            skill_updates_enabled=True,
            skill_update_candidate_writes_enabled=True,
        )


def test_safe_shadow_kill_switch_does_not_make_readiness_fail() -> None:
    settings = Settings(
        app_env="production",
        auth_mode="oidc",
        oidc_issuer_url="https://identity.example.test",
        oidc_client_id="assistant",
        openwa_transport_mode="live",
        ai_mode="shadow",
        ai_kill_switch=True,
    )
    # Production credential/config issues are reported separately; the safe kill switch itself
    # is not an availability failure.
    assert "AI kill switch is enabled" not in settings.readiness_issues()


def test_fakes_are_not_reported_as_live_staging_integrations() -> None:
    settings = Settings(
        _env_file=None,
        app_env="staging",
        auth_mode="oidc",
        oidc_issuer_url="https://identity.example.test",
        oidc_client_id="assistant",
        openwa_transport_mode="live",
        openwa_enabled=False,
    )
    issues = settings.readiness_issues()
    assert "Open-wa integration is disabled" in issues
    assert "Bedrock integration is disabled" in issues
    assert "Zoho CRM integration is disabled" in issues
    assert "Private S3 export bucket is required outside local development" in issues


def test_placeholder_secrets_do_not_pass_configured_integration_readiness() -> None:
    settings = Settings(
        app_env="development",
        openwa_enabled=True,
        openwa_transport_mode="live",
        openwa_api_key=SecretStr("replace-me"),
        openwa_webhook_secret=SecretStr("replace-me"),
        bedrock_enabled=True,
        bedrock_model_id="replace-me",
        aws_bearer_token_bedrock=SecretStr("replace-me"),
    )
    issues = settings.readiness_issues()
    assert "Open-wa webhook secret is missing" in issues
    assert "Open-wa API key is missing" in issues
    assert "Bedrock model ID is missing" in issues
    assert "Bedrock bearer token is missing" in issues


def test_configured_production_integrations_can_pass_settings_readiness() -> None:
    settings = Settings(
        app_env="production",
        app_release="test",
        database_url="sqlite://",
        openwa_enabled=True,
        openwa_transport_mode="live",
        openwa_api_key=SecretStr("open-wa-key"),
        openwa_webhook_secret=SecretStr("webhook-secret"),
        bedrock_enabled=True,
        bedrock_model_id="model-id",
        aws_bearer_token_bedrock=SecretStr("bedrock-key"),
        zoho_enabled=True,
        zoho_client_id=SecretStr("client-id"),
        zoho_client_secret=SecretStr("client-secret"),
        zoho_refresh_token=SecretStr("refresh-token"),
        zoho_module_mapping_json=(
            '{"contact":"Contacts","requirement":"Deals","portfolio_asset":"Property_Assets"}'
        ),
        zoho_field_mapping_json=(
            '{"contact":{"unique_field":"Assistant_Source_Id","fields":{"id":"Assistant_Source_Id"}},'
            '"requirement":{"unique_field":"Assistant_Source_Id","fields":{"id":"Assistant_Source_Id"}},'
            '"portfolio_asset":{"unique_field":"Assistant_Source_Id","fields":{"id":"Assistant_Source_Id"}}}'
        ),
        auth_mode="oidc",
        oidc_issuer_url="https://identity.example.test",
        oidc_client_id="assistant",
        exports_s3_bucket="private-test-bucket",
    )
    assert settings.readiness_issues() == []
