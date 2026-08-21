from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from authlib.oauth2.rfc6749.errors import MismatchingStateException
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request

from app.auth import (
    AuthConfigurationError,
    AuthenticationError,
    OIDCAccessTokenVerifier,
    OIDCSessionManager,
    build_mcp_auth,
    scopes_from_claims,
)
from app.config import settings


def _verifier(*, allowed_subjects: tuple[str, ...] = ()):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = OIDCAccessTokenVerifier(
        issuer="https://identity.example/",
        audience="https://api.example",
        jwks_url="https://identity.example/.well-known/jwks.json",
        allowed_subjects=allowed_subjects,
        leeway_seconds=0,
    )
    verifier.jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(
            key=private_key.public_key()
        )
    )
    return verifier, private_key


def _token(private_key, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": "auth0|beta-user",
        "iss": "https://identity.example/",
        "aud": "https://api.example",
        "exp": now + 600,
        "iat": now,
        "scope": "openid sales:read",
        "permissions": ["sales:write"],
        "azp": "chatgpt-client",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_scopes_from_claims_merges_standard_and_permissions() -> None:
    assert scopes_from_claims(
        {"scope": "sales:read profile", "permissions": ["sales:write", "profile"]}
    ) == ["profile", "sales:read", "sales:write"]


def test_oidc_verifier_accepts_valid_signed_token() -> None:
    verifier, private_key = _verifier(allowed_subjects=("auth0|beta-user",))

    access = asyncio.run(verifier.verify_token(_token(private_key)))

    assert access is not None
    assert access.subject == "auth0|beta-user"
    assert access.client_id == "chatgpt-client"
    assert access.scopes == ["openid", "sales:read", "sales:write"]
    assert access.token


@pytest.mark.parametrize(
    ("claim_overrides", "allowed_subjects"),
    [
        ({"aud": "https://wrong.example"}, ()),
        ({"exp": 1}, ()),
        ({"sub": "auth0|outsider"}, ("auth0|beta-user",)),
    ],
)
def test_oidc_verifier_rejects_invalid_or_unlisted_tokens(
    claim_overrides: dict[str, Any], allowed_subjects: tuple[str, ...]
) -> None:
    verifier, private_key = _verifier(allowed_subjects=allowed_subjects)

    assert (
        asyncio.run(verifier.verify_token(_token(private_key, **claim_overrides)))
        is None
    )


def test_build_mcp_auth_is_fail_closed_and_derives_resource_url() -> None:
    incomplete = replace(
        settings,
        auth_mode="oidc",
        public_base_url=None,
        mcp_resource_url=None,
        oidc_issuer_url=None,
        oidc_audience=None,
    )
    with pytest.raises(AuthConfigurationError, match="OIDC authentication is missing"):
        build_mcp_auth(incomplete)

    configured = replace(
        settings,
        auth_mode="oidc",
        public_base_url="https://sales.example",
        mcp_resource_url=None,
        oidc_issuer_url="https://identity.example/",
        oidc_audience="https://api.example",
        mcp_required_scopes=("sales:read", "sales:write"),
    )
    bundle = build_mcp_auth(configured)

    assert bundle is not None
    assert str(bundle.settings.resource_server_url).rstrip("/") == (
        "https://sales.example/mcp"
    )
    assert bundle.settings.required_scopes == ["sales:read", "sales:write"]


class _FakeOIDCClient:
    def __init__(self, token: dict[str, Any]) -> None:
        self.token = token
        self.redirect_uri: str | None = None
        self.redirect_kwargs: dict[str, Any] = {}

    async def authorize_access_token(self, _request: Request) -> dict[str, Any]:
        return self.token

    async def authorize_redirect(
        self, _request: Request, redirect_uri: str, **kwargs: Any
    ) -> SimpleNamespace:
        self.redirect_uri = redirect_uri
        self.redirect_kwargs = kwargs
        return SimpleNamespace(status_code=302)


class _FailingOIDCClient:
    async def authorize_access_token(self, _request: Request) -> dict[str, Any]:
        raise MismatchingStateException()


class _FakeOAuth:
    def __init__(self, client: Any) -> None:
        self.client = client

    def create_client(self, _name: str) -> Any:
        return self.client


class _FakeVerifier:
    def __init__(self, access: AccessToken | None) -> None:
        self.access = access

    async def verify_token(self, _token: str) -> AccessToken | None:
        return self.access


def _admin_settings():
    return replace(
        settings,
        public_base_url="https://sales.example",
        oidc_issuer_url="https://identity.example/",
        oidc_audience="https://api.example",
        admin_oidc_client_id="admin-client",
        admin_oidc_client_secret="client-secret",
        admin_session_secret="session-secret-that-is-long-enough",
        admin_allowed_emails=("operator@example.com",),
        admin_read_scope="sales:read",
    )


def test_admin_oidc_requires_https_dashboard_origin() -> None:
    configured = replace(
        _admin_settings(),
        dashboard_base_url="http://api.sales.example/path",
    )

    with pytest.raises(
        AuthConfigurationError,
        match="OLLUM_DASHBOARD_BASE_URL must be an absolute HTTPS origin",
    ):
        OIDCSessionManager(configured, _FakeVerifier(None))  # type: ignore[arg-type]


def _request_for_host(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/auth/login",
            "headers": [(b"host", host.encode("ascii"))],
            "server": (host, 443),
            "session": {},
        }
    )


def test_admin_oidc_supports_separate_callback_origin_with_one_time_handoff() -> None:
    configured = replace(
        _admin_settings(),
        dashboard_base_url="https://api.sales.example",
        oidc_redirect_base_url="https://mcp.sales.example",
    )
    oidc_client = _FakeOIDCClient({})
    manager = OIDCSessionManager(configured, _FakeVerifier(None))  # type: ignore[arg-type]
    manager.oauth = _FakeOAuth(oidc_client)  # type: ignore[assignment]

    asyncio.run(manager.begin_login(_request_for_host("mcp.sales.example")))

    assert oidc_client.redirect_uri == "https://mcp.sales.example/auth/callback"
    assert oidc_client.redirect_kwargs["audience"] == "https://api.example"
    assert manager.login_must_start_on_redirect_host(
        _request_for_host("api.sales.example")
    )
    assert not manager.login_must_start_on_redirect_host(
        _request_for_host("mcp.sales.example")
    )
    assert manager.login_start_url() == "https://mcp.sales.example/auth/login"
    assert manager.dashboard_url("/admin") == "https://api.sales.example/admin"

    code = manager.issue_login_handoff({"email": "operator@example.com"})
    assert manager.consume_login_handoff(code) == {"email": "operator@example.com"}
    assert manager.consume_login_handoff(code) is None

    manager._HANDOFF_TTL_SECONDS = -1
    expired_code = manager.issue_login_handoff({"email": "operator@example.com"})
    assert manager.consume_login_handoff(expired_code) is None


def test_admin_oidc_turns_invalid_state_into_safe_authentication_error() -> None:
    manager = OIDCSessionManager(_admin_settings(), _FakeVerifier(None))  # type: ignore[arg-type]
    manager.oauth = _FakeOAuth(_FailingOIDCClient())  # type: ignore[assignment]

    with pytest.raises(
        AuthenticationError, match="login session expired or is invalid"
    ):
        asyncio.run(manager.complete_login(_request_for_host("sales.example")))


def test_admin_login_keeps_access_token_out_of_signed_session() -> None:
    expires_at = int(time.time()) + 600
    access = AccessToken(
        token="raw-access-token",
        client_id="admin-client",
        scopes=["sales:read", "sales:write"],
        expires_at=expires_at,
        resource="https://api.example",
        subject="auth0|beta-user",
    )
    manager = OIDCSessionManager(_admin_settings(), _FakeVerifier(access))  # type: ignore[arg-type]
    manager.oauth = _FakeOAuth(  # type: ignore[assignment]
        _FakeOIDCClient(
            {
                "access_token": "raw-access-token",
                "userinfo": {
                    "sub": "auth0|beta-user",
                    "email": "operator@example.com",
                    "email_verified": True,
                    "name": "Beta Operator",
                },
            }
        )
    )
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "session": {}}
    )

    user = asyncio.run(manager.complete_login(request))

    assert user["email"] == "operator@example.com"
    assert user["expires_at"] == expires_at
    assert "raw-access-token" not in repr(request.session)
    assert len(user["csrf"]) >= 32


def test_admin_login_binds_same_workspace_identity_used_by_mcp() -> None:
    expires_at = int(time.time()) + 600
    access = AccessToken(
        token="raw-access-token",
        client_id="admin-client",
        scopes=["sales:read", "sales:write"],
        expires_at=expires_at,
        subject="auth0|beta-user",
    )
    observed: list[dict[str, Any]] = []

    def authorize(identity: dict[str, Any]) -> dict[str, Any]:
        observed.append(identity)
        return {
            "id": "member-1",
            "workspace_id": "ollum-group",
            "role": "owner",
            "workspace": {"id": "ollum-group", "name": "Ollum Group"},
        }

    manager = OIDCSessionManager(_admin_settings(), _FakeVerifier(access), authorize)  # type: ignore[arg-type]
    manager.oauth = _FakeOAuth(  # type: ignore[assignment]
        _FakeOIDCClient(
            {
                "access_token": "raw-access-token",
                "userinfo": {
                    "sub": "auth0|beta-user",
                    "email": "operator@example.com",
                    "email_verified": True,
                    "name": "Beta Operator",
                },
            }
        )
    )
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "session": {}}
    )

    user = asyncio.run(manager.complete_login(request))

    assert observed[0]["subject"] == access.subject
    assert observed[0]["bootstrap_allowed"] is True
    assert user["workspace_id"] == "ollum-group"
    assert user["workspace_name"] == "Ollum Group"
    assert user["role"] == "owner"
    assert "raw-access-token" not in repr(request.session)


def test_admin_login_rejects_missing_read_scope() -> None:
    access = AccessToken(
        token="token",
        client_id="admin-client",
        scopes=["openid"],
        expires_at=int(time.time()) + 600,
        subject="auth0|beta-user",
    )
    manager = OIDCSessionManager(_admin_settings(), _FakeVerifier(access))  # type: ignore[arg-type]
    manager.oauth = _FakeOAuth(  # type: ignore[assignment]
        _FakeOIDCClient(
            {
                "access_token": "token",
                "userinfo": {
                    "sub": "auth0|beta-user",
                    "email": "operator@example.com",
                    "email_verified": True,
                },
            }
        )
    )
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "session": {}}
    )

    with pytest.raises(AuthenticationError, match="missing scope"):
        asyncio.run(manager.complete_login(request))


def test_admin_login_rejects_mismatched_token_subjects() -> None:
    access = AccessToken(
        token="token",
        client_id="admin-client",
        scopes=["sales:read"],
        expires_at=int(time.time()) + 600,
        subject="auth0|access-subject",
    )
    manager = OIDCSessionManager(_admin_settings(), _FakeVerifier(access))  # type: ignore[arg-type]
    manager.oauth = _FakeOAuth(  # type: ignore[assignment]
        _FakeOIDCClient(
            {
                "access_token": "token",
                "userinfo": {
                    "sub": "auth0|different-subject",
                    "email": "operator@example.com",
                    "email_verified": True,
                },
            }
        )
    )
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "session": {}}
    )

    with pytest.raises(AuthenticationError, match="different subjects"):
        asyncio.run(manager.complete_login(request))
