from __future__ import annotations

import base64
import hmac
import html
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac as cryptography_hmac
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

POST_LOGIN_REDIRECT_KEY = "post_login_redirect"
_REQUEST_ID_MAX_LENGTH = 256
_FORM_BODY_MAX_BYTES = 16_384


class OllumRefreshToken(RefreshToken):
    resource: str | None = None
    family_id: str


class OllumAccessToken(AccessToken):
    family_id: str


class PersistentOAuthProvider(
    OAuthAuthorizationServerProvider[
        AuthorizationCode,
        OllumRefreshToken,
        OllumAccessToken,
    ]
):
    """Persistent OAuth 2.1 provider for ChatGPT MCP connections.

    Client secrets are encrypted at rest. Authorization codes, access tokens and
    refresh tokens are stored only as keyed HMAC digests, so a CRM database copy
    is not sufficient to impersonate a connected client.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        dashboard_base_url: str,
        resource_url: str,
        storage_secret: str,
        allowed_redirect_hosts: tuple[str, ...] = ("chatgpt.com",),
        access_token_ttl_seconds: int = 3600,
        refresh_token_ttl_seconds: int = 2_592_000,
        authorization_code_ttl_seconds: int = 300,
    ) -> None:
        if len(storage_secret.encode("utf-8")) < 32:
            raise ValueError(
                "OLLUM_OAUTH_STORAGE_SECRET must contain at least 32 bytes"
            )
        parsed_dashboard = urlsplit(dashboard_base_url)
        parsed_resource = urlsplit(resource_url)
        if parsed_dashboard.scheme != "https" or not parsed_dashboard.netloc:
            raise ValueError(
                "OAuth dashboard base URL must be an absolute HTTPS origin"
            )
        if parsed_resource.scheme != "https" or not parsed_resource.netloc:
            raise ValueError("OAuth resource URL must be an absolute HTTPS URL")
        normalized_hosts = tuple(
            sorted(
                {
                    host.strip().lower()
                    for host in allowed_redirect_hosts
                    if host.strip()
                }
            )
        )
        if not normalized_hosts:
            raise ValueError("At least one OAuth redirect host must be allowed")
        if (
            min(
                access_token_ttl_seconds,
                refresh_token_ttl_seconds,
                authorization_code_ttl_seconds,
            )
            <= 0
        ):
            raise ValueError("OAuth token lifetimes must be positive")

        self.db_path = str(db_path)
        self.dashboard_base_url = dashboard_base_url.rstrip("/")
        self.resource_url = resource_url
        self.allowed_redirect_hosts = normalized_hosts
        self.access_token_ttl_seconds = access_token_ttl_seconds
        self.refresh_token_ttl_seconds = refresh_token_ttl_seconds
        self.authorization_code_ttl_seconds = authorization_code_ttl_seconds
        key_material = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=b"ollum-sales-oauth-storage-v1",
            info=b"persistent-oauth-provider",
        ).derive(storage_secret.encode("utf-8"))
        encryption_key = base64.urlsafe_b64encode(key_material[:32])
        self._digest_key = key_material[32:]
        self._fernet = Fernet(encryption_key)
        self._lock = threading.RLock()
        self._initialize()

    def _digest(self, value: str) -> str:
        digest = cryptography_hmac.HMAC(self._digest_key, hashes.SHA256())
        digest.update(value.encode("utf-8"))
        return digest.finalize().hex()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    encrypted_payload BLOB NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_pending_authorizations (
                    request_digest TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
                    code_digest TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_explicit INTEGER NOT NULL,
                    resource TEXT,
                    subject TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                    token_digest TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT,
                    subject TEXT NOT NULL,
                    family_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_oauth_access_family
                    ON oauth_access_tokens(family_id);
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token_digest TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT,
                    subject TEXT NOT NULL,
                    family_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_oauth_refresh_family
                    ON oauth_refresh_tokens(family_id);
                """
            )

    @staticmethod
    def _json_list(value: str) -> list[str]:
        loaded = json.loads(value)
        if not isinstance(loaded, list):
            return []
        return [str(item) for item in loaded]

    def _cleanup_expired(self, connection: sqlite3.Connection, now: int) -> None:
        connection.execute(
            "DELETE FROM oauth_pending_authorizations WHERE expires_at <= ?", (now,)
        )
        connection.execute(
            "DELETE FROM oauth_authorization_codes WHERE expires_at <= ?", (now,)
        )
        connection.execute(
            "DELETE FROM oauth_access_tokens WHERE expires_at <= ?", (now,)
        )
        connection.execute(
            "DELETE FROM oauth_refresh_tokens WHERE expires_at <= ?", (now,)
        )

    def _validate_redirect_uris(self, client_info: OAuthClientInformationFull) -> None:
        uris = client_info.redirect_uris or []
        for uri in uris:
            parsed = urlsplit(str(uri))
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or host not in self.allowed_redirect_hosts:
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Only approved HTTPS ChatGPT callback hosts may be registered",
                )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id or len(client_id) > 256:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted_payload FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = self._fernet.decrypt(bytes(row["encrypted_payload"]))
            return OAuthClientInformationFull.model_validate_json(payload)
        except (InvalidToken, ValueError):
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        self._validate_redirect_uris(client_info)
        encrypted = self._fernet.encrypt(client_info.model_dump_json().encode("utf-8"))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO oauth_clients(client_id, encrypted_payload, created_at)
                VALUES (?, ?, ?)
                """,
                (client_info.client_id, encrypted, int(time.time())),
            )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if not client.client_id:
            raise ValueError("Registered OAuth client has no client_id")
        request_id = secrets.token_urlsafe(32)
        now = int(time.time())
        payload = {
            "state": params.state,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }
        with self._lock, self._connect() as connection:
            self._cleanup_expired(connection, now)
            connection.execute(
                """
                INSERT INTO oauth_pending_authorizations(
                    request_digest, client_id, params_json, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    self._digest(request_id),
                    client.client_id,
                    json.dumps(payload, separators=(",", ":")),
                    now + self.authorization_code_ttl_seconds,
                ),
            )
        return f"{self.dashboard_base_url}/oauth/authorize?{urlencode({'request_id': request_id})}"

    def pending_authorization(self, request_id: str) -> dict[str, Any] | None:
        if not request_id or len(request_id) > _REQUEST_ID_MAX_LENGTH:
            return None
        now = int(time.time())
        with self._lock, self._connect() as connection:
            self._cleanup_expired(connection, now)
            row = connection.execute(
                """
                SELECT client_id, params_json, expires_at
                FROM oauth_pending_authorizations
                WHERE request_digest = ?
                """,
                (self._digest(request_id),),
            ).fetchone()
        if row is None:
            return None
        params = json.loads(str(row["params_json"]))
        return {
            "client_id": str(row["client_id"]),
            "scopes": [str(item) for item in params.get("scopes") or []],
            "expires_at": int(row["expires_at"]),
        }

    def complete_authorization(
        self,
        request_id: str,
        *,
        subject: str,
        approved: bool,
    ) -> str | None:
        if not request_id or len(request_id) > _REQUEST_ID_MAX_LENGTH or not subject:
            return None
        request_digest = self._digest(request_id)
        now = int(time.time())
        with self._lock, self._connect() as connection:
            self._cleanup_expired(connection, now)
            row = connection.execute(
                """
                SELECT client_id, params_json
                FROM oauth_pending_authorizations
                WHERE request_digest = ?
                """,
                (request_digest,),
            ).fetchone()
            if row is None:
                return None
            params = json.loads(str(row["params_json"]))
            redirect_uri = str(params["redirect_uri"])
            state = params.get("state")
            if not approved:
                connection.execute(
                    "DELETE FROM oauth_pending_authorizations WHERE request_digest = ?",
                    (request_digest,),
                )
                return construct_redirect_uri(
                    redirect_uri,
                    error="access_denied",
                    state=state,
                )

            code = secrets.token_urlsafe(32)
            connection.execute(
                """
                INSERT INTO oauth_authorization_codes(
                    code_digest, client_id, scopes_json, expires_at,
                    code_challenge, redirect_uri, redirect_uri_explicit,
                    resource, subject
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._digest(code),
                    str(row["client_id"]),
                    json.dumps(params.get("scopes") or []),
                    now + self.authorization_code_ttl_seconds,
                    str(params["code_challenge"]),
                    redirect_uri,
                    1 if params.get("redirect_uri_provided_explicitly") else 0,
                    params.get("resource"),
                    subject,
                ),
            )
            connection.execute(
                "DELETE FROM oauth_pending_authorizations WHERE request_digest = ?",
                (request_digest,),
            )
        return construct_redirect_uri(redirect_uri, code=code, state=state)

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        if not client.client_id or not authorization_code:
            return None
        now = int(time.time())
        with self._lock, self._connect() as connection:
            self._cleanup_expired(connection, now)
            row = connection.execute(
                """
                SELECT * FROM oauth_authorization_codes
                WHERE code_digest = ? AND client_id = ?
                """,
                (self._digest(authorization_code), client.client_id),
            ).fetchone()
        if row is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=str(row["client_id"]),
            scopes=self._json_list(str(row["scopes_json"])),
            expires_at=float(row["expires_at"]),
            code_challenge=str(row["code_challenge"]),
            redirect_uri=AnyUrl(str(row["redirect_uri"])),
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=str(row["resource"]) if row["resource"] is not None else None,
            subject=str(row["subject"]),
        )

    def _insert_token_pair(
        self,
        connection: sqlite3.Connection,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        subject: str,
        family_id: str | None = None,
    ) -> OAuthToken:
        now = int(time.time())
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        family = family_id or secrets.token_urlsafe(24)
        connection.execute(
            """
            INSERT INTO oauth_access_tokens(
                token_digest, client_id, scopes_json, expires_at,
                resource, subject, family_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._digest(access_token),
                client_id,
                json.dumps(scopes),
                now + self.access_token_ttl_seconds,
                resource,
                subject,
                family,
            ),
        )
        connection.execute(
            """
            INSERT INTO oauth_refresh_tokens(
                token_digest, client_id, scopes_json, expires_at,
                resource, subject, family_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._digest(refresh_token),
                client_id,
                json.dumps(scopes),
                now + self.refresh_token_ttl_seconds,
                resource,
                subject,
                family,
            ),
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self.access_token_ttl_seconds,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        if not client.client_id or not authorization_code.subject:
            raise TokenError("invalid_grant", "Authorization code is incomplete")
        with self._lock, self._connect() as connection:
            deleted = connection.execute(
                """
                DELETE FROM oauth_authorization_codes
                WHERE code_digest = ? AND client_id = ? AND expires_at > ?
                """,
                (
                    self._digest(authorization_code.code),
                    client.client_id,
                    int(time.time()),
                ),
            )
            if deleted.rowcount != 1:
                raise TokenError("invalid_grant", "Authorization code was already used")
            return self._insert_token_pair(
                connection,
                client_id=client.client_id,
                scopes=authorization_code.scopes,
                resource=authorization_code.resource or self.resource_url,
                subject=authorization_code.subject,
            )

    async def load_access_token(self, token: str) -> OllumAccessToken | None:
        if not token:
            return None
        now = int(time.time())
        with self._lock, self._connect() as connection:
            self._cleanup_expired(connection, now)
            row = connection.execute(
                "SELECT * FROM oauth_access_tokens WHERE token_digest = ?",
                (self._digest(token),),
            ).fetchone()
        if row is None:
            return None
        return OllumAccessToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=self._json_list(str(row["scopes_json"])),
            expires_at=int(row["expires_at"]),
            resource=str(row["resource"]) if row["resource"] is not None else None,
            subject=str(row["subject"]),
            family_id=str(row["family_id"]),
            claims={"iss": self.dashboard_base_url, "sub": str(row["subject"])},
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> OllumRefreshToken | None:
        if not client.client_id or not refresh_token:
            return None
        now = int(time.time())
        with self._lock, self._connect() as connection:
            self._cleanup_expired(connection, now)
            row = connection.execute(
                """
                SELECT * FROM oauth_refresh_tokens
                WHERE token_digest = ? AND client_id = ?
                """,
                (self._digest(refresh_token), client.client_id),
            ).fetchone()
        if row is None:
            return None
        return OllumRefreshToken(
            token=refresh_token,
            client_id=str(row["client_id"]),
            scopes=self._json_list(str(row["scopes_json"])),
            expires_at=int(row["expires_at"]),
            subject=str(row["subject"]),
            resource=str(row["resource"]) if row["resource"] is not None else None,
            family_id=str(row["family_id"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: OllumRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not client.client_id or not refresh_token.subject:
            raise TokenError("invalid_grant", "Refresh token is incomplete")
        requested_scopes = scopes or refresh_token.scopes
        if not set(requested_scopes).issubset(set(refresh_token.scopes)):
            raise TokenError("invalid_scope", "Refresh cannot expand granted scopes")
        with self._lock, self._connect() as connection:
            deleted = connection.execute(
                """
                DELETE FROM oauth_refresh_tokens
                WHERE token_digest = ? AND client_id = ? AND expires_at > ?
                """,
                (
                    self._digest(refresh_token.token),
                    client.client_id,
                    int(time.time()),
                ),
            )
            if deleted.rowcount != 1:
                raise TokenError("invalid_grant", "Refresh token was already used")
            connection.execute(
                "DELETE FROM oauth_access_tokens WHERE family_id = ?",
                (refresh_token.family_id,),
            )
            return self._insert_token_pair(
                connection,
                client_id=client.client_id,
                scopes=requested_scopes,
                resource=refresh_token.resource or self.resource_url,
                subject=refresh_token.subject,
                family_id=refresh_token.family_id,
            )

    async def revoke_token(
        self,
        token: OllumAccessToken | OllumRefreshToken,
    ) -> None:
        family_id = getattr(token, "family_id", "")
        with self._lock, self._connect() as connection:
            if not family_id:
                table = (
                    "oauth_refresh_tokens"
                    if isinstance(token, RefreshToken)
                    else "oauth_access_tokens"
                )
                row = connection.execute(
                    f"SELECT family_id FROM {table} WHERE token_digest = ?",
                    (self._digest(token.token),),
                ).fetchone()
                family_id = str(row["family_id"]) if row is not None else ""
            if family_id:
                connection.execute(
                    "DELETE FROM oauth_access_tokens WHERE family_id = ?", (family_id,)
                )
                connection.execute(
                    "DELETE FROM oauth_refresh_tokens WHERE family_id = ?", (family_id,)
                )


def safe_post_login_redirect(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path != "/oauth/authorize":
        return None
    request_id = parse_qs(parsed.query).get("request_id", [""])[0]
    if not request_id or len(request_id) > _REQUEST_ID_MAX_LENGTH:
        return None
    return value


def _session_user(request: Request) -> dict[str, Any] | None:
    try:
        user = request.session.get("user")
    except AssertionError:
        return None
    if not isinstance(user, dict):
        return None
    if int(user.get("expires_at") or 0) <= int(time.time()):
        request.session.clear()
        return None
    if not user.get("sub") or not user.get("workspace_id") or not user.get("csrf"):
        return None
    return user


def _oauth_provider(request: Request) -> PersistentOAuthProvider:
    provider = getattr(request.app.state, "oauth_provider", None)
    if provider is None:
        raise RuntimeError("OAuth provider is unavailable")
    if not isinstance(provider, PersistentOAuthProvider):
        raise TypeError("OAuth provider has an invalid type")
    return provider


async def oauth_authorize_page(request: Request) -> Response:
    request_id = str(request.query_params.get("request_id") or "")
    provider = _oauth_provider(request)
    pending = provider.pending_authorization(request_id)
    if pending is None:
        return JSONResponse(
            {"error": "The OAuth authorization request expired or is invalid"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    user = _session_user(request)
    if user is None:
        next_path = f"/oauth/authorize?{urlencode({'request_id': request_id})}"
        request.session[POST_LOGIN_REDIRECT_KEY] = next_path
        return RedirectResponse("/auth/login", status_code=303)
    client = await provider.get_client(str(pending["client_id"]))
    if client is None:
        return JSONResponse(
            {"error": "The OAuth client is no longer registered"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    client_name = html.escape(str(client.client_name or "ChatGPT"))
    scopes = "".join(
        f"<li>{html.escape(scope)}</li>" for scope in pending.get("scopes") or []
    )
    document = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Подключение Ollum Sales</title>
  <link rel="stylesheet" href="/assets/oauth.css">
</head>
<body>
  <main class="oauth-shell">
    <section class="oauth-card" aria-labelledby="oauth-title">
      <div class="oauth-mark" aria-hidden="true">O</div>
      <p class="oauth-kicker">OLLUM GROUP · ЗАКРЫТАЯ БЕТА</p>
      <h1 id="oauth-title">Подключить {client_name}</h1>
      <p class="oauth-copy">ChatGPT получит доступ к рабочему пространству Ollum Sales от имени текущего пользователя.</p>
      <div class="oauth-scope-box">
        <span>Разрешения</span>
        <ul>{scopes}</ul>
      </div>
      <p class="oauth-safety">SAFE остаётся включён: подключение не разрешает автоматическую отправку WhatsApp.</p>
      <form method="post" action="/oauth/authorize/complete">
        <input type="hidden" name="request_id" value="{html.escape(request_id)}">
        <input type="hidden" name="csrf" value="{html.escape(str(user["csrf"]))}">
        <button class="oauth-primary" type="submit" name="decision" value="allow">Разрешить подключение</button>
        <button class="oauth-secondary" type="submit" name="decision" value="deny">Отмена</button>
      </form>
    </section>
  </main>
</body>
</html>"""
    return HTMLResponse(document, headers={"Cache-Control": "no-store"})


async def oauth_authorize_complete(request: Request) -> Response:
    body = await request.body()
    if len(body) > _FORM_BODY_MAX_BYTES:
        return JSONResponse({"error": "Request is too large"}, status_code=413)
    form = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True)
    request_id = str(form.get("request_id", [""])[0])
    decision = str(form.get("decision", [""])[0])
    csrf = str(form.get("csrf", [""])[0])
    user = _session_user(request)
    if user is None:
        return JSONResponse({"error": "Authentication is required"}, status_code=401)
    expected_csrf = str(user.get("csrf") or "")
    if not expected_csrf or not hmac.compare_digest(expected_csrf, csrf):
        return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)
    if decision not in {"allow", "deny"}:
        return JSONResponse({"error": "Invalid OAuth decision"}, status_code=400)
    provider = _oauth_provider(request)
    redirect_url = provider.complete_authorization(
        request_id,
        subject=str(user["sub"]),
        approved=decision == "allow",
    )
    if redirect_url is None:
        return JSONResponse(
            {"error": "The OAuth authorization request expired or was already used"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    context = getattr(request.app.state, "admin_context", None)
    if context is not None:
        context.crm.record_admin_audit(
            actor=str(user.get("email") or user["sub"]),
            action="oauth.consent",
            outcome="approved" if decision == "allow" else "denied",
            details={"client": "ChatGPT MCP"},
        )
    safe_redirect_url = html.escape(redirect_url, quote=True)
    approved = decision == "allow"
    title = "Подключение одобрено" if approved else "Подключение отклонено"
    copy = (
        "Разрешение сохранено. Завершите подключение в ChatGPT."
        if approved
        else "Подключение не было разрешено. Вернитесь в ChatGPT."
    )
    document = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Ollum Sales</title>
  <link rel="stylesheet" href="/admin-assets/oauth.css">
</head>
<body>
  <main class="oauth-shell">
    <section class="oauth-card" aria-labelledby="oauth-handoff-title">
      <div class="oauth-mark" aria-hidden="true">O</div>
      <p class="oauth-kicker">OLLUM SALES / OAUTH</p>
      <h1 id="oauth-handoff-title">{title}</h1>
      <p class="oauth-copy">{copy}</p>
      <a class="oauth-action oauth-primary" href="{safe_redirect_url}"
         rel="noreferrer noopener">Вернуться в ChatGPT</a>
      <p class="oauth-safety">Переход выполняется только после вашего нажатия.</p>
    </section>
  </main>
</body>
</html>"""
    return HTMLResponse(
        document,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'"
            ),
            "Referrer-Policy": "no-referrer",
        },
    )


def create_oauth_consent_routes() -> list[Route]:
    return [
        Route("/oauth/authorize", oauth_authorize_page, methods=["GET"]),
        Route("/oauth/authorize/complete", oauth_authorize_complete, methods=["POST"]),
    ]
