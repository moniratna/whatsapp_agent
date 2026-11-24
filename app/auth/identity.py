from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings
from app.db.models import AdminUser

AdminRole = Literal["admin", "supervisor", "read_only"]


@dataclass(frozen=True)
class AdminPrincipal:
    subject_id: str
    role: AdminRole
    email: str
    display_name: str | None


@lru_cache(maxsize=16)
def _discovery_jwks_url(issuer: str) -> str:
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    response = httpx.get(discovery_url, timeout=5.0)
    response.raise_for_status()
    metadata = response.json()
    if not isinstance(metadata, dict) or metadata.get("issuer") != issuer:
        raise ValueError("OIDC discovery issuer mismatch")
    jwks_url = metadata.get("jwks_uri")
    if not isinstance(jwks_url, str) or not jwks_url.startswith("https://"):
        raise ValueError("OIDC JWKS URL must use HTTPS")
    return jwks_url


@lru_cache(maxsize=16)
def _jwk_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url, timeout=5, cache_jwk_set=True)


def _oidc_principal(authorization: str | None, settings: Settings) -> AdminPrincipal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    if not token or len(token) > 16_384:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    try:
        issuer = settings.oidc_issuer_url
        audience = settings.oidc_client_id
        if issuer is None or audience is None:
            raise ValueError("OIDC is not configured")
        jwks_url = settings.oidc_jwks_url or _discovery_jwks_url(issuer)
        if not jwks_url.startswith("https://"):
            raise ValueError("OIDC JWKS URL must use HTTPS")
        signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "sub"]},
            leeway=30,
        )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("missing subject")
        raw_roles = claims.get(settings.oidc_role_claim)
        roles = [raw_roles] if isinstance(raw_roles, str) else raw_roles
        if not isinstance(roles, list):
            raise ValueError("missing role claim")
        recognized = {role for role in roles if role in {"admin", "supervisor", "read_only"}}
        role: AdminRole
        if "supervisor" in recognized:
            role = "supervisor"
        elif "admin" in recognized:
            role = "admin"
        elif "read_only" in recognized:
            role = "read_only"
        else:
            raise HTTPException(status_code=403, detail="required role is missing")
        email = claims.get("email")
        display_name = claims.get("name")
        return AdminPrincipal(
            subject_id=subject,
            role=role,
            email=email if isinstance(email, str) else f"{subject}@identity.invalid",
            display_name=display_name if isinstance(display_name, str) else None,
        )
    except HTTPException:
        raise
    except (PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or unverifiable bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_current_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
    dev_user: Annotated[str | None, Header(alias="X-Dev-User")] = None,
    dev_role: Annotated[str | None, Header(alias="X-Dev-Role")] = None,
) -> AdminPrincipal:
    if settings.auth_mode == "oidc":
        return _oidc_principal(authorization, settings)
    if settings.app_env not in {"development", "test"}:
        raise HTTPException(status_code=503, detail="development identity is disabled")
    if not dev_user or len(dev_user) > 256:
        raise HTTPException(status_code=401, detail="development identity header required")
    role = dev_role or "read_only"
    if role not in {"admin", "supervisor", "read_only"}:
        raise HTTPException(status_code=403, detail="invalid development role")
    return AdminPrincipal(
        subject_id=f"dev:{dev_user}",
        role=role,  # type: ignore[arg-type]
        email=f"{dev_user}@dev.local",
        display_name=dev_user,
    )


def require_roles(*roles: AdminRole) -> Callable[..., AdminPrincipal]:
    def check_role(
        principal: Annotated[AdminPrincipal, Depends(get_current_principal)],
    ) -> AdminPrincipal:
        if principal.role not in roles:
            raise HTTPException(status_code=403, detail="insufficient role")
        return principal

    return check_role


def ensure_admin_user(session: Session, principal: AdminPrincipal) -> AdminUser:
    user = session.scalar(
        select(AdminUser).where(AdminUser.subject_id == principal.subject_id).with_for_update()
    )
    if user is None:
        bind = session.get_bind()
        insert_fn = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
        session.execute(
            insert_fn(AdminUser)
            .values(
                subject_id=principal.subject_id,
                email=principal.email,
                display_name=principal.display_name,
                role=principal.role,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["subject_id"])
        )
        user = session.scalar(
            select(AdminUser).where(AdminUser.subject_id == principal.subject_id).with_for_update()
        )
    if user is None or not user.is_active:
        raise HTTPException(status_code=403, detail="administrator account is disabled")
    user.email = principal.email
    user.display_name = principal.display_name
    user.role = principal.role
    user.updated_at = datetime.now(UTC)
    session.flush()
    return user
