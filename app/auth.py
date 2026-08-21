from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jwt
from authlib.integrations.starlette_client import OAuth
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
from starlette.requests import Request

from .config import Settings

logger = logging.getLogger("ollum-sales-auth")


class AuthConfigurationError(RuntimeError):
    """Raised when a requested authentication mode is incomplete."""


class AuthenticationError(RuntimeError):
    """Raised when an identity cannot be authenticated or authorized."""


def _split_scopes(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item for item in value.replace(",", " ").split() if item}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    """Return a stable union of OAuth scope and Auth0-style permissions claims."""
    return sorted(
        _split_scopes(claims.get("scope")) | _split_scopes(claims.get("permissions"))
    )


class OIDCAccessTokenVerifier(TokenVerifier):
    """Validate signed OIDC access tokens before MCP tools see a request."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: tuple[str, ...] = ("RS256",),
        allowed_subjects: tuple[str, ...] = (),
        leeway_seconds: int = 30,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.algorithms = list(algorithms)
        self.allowed_subjects = frozenset(allowed_subjects)
        self.leeway_seconds = leeway_seconds
        self.jwks_client = PyJWKClient(
            jwks_url,
            cache_keys=True,
            max_cached_keys=16,
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )

    def _decode(self, token: str) -> dict[str, Any]:
        signing_key = self.jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=self.algorithms,
            audience=self.audience,
            issuer=self.issuer,
            leeway=self.leeway_seconds,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
        return dict(claims)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await asyncio.to_thread(self._decode, token)
        except (InvalidTokenError, PyJWKClientError, OSError, ValueError):
            logger.warning("Rejected an invalid OIDC access token")
            return None

        subject = str(claims.get("sub") or "")
        if not subject or (
            self.allowed_subjects and subject not in self.allowed_subjects
        ):
            logger.warning("Rejected an OIDC subject outside the beta allowlist")
            return None

        scopes = scopes_from_claims(claims)
        audience = claims.get("aud")
        resource = audience if isinstance(audience, str) else self.audience
        client_id = str(claims.get("azp") or claims.get("client_id") or subject)
        safe_claims = {
            name: claims[name]
            for name in ("sub", "iss", "aud", "azp", "scope", "permissions")
            if name in claims
        }
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=resource,
            subject=subject,
            claims=safe_claims,
        )


@dataclass(frozen=True)
class MCPAuthBundle:
    verifier: OIDCAccessTokenVerifier
    settings: AuthSettings


def build_mcp_auth(settings: Settings) -> MCPAuthBundle | None:
    if settings.auth_mode in {"none", "bearer"}:
        return None
    if settings.auth_mode != "oidc":
        raise AuthConfigurationError(
            "OLLUM_AUTH_MODE must be one of: none, bearer, oidc"
        )

    resource_url = settings.mcp_resource_url
    if not resource_url and settings.public_base_url:
        resource_url = f"{settings.public_base_url.rstrip('/')}/mcp"
    missing = [
        name
        for name, value in (
            ("OLLUM_OIDC_ISSUER_URL", settings.oidc_issuer_url),
            ("OLLUM_OIDC_AUDIENCE", settings.oidc_audience),
            ("OLLUM_MCP_RESOURCE_URL or OLLUM_PUBLIC_BASE_URL", resource_url),
        )
        if not value
    ]
    if missing:
        raise AuthConfigurationError(
            f"OIDC authentication is missing: {', '.join(missing)}"
        )
    assert settings.oidc_issuer_url is not None
    assert settings.oidc_audience is not None
    assert resource_url is not None
    jwks_url = settings.oidc_jwks_url or (
        f"{settings.oidc_issuer_url.rstrip('/')}/.well-known/jwks.json"
    )
    verifier = OIDCAccessTokenVerifier(
        issuer=settings.oidc_issuer_url,
        audience=settings.oidc_audience,
        jwks_url=jwks_url,
        algorithms=settings.oidc_algorithms,
        allowed_subjects=settings.oidc_allowed_subjects,
    )
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(settings.oidc_issuer_url),
        resource_server_url=AnyHttpUrl(resource_url),
        required_scopes=list(settings.mcp_required_scopes),
    )
    return MCPAuthBundle(verifier=verifier, settings=auth_settings)


class OIDCSessionManager:
    """Run the dashboard's OIDC code flow without placing tokens in cookies."""

    def __init__(
        self,
        settings: Settings,
        token_verifier: OIDCAccessTokenVerifier,
        identity_authorizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        dashboard_base_url = settings.dashboard_base_url or settings.public_base_url
        required = [
            name
            for name, value in (
                (
                    "OLLUM_DASHBOARD_BASE_URL or OLLUM_PUBLIC_BASE_URL",
                    dashboard_base_url,
                ),
                ("OLLUM_OIDC_ISSUER_URL", settings.oidc_issuer_url),
                ("OLLUM_OIDC_AUDIENCE", settings.oidc_audience),
                ("OLLUM_ADMIN_OIDC_CLIENT_ID", settings.admin_oidc_client_id),
                ("OLLUM_ADMIN_OIDC_CLIENT_SECRET", settings.admin_oidc_client_secret),
                ("OLLUM_ADMIN_SESSION_SECRET", settings.admin_session_secret),
                ("OLLUM_ADMIN_ALLOWED_EMAILS", settings.admin_allowed_emails),
            )
            if not value
        ]
        if required:
            raise AuthConfigurationError(
                f"Admin OIDC authentication is missing: {', '.join(required)}"
            )
        parsed_dashboard = urlsplit(str(dashboard_base_url))
        if parsed_dashboard.scheme != "https" or not parsed_dashboard.netloc:
            raise AuthConfigurationError(
                "OLLUM_DASHBOARD_BASE_URL must be an absolute HTTPS origin"
            )
        if parsed_dashboard.path not in {"", "/"} or parsed_dashboard.query:
            raise AuthConfigurationError(
                "OLLUM_DASHBOARD_BASE_URL must not contain a path or query"
            )
        assert settings.oidc_issuer_url is not None
        assert settings.admin_oidc_client_id is not None
        assert settings.admin_oidc_client_secret is not None
        self.settings = settings
        self.token_verifier = token_verifier
        self.identity_authorizer = identity_authorizer
        self.dashboard_base_url = dashboard_base_url
        self.oauth = OAuth()
        self.oauth.register(
            "ollum",
            client_id=settings.admin_oidc_client_id,
            client_secret=settings.admin_oidc_client_secret,
            server_metadata_url=(
                f"{settings.oidc_issuer_url.rstrip('/')}/.well-known/openid-configuration"
            ),
            client_kwargs={
                "scope": "openid profile email "
                + " ".join(settings.mcp_required_scopes)
            },
        )

    async def begin_login(self, request: Request):
        client = self.oauth.create_client("ollum")
        assert client is not None
        assert self.dashboard_base_url is not None
        redirect_uri = f"{self.dashboard_base_url.rstrip('/')}/auth/callback"
        kwargs: dict[str, Any] = {}
        if self.settings.oidc_audience:
            kwargs["audience"] = self.settings.oidc_audience
        return await client.authorize_redirect(request, redirect_uri, **kwargs)

    async def complete_login(self, request: Request) -> dict[str, Any]:
        client = self.oauth.create_client("ollum")
        assert client is not None
        token = await client.authorize_access_token(request)
        raw_access_token = str(token.get("access_token") or "")
        verified = (
            await self.token_verifier.verify_token(raw_access_token)
            if raw_access_token
            else None
        )
        if verified is None:
            raise AuthenticationError(
                "The identity provider returned an invalid access token"
            )

        if (
            self.settings.admin_read_scope
            and self.settings.admin_read_scope not in verified.scopes
        ):
            raise AuthenticationError(
                f"The account is missing scope: {self.settings.admin_read_scope}"
            )

        userinfo = dict(token.get("userinfo") or {})
        userinfo_subject = str(userinfo.get("sub") or "")
        if not userinfo_subject or userinfo_subject != verified.subject:
            raise AuthenticationError(
                "The identity token and access token identify different subjects"
            )
        email = str(userinfo.get("email") or "").strip().lower()
        if not email:
            raise AuthenticationError("A verified email address is required")
        if userinfo.get("email_verified") is False:
            raise AuthenticationError("A verified email address is required")

        membership: dict[str, Any] | None = None
        bootstrap_allowed = email in self.settings.admin_allowed_emails
        if self.identity_authorizer is not None:
            try:
                membership = self.identity_authorizer(
                    {
                        "subject": userinfo_subject,
                        "email": email,
                        "name": str(userinfo.get("name") or email),
                        "bootstrap_allowed": bootstrap_allowed,
                    }
                )
            except ValueError as exc:
                raise AuthenticationError(str(exc)) from exc
        elif not bootstrap_allowed:
            raise AuthenticationError(
                "This account is not in the closed-beta allowlist"
            )

        user = {
            "sub": userinfo_subject,
            "email": email,
            "name": str(userinfo.get("name") or email),
            "scopes": verified.scopes,
            "csrf": secrets.token_urlsafe(32),
            "expires_at": verified.expires_at,
        }
        if membership is not None:
            workspace = membership.get("workspace")
            if not isinstance(workspace, dict):
                raise AuthenticationError(
                    "Workspace authorization returned invalid data"
                )
            user.update(
                {
                    "workspace_id": membership.get("workspace_id"),
                    "workspace_name": workspace.get("name"),
                    "member_id": membership.get("id"),
                    "role": membership.get("role"),
                }
            )
        request.session.clear()
        request.session["user"] = user
        return user

    @staticmethod
    def logout(request: Request) -> None:
        request.session.clear()
