from __future__ import annotations

import asyncio
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from app.oauth_server import PersistentOAuthProvider, safe_post_login_redirect


def _provider(tmp_path, *, secret: str = "s" * 48) -> PersistentOAuthProvider:
    return PersistentOAuthProvider(
        db_path=tmp_path / "oauth.db",
        dashboard_base_url="https://api.sales.example",
        resource_url="https://mcp.sales.example/mcp",
        storage_secret=secret,
        allowed_redirect_hosts=("chatgpt.com",),
        access_token_ttl_seconds=600,
        refresh_token_ttl_seconds=1200,
        authorization_code_ttl_seconds=300,
    )


def _client(
    *, redirect_uri: str = "https://chatgpt.com/connector/oauth/test"
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="chatgpt-client",
        client_secret="client-secret-value",
        client_id_issued_at=1,
        redirect_uris=[AnyUrl(redirect_uri)],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="sales:read sales:write",
        client_name="Ollum Sales Test",
    )


def test_persistent_oauth_flow_rotates_tokens_and_survives_restart(tmp_path) -> None:
    provider = _provider(tmp_path)
    client = _client()
    asyncio.run(provider.register_client(client))

    authorize_url = asyncio.run(
        provider.authorize(
            client,
            AuthorizationParams(
                state="chatgpt-state",
                scopes=["sales:read", "sales:write"],
                code_challenge="pkce-challenge",
                redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/test"),
                redirect_uri_provided_explicitly=True,
                resource="https://mcp.sales.example/mcp",
            ),
        )
    )
    request_id = parse_qs(urlsplit(authorize_url).query)["request_id"][0]
    assert provider.pending_authorization(request_id) == {
        "client_id": "chatgpt-client",
        "scopes": ["sales:read", "sales:write"],
        "expires_at": provider.pending_authorization(request_id)["expires_at"],
    }

    callback_url = provider.complete_authorization(
        request_id,
        subject="auth0|beta-user",
        approved=True,
    )
    assert callback_url is not None
    callback_params = parse_qs(urlsplit(callback_url).query)
    assert callback_params["state"] == ["chatgpt-state"]
    code_value = callback_params["code"][0]

    authorization_code = asyncio.run(
        provider.load_authorization_code(client, code_value)
    )
    assert authorization_code is not None
    assert authorization_code.subject == "auth0|beta-user"
    tokens = asyncio.run(
        provider.exchange_authorization_code(client, authorization_code)
    )
    assert tokens.refresh_token
    access = asyncio.run(provider.load_access_token(tokens.access_token))
    assert access is not None
    assert access.subject == "auth0|beta-user"
    assert access.scopes == ["sales:read", "sales:write"]

    restarted = _provider(tmp_path)
    stored_client = asyncio.run(restarted.get_client("chatgpt-client"))
    assert stored_client is not None
    assert stored_client.client_secret == "client-secret-value"
    assert asyncio.run(restarted.load_access_token(tokens.access_token)) is not None

    refresh = asyncio.run(restarted.load_refresh_token(client, tokens.refresh_token))
    assert refresh is not None
    rotated = asyncio.run(
        restarted.exchange_refresh_token(client, refresh, ["sales:read"])
    )
    assert asyncio.run(restarted.load_access_token(tokens.access_token)) is None
    assert (
        asyncio.run(restarted.load_refresh_token(client, tokens.refresh_token)) is None
    )
    rotated_access = asyncio.run(restarted.load_access_token(rotated.access_token))
    assert rotated_access is not None
    assert rotated_access.scopes == ["sales:read"]

    asyncio.run(restarted.revoke_token(rotated_access))
    assert asyncio.run(restarted.load_access_token(rotated.access_token)) is None
    assert rotated.refresh_token is not None
    assert (
        asyncio.run(restarted.load_refresh_token(client, rotated.refresh_token)) is None
    )


def test_oauth_secrets_and_bearer_tokens_are_not_stored_in_plaintext(tmp_path) -> None:
    provider = _provider(tmp_path)
    client = _client()
    asyncio.run(provider.register_client(client))
    authorize_url = asyncio.run(
        provider.authorize(
            client,
            AuthorizationParams(
                state=None,
                scopes=["sales:read"],
                code_challenge="challenge",
                redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/test"),
                redirect_uri_provided_explicitly=True,
                resource="https://mcp.sales.example/mcp",
            ),
        )
    )
    request_id = parse_qs(urlsplit(authorize_url).query)["request_id"][0]
    callback_url = provider.complete_authorization(
        request_id,
        subject="auth0|beta-user",
        approved=True,
    )
    assert callback_url is not None
    code_value = parse_qs(urlsplit(callback_url).query)["code"][0]
    code = asyncio.run(provider.load_authorization_code(client, code_value))
    assert code is not None
    tokens = asyncio.run(provider.exchange_authorization_code(client, code))
    assert tokens.refresh_token is not None

    database_bytes = (tmp_path / "oauth.db").read_bytes()
    for secret_value in (
        "client-secret-value",
        code_value,
        tokens.access_token,
        tokens.refresh_token,
    ):
        assert secret_value.encode("utf-8") not in database_bytes

    with sqlite3.connect(tmp_path / "oauth.db") as connection:
        digest = connection.execute(
            "SELECT token_digest FROM oauth_access_tokens"
        ).fetchone()[0]
    assert digest != tokens.access_token
    assert digest == provider._digest(tokens.access_token)

    other_directory = tmp_path / "other"
    other_directory.mkdir()
    other_provider = _provider(other_directory, secret="x" * 48)
    assert other_provider._digest(tokens.access_token) != digest


def test_oauth_registration_rejects_unapproved_redirect_hosts(tmp_path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(RegistrationError) as error:
        asyncio.run(
            provider.register_client(
                _client(redirect_uri="https://evil.example/callback")
            )
        )
    assert error.value.error == "invalid_redirect_uri"
    assert asyncio.run(provider.get_client("chatgpt-client")) is None


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://127.0.0.1:49152/callback/codex-test",
        "http://[::1]:49152/callback/codex-test",
    ],
)
def test_oauth_registration_accepts_codex_loopback_redirects(
    tmp_path, redirect_uri: str
) -> None:
    provider = _provider(tmp_path)
    asyncio.run(provider.register_client(_client(redirect_uri=redirect_uri)))

    registered = asyncio.run(provider.get_client("chatgpt-client"))
    assert registered is not None
    assert [str(uri) for uri in registered.redirect_uris or []] == [redirect_uri]


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://localhost:49152/callback/codex-test",
        "http://evil.example/callback",
        "http://127.0.0.1:49152/callback#fragment",
    ],
)
def test_oauth_registration_rejects_unsafe_http_redirects(
    tmp_path, redirect_uri: str
) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(RegistrationError) as error:
        asyncio.run(provider.register_client(_client(redirect_uri=redirect_uri)))
    assert error.value.error == "invalid_redirect_uri"


def test_oauth_storage_secret_must_be_long_enough(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        _provider(tmp_path, secret="short")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "/oauth/authorize?request_id=safe-request",
            "/oauth/authorize?request_id=safe-request",
        ),
        ("https://evil.example/oauth/authorize?request_id=x", None),
        ("//evil.example/oauth/authorize?request_id=x", None),
        ("/admin", None),
        ("/oauth/authorize", None),
    ],
)
def test_safe_post_login_redirect(value: str, expected: str | None) -> None:
    assert safe_post_login_redirect(value) == expected
