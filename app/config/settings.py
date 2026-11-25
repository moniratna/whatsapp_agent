import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _configured(value: str | SecretStr) -> bool:
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    return bool(raw.strip()) and raw.strip().casefold() not in {
        "replace-me",
        "replace_me",
        "changeme",
        "your-value-here",
    }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_release: str = "local"
    run_workers_in_api: bool = False
    database_url: str = "postgresql+psycopg://assistant:assistant@localhost:5432/assistant"

    openwa_base_url: str = "http://localhost:8001"
    openwa_api_key: SecretStr = SecretStr("")
    openwa_webhook_secret: SecretStr = SecretStr("")
    openwa_enabled: bool = False
    openwa_transport_mode: Literal["fake", "live"] = "fake"
    openwa_interactive_enabled: bool = False
    webhook_max_bytes: int = Field(default=1_048_576, gt=0, le=10_485_760)
    response_sla_minutes: int = Field(default=30, gt=0, le=10_080)
    exports_directory: str = "var/private_exports"
    export_retention_hours: int = Field(default=24, gt=0, le=720)
    exports_s3_bucket: str = ""
    exports_s3_prefix: str = "private/transcript-exports"

    aws_region: str = "us-east-1"
    bedrock_model_id: str = ""
    aws_bearer_token_bedrock: SecretStr = SecretStr("")
    bedrock_enabled: bool = False

    zoho_accounts_url: str = "https://accounts.zoho.com"
    zoho_api_domain: str = "https://www.zohoapis.com"
    zoho_client_id: SecretStr = SecretStr("")
    zoho_client_secret: SecretStr = SecretStr("")
    zoho_refresh_token: SecretStr = SecretStr("")
    zoho_enabled: bool = False
    zoho_module_mapping_json: str = "{}"
    zoho_field_mapping_json: str = "{}"

    auth_mode: Literal["development", "oidc"] = "development"
    dashboard_api_key: SecretStr = SecretStr("")
    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
    oidc_jwks_url: str | None = None
    oidc_role_claim: str = "roles"

    ai_mode: Literal["off", "shadow", "assisted", "canary", "active"] = "shadow"
    ai_kill_switch: bool = True
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_production_auth(self) -> "Settings":
        if self.app_env in {"staging", "production"} and self.auth_mode != "oidc":
            raise ValueError("Staging and production require AUTH_MODE=oidc")
        if self.app_env in {"staging", "production"} and self.openwa_transport_mode != "live":
            raise ValueError("Staging and production require OPENWA_TRANSPORT_MODE=live")
        if self.auth_mode == "oidc" and not (self.oidc_issuer_url and self.oidc_client_id):
            raise ValueError("OIDC_ISSUER_URL and OIDC_CLIENT_ID are required when AUTH_MODE=oidc")
        return self

    def readiness_issues(self) -> list[str]:
        issues: list[str] = []
        if self.app_env in {"staging", "production"} and not self.openwa_enabled:
            issues.append("Open-wa integration is disabled")
        if self.openwa_enabled and not _configured(self.openwa_webhook_secret):
            issues.append("Open-wa webhook secret is missing")
        if (
            self.openwa_enabled
            and self.openwa_transport_mode == "live"
            and not _configured(self.openwa_api_key)
        ):
            issues.append("Open-wa API key is missing")
        if self.bedrock_enabled and not _configured(self.bedrock_model_id):
            issues.append("Bedrock model ID is missing")
        if self.bedrock_enabled and not _configured(self.aws_bearer_token_bedrock):
            issues.append("Bedrock bearer token is missing")
        if self.app_env in {"staging", "production"} and not self.bedrock_enabled:
            issues.append("Bedrock integration is disabled")
        if self.zoho_enabled and not all(
            _configured(value)
            for value in (self.zoho_client_id, self.zoho_client_secret, self.zoho_refresh_token)
        ):
            issues.append("Zoho OAuth credentials are incomplete")
        if self.zoho_enabled:
            try:
                modules = json.loads(self.zoho_module_mapping_json)
                fields = json.loads(self.zoho_field_mapping_json)
                mapping_ready = all(
                    isinstance(modules.get(kind), str)
                    and isinstance(fields.get(kind), dict)
                    and isinstance(fields[kind].get("unique_field"), str)
                    and isinstance(fields[kind].get("fields"), dict)
                    for kind in ("contact", "requirement", "portfolio_asset")
                )
            except (json.JSONDecodeError, AttributeError):
                mapping_ready = False
            if not mapping_ready:
                issues.append("Zoho module and field mappings are incomplete")
        elif self.app_env in {"staging", "production"}:
            issues.append("Zoho CRM integration is disabled")
        if self.app_env in {"staging", "production"} and not self.exports_s3_bucket:
            issues.append("Private S3 export bucket is required outside local development")
        return issues


@lru_cache
def get_settings() -> Settings:
    return Settings()
