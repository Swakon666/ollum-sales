from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import app.admin as admin_module
from app.admin import (
    AdminContext,
    AdminJobRegistry,
    SecurityHeadersMiddleware,
    create_admin_routes,
)
from app.config import settings
from app.crm import SalesCRM
from app.oauth_server import PersistentOAuthProvider, create_oauth_consent_routes


class FakeAutopilot:
    def __init__(self) -> None:
        self.running = False
        self.mode = "safe"
        self.start_calls: list[dict[str, Any]] = []
        self.run_calls = 0

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "mode": self.mode,
            "interval_minutes": 60,
            "score_threshold": 65,
            "max_verticals_per_cycle": 2,
            "leads_per_vertical": 10,
            "pending_send_requests": 0,
        }

    def start(self, **kwargs: Any) -> dict[str, Any]:
        self.start_calls.append(kwargs)
        self.running = True
        self.mode = str(kwargs["mode"])
        return {"success": True, **self.status()}

    def stop(self) -> dict[str, Any]:
        self.running = False
        return {"success": True, **self.status()}

    def run_cycle(self, *, force: bool = False) -> dict[str, Any]:
        self.run_calls += 1
        return {
            "success": True,
            "force": force,
            "mode": "safe",
            "leads_discovered": 0,
            "drafts_created": 0,
        }


class FakeSheets:
    def __init__(self) -> None:
        self.sync_calls = 0

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "configured": True, "last_sync_status": "success"}

    def sync(self) -> dict[str, Any]:
        self.sync_calls += 1
        return {"success": True, "sheets_updated": 6}


class FakeConversationAgent:
    def __init__(self, crm: SalesCRM) -> None:
        self.crm = crm

    def status(self, workspace_id: str) -> dict[str, Any]:
        summary = self.crm.conversation_agent_summary(workspace_id)
        return {
            "settings": summary.pop("settings"),
            "summary": summary,
            "runtime": {
                "enabled": True,
                "ready": True,
                "execution_mode": "chatgpt_mcp",
                "brain": "ChatGPT through Ollum Sales MCP",
                "server_llm_enabled": False,
                "requires_api_key": False,
                "company_ready": True,
            },
            "safety": {
                "approves": False,
                "sends": False,
                "external_send": False,
                "draft_only": True,
            },
        }


class FakeSessions:
    def __init__(self) -> None:
        self.handoffs: dict[str, dict[str, Any]] = {}

    async def begin_login(self, _request: Request):
        return JSONResponse({"login": True})

    async def complete_login(self, _request: Request):
        return {
            "sub": "auth0|beta-user",
            "email": "operator@example.com",
            "name": "Beta Operator",
            "scopes": ["sales:read", "sales:write"],
            "csrf": "callback-csrf-token",
            "expires_at": int(time.time()) + 600,
            "workspace_id": "ollum-group",
            "workspace_name": "Ollum Group",
            "member_id": "test-member",
            "role": "owner",
        }

    @staticmethod
    def login_must_start_on_redirect_host(request: Request) -> bool:
        return request.url.hostname != "mcp.sales.example"

    @staticmethod
    def login_start_url() -> str:
        return "https://mcp.sales.example/auth/login"

    @staticmethod
    def dashboard_url(path: str) -> str:
        return f"https://api.sales.example/{path.lstrip('/')}"

    @property
    def uses_cross_origin_handoff(self) -> bool:
        return True

    def issue_login_handoff(self, user: dict[str, Any]) -> str:
        self.handoffs["single-use-code"] = dict(user)
        return "single-use-code"

    def consume_login_handoff(self, code: str) -> dict[str, Any] | None:
        return self.handoffs.pop(code, None)

    @staticmethod
    def logout(request: Request) -> None:
        request.session.clear()


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    crm = SalesCRM(tmp_path / "admin.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    autopilot = FakeAutopilot()
    sheets = FakeSheets()
    beta_settings = replace(
        settings,
        auth_mode="oidc",
        public_base_url="https://sales.example",
        dashboard_base_url="https://api.sales.example",
        oidc_redirect_base_url="https://mcp.sales.example",
        mcp_resource_url="https://sales.example/mcp",
        oidc_issuer_url="https://identity.example/",
        oidc_audience="https://api.example",
        admin_enabled=True,
        admin_allowed_emails=("operator@example.com",),
        admin_read_scope="sales:read",
        admin_write_scope="sales:write",
        allow_whatsapp_send=False,
        allow_autopilot_send=False,
    )
    context = AdminContext(
        crm=crm,
        autopilot=autopilot,  # type: ignore[arg-type]
        sheets=sheets,  # type: ignore[arg-type]
        settings=beta_settings,
        sessions=FakeSessions(),  # type: ignore[arg-type]
        jobs=AdminJobRegistry(),
        conversation_agent=FakeConversationAgent(crm),  # type: ignore[arg-type]
    )
    oauth_provider = PersistentOAuthProvider(
        db_path=tmp_path / "admin.db",
        dashboard_base_url="https://api.sales.example",
        resource_url="https://sales.example/mcp",
        storage_secret="test-oauth-storage-secret-with-more-than-32-bytes",
        allowed_redirect_hosts=("chatgpt.com",),
    )

    async def test_login(request: Request) -> JSONResponse:
        request.session["user"] = {
            "sub": "auth0|beta-user",
            "email": "operator@example.com",
            "name": "Beta Operator",
            "scopes": ["sales:read", "sales:write"],
            "csrf": "test-csrf-token",
            "expires_at": int(time.time()) + 600,
            "workspace_id": "ollum-group",
            "workspace_name": "Ollum Group",
            "member_id": "test-member",
            "role": "owner",
        }
        return JSONResponse({"ok": True})

    async def test_expired_login(request: Request) -> JSONResponse:
        request.session["user"] = {
            "sub": "auth0|beta-user",
            "email": "operator@example.com",
            "name": "Beta Operator",
            "scopes": ["sales:read", "sales:write"],
            "csrf": "expired-csrf-token",
            "expires_at": int(time.time()) - 1,
            "workspace_id": "ollum-group",
            "workspace_name": "Ollum Group",
            "member_id": "test-member",
            "role": "owner",
        }
        return JSONResponse({"ok": True})

    async def test_viewer_login(request: Request) -> JSONResponse:
        request.session["user"] = {
            "sub": "auth0|viewer",
            "email": "viewer@example.com",
            "name": "Beta Viewer",
            "scopes": ["sales:read", "sales:write"],
            "csrf": "test-csrf-token",
            "expires_at": int(time.time()) + 600,
            "workspace_id": "ollum-group",
            "workspace_name": "Ollum Group",
            "member_id": "viewer-member",
            "role": "viewer",
        }
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/__test/login", test_login),
            Route("/__test/login-expired", test_expired_login),
            Route("/__test/login-viewer", test_viewer_login),
            *create_admin_routes(context),
            *create_oauth_consent_routes(),
        ]
    )
    app.state.admin_context = context
    app.state.oauth_provider = oauth_provider
    app.add_middleware(
        SessionMiddleware,
        secret_key="test-session-secret-with-32-bytes",
        session_cookie="ollum_admin_test",
        same_site="lax",
        https_only=False,
    )
    secured = SecurityHeadersMiddleware(app)
    monkeypatch.setattr(
        admin_module,
        "bridge_status",
        lambda: {
            "reachable": True,
            "ready": True,
            "connected": True,
            "logged_in": True,
            "send_enabled": False,
        },
    )
    monkeypatch.setattr(
        admin_module,
        "bridge_pairing_status",
        lambda: {
            "reachable": True,
            "state": "not_required",
            "needs_pairing": False,
            "has_qr": False,
        },
    )
    with TestClient(secured, base_url="https://sales.example") as client:
        yield client, context, autopilot, sheets


def _login(client: TestClient) -> None:
    assert client.get("/__test/login").status_code == 200


def _csrf_headers() -> dict[str, str]:
    return {"X-CSRF-Token": "test-csrf-token"}


def _pending_oauth_request(provider: PersistentOAuthProvider) -> str:
    client = OAuthClientInformationFull(
        client_id="chatgpt-test-client",
        client_secret="chatgpt-test-secret",
        redirect_uris=[AnyUrl("https://chatgpt.com/connector/oauth/test")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="sales:read sales:write",
        client_name="Ollum Sales Test",
    )
    asyncio.run(provider.register_client(client))
    authorization_url = asyncio.run(
        provider.authorize(
            client,
            AuthorizationParams(
                state="test-state",
                scopes=["sales:read", "sales:write"],
                code_challenge="pkce-challenge",
                redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/test"),
                redirect_uri_provided_explicitly=True,
                resource="https://sales.example/mcp",
            ),
        )
    )
    return parse_qs(urlsplit(authorization_url).query)["request_id"][0]


def _oauth_handoff_url(document: str) -> str:
    match = re.search(
        r'<a class="oauth-action oauth-primary" href="([^"]+)"[^>]*>'
        r"Вернуться в ChatGPT</a>",
        document,
    )
    assert match is not None
    return html.unescape(match.group(1))


def test_admin_requires_session_and_sets_security_headers(admin_client) -> None:
    client, _context, _autopilot, _sheets = admin_client

    assert client.get("/api/admin/bootstrap").status_code == 401
    redirect = client.get("/admin", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/auth/login"

    _login(client)
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Ollum Control" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["strict-transport-security"].startswith("max-age=")
    assert page.headers["cache-control"] == "no-store"


def test_admin_assets_keep_large_lists_bounded_and_keyboard_accessible(
    admin_client,
) -> None:
    client, _context, _autopilot, _sheets = admin_client
    _login(client)

    page = client.get("/admin")
    script = client.get("/assets/admin.js")
    stylesheet = client.get("/assets/admin.css")
    display_font = client.get("/assets/fonts/bricolage-latin.woff2")
    favicon = client.get("/assets/favicon.svg")

    assert (
        page.status_code
        == script.status_code
        == stylesheet.status_code
        == display_font.status_code
        == favicon.status_code
        == 200
    )
    assert display_font.headers["content-type"] == "font/woff2"
    assert favicon.headers["content-type"] == "image/svg+xml"
    assert 'rel="icon" href="/assets/favicon.svg"' in page.text
    assert 'class="skip-link"' in page.text
    assert 'id="main-content" tabindex="-1"' in page.text
    assert 'id="mobile-nav-toggle"' in page.text
    assert 'aria-controls="primary-navigation"' in page.text
    assert 'rel="preload" href="/assets/fonts/bricolage-latin.woff2"' in page.text
    assert 'class="nav-item is-active" href="#overview"' in page.text
    for pagination_id in (
        "leads-pagination",
        "campaigns-pagination",
        "drafts-pagination",
        "audit-pagination",
    ):
        assert f'id="{pagination_id}"' in page.text
    assert "const PAGE_SIZE = 50;" in script.text
    assert "function pageItems(" in script.text
    assert "function renderView(" in script.text
    assert "function setMobileNavigation(" in script.text
    assert "workspace.inert = expanded" in script.text
    assert 'event.key === "Escape"' in script.text
    assert "aria-current" in script.text
    assert 'window.scrollTo({ top: 0, left: 0, behavior: "auto" })' in script.text
    assert ":focus-visible" in stylesheet.text
    assert "--paper: #f2f3ef" in stylesheet.text
    assert "--accent: #12cc77" in stylesheet.text
    assert "--accent-soft: #e5f8ef" in stylesheet.text
    assert 'href="#agent" data-view="agent"' in page.text
    assert 'id="conversation-agent-form"' in page.text
    assert 'name="response_sla_minutes"' in page.text
    assert 'id="agent-sessions-table"' in page.text
    assert "function renderConversationAgent(" in script.text
    assert "function retryInboxEvent(" in script.text
    assert "transition: all" not in stylesheet.text
    assert "background-image: none" in stylesheet.text
    assert "env(safe-area-inset-top)" in stylesheet.text
    assert "overscroll-behavior: contain" in stylesheet.text
    assert "outline: none" not in stylesheet.text


def test_local_admin_preview_covers_current_agent_dashboard_contract() -> None:
    from scripts.preview_admin import app as preview_app

    with TestClient(preview_app) as client:
        bootstrap = client.get("/api/v1/bootstrap")
        sessions = client.get("/api/v1/conversation-agent/sessions?limit=200")
        inbox = client.get("/api/v1/agent/inbox?limit=200")

    assert bootstrap.status_code == 200
    assert (
        bootstrap.json()["conversation_agent"]["settings"]["response_sla_minutes"] == 60
    )
    assert bootstrap.json()["agent_inbox"]["sla_overdue"] == 1
    assert sessions.status_code == 200
    assert len(sessions.json()) == 1
    assert inbox.status_code == 200
    assert any(item["retryable"] for item in inbox.json())


def test_cross_origin_oauth_handoff_finishes_on_api_and_is_single_use(
    admin_client,
) -> None:
    client, _context, _autopilot, _sheets = admin_client

    login = client.get("https://api.sales.example/auth/login", follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "https://mcp.sales.example/auth/login"

    callback = client.get(
        "https://mcp.sales.example/auth/callback", follow_redirects=False
    )
    assert callback.status_code == 303
    handoff_url = callback.headers["location"]
    assert handoff_url == (
        "https://api.sales.example/auth/handoff?code=single-use-code"
    )
    assert "operator@example.com" not in handoff_url

    handoff = client.get(handoff_url, follow_redirects=False)
    assert handoff.status_code == 303
    assert handoff.headers["location"] == "https://api.sales.example/admin"
    assert client.get("https://api.sales.example/api/v1/session").status_code == 200

    replay = client.get(handoff_url, follow_redirects=False)
    assert replay.status_code == 403


def test_chatgpt_oauth_consent_survives_cross_origin_dashboard_login(
    admin_client,
) -> None:
    client, _context, _autopilot, _sheets = admin_client
    provider = client.app.app.state.oauth_provider
    request_id = _pending_oauth_request(provider)
    consent_path = f"/oauth/authorize?request_id={request_id}"

    consent = client.get(
        f"https://api.sales.example{consent_path}", follow_redirects=False
    )
    assert consent.status_code == 303
    assert consent.headers["location"] == "/auth/login"

    login = client.get("https://api.sales.example/auth/login", follow_redirects=False)
    assert login.headers["location"] == "https://mcp.sales.example/auth/login"
    callback = client.get(
        "https://mcp.sales.example/auth/callback", follow_redirects=False
    )
    handoff = client.get(callback.headers["location"], follow_redirects=False)
    assert handoff.status_code == 303
    assert handoff.headers["location"] == (f"https://api.sales.example{consent_path}")

    page = client.get(handoff.headers["location"])
    assert page.status_code == 200
    assert "Подключить Ollum Sales Test" in page.text
    assert "sales:read" in page.text
    assert "автоматическую отправку WhatsApp" in page.text
    assert page.headers["cache-control"] == "no-store"


def test_chatgpt_oauth_consent_requires_csrf_and_issues_single_use_code(
    admin_client,
) -> None:
    client, _context, _autopilot, _sheets = admin_client
    provider = client.app.app.state.oauth_provider
    request_id = _pending_oauth_request(provider)
    _login(client)

    rejected = client.post(
        "/oauth/authorize/complete",
        data={
            "request_id": request_id,
            "csrf": "wrong",
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 403

    approved = client.post(
        "/oauth/authorize/complete",
        data={
            "request_id": request_id,
            "csrf": "test-csrf-token",
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert approved.status_code == 200
    assert approved.headers["cache-control"] == "no-store"
    assert approved.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in approved.headers["content-security-policy"]
    assert "Подключение одобрено" in approved.text
    assert "Переход выполняется только после вашего нажатия" in approved.text
    assert "<script" not in approved.text
    callback = urlsplit(_oauth_handoff_url(approved.text))
    assert callback.scheme == "https"
    assert callback.netloc == "chatgpt.com"
    callback_params = parse_qs(callback.query)
    assert callback_params["state"] == ["test-state"]
    assert callback_params["code"]

    replay = client.post(
        "/oauth/authorize/complete",
        data={
            "request_id": request_id,
            "csrf": "test-csrf-token",
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert replay.status_code == 400


def test_chatgpt_oauth_denial_uses_explicit_safe_handoff(admin_client) -> None:
    client, _context, _autopilot, _sheets = admin_client
    provider = client.app.app.state.oauth_provider
    request_id = _pending_oauth_request(provider)
    _login(client)

    denied = client.post(
        "/oauth/authorize/complete",
        data={
            "request_id": request_id,
            "csrf": "test-csrf-token",
            "decision": "deny",
        },
        follow_redirects=False,
    )

    assert denied.status_code == 200
    assert "Подключение отклонено" in denied.text
    callback = urlsplit(_oauth_handoff_url(denied.text))
    callback_params = parse_qs(callback.query)
    assert callback.scheme == "https"
    assert callback.netloc == "chatgpt.com"
    assert callback_params == {
        "error": ["access_denied"],
        "state": ["test-state"],
    }


def test_expired_admin_session_is_rejected(admin_client) -> None:
    client, _context, _autopilot, _sheets = admin_client

    assert client.get("/__test/login-expired").status_code == 200
    assert client.get("/api/admin/bootstrap").status_code == 401


def test_bootstrap_reports_safe_guards_and_no_send_control(admin_client) -> None:
    client, _context, _autopilot, _sheets = admin_client
    _login(client)

    payload = client.get("/api/admin/bootstrap").json()

    assert payload["safety"] == {
        "safe_mode": True,
        "whatsapp_send_enabled": False,
        "autopilot_send_enabled": False,
        "send_controls_exposed": False,
    }
    assert payload["whatsapp"]["logged_in"] is True
    assert payload["plugin"]["server_url"] == "https://sales.example/mcp"
    assert payload["plugin"]["brain"] == "ChatGPT through Ollum Sales MCP"
    assert payload["plugin"]["server_llm_enabled"] is False
    assert payload["plugin"]["server_sync_interval_minutes"] == 15
    assert payload["plugin"]["recommended_chatgpt_schedule"] == "hourly_in_chat"
    assert (
        "sales_prepare_persisted_conversation" in payload["plugin"]["scheduled_prompt"]
    )


def test_write_routes_require_scope_and_csrf(admin_client) -> None:
    client, _context, autopilot, _sheets = admin_client
    _login(client)

    missing = client.post("/api/admin/autopilot/start", json={"mode": "safe"})
    assert missing.status_code == 403
    assert autopilot.start_calls == []

    unsafe = client.post(
        "/api/admin/autopilot/start",
        json={"mode": "active"},
        headers=_csrf_headers(),
    )
    assert unsafe.status_code == 409
    assert autopilot.start_calls == []

    safe = client.post(
        "/api/admin/autopilot/start",
        json={"mode": "safe"},
        headers=_csrf_headers(),
    )
    assert safe.status_code == 200
    assert autopilot.start_calls[0]["mode"] == "safe"


def test_workspace_viewer_cannot_mutate_even_with_write_scope(admin_client) -> None:
    client, _context, autopilot, _sheets = admin_client
    assert client.get("/__test/login-viewer").status_code == 200

    response = client.post(
        "/api/v1/autopilot/start",
        json={"mode": "safe"},
        headers=_csrf_headers(),
    )

    assert response.status_code == 403
    assert "operator role" in response.json()["error"]
    assert autopilot.start_calls == []


def test_exact_draft_approval_never_creates_a_send_request(admin_client) -> None:
    client, context, _autopilot, _sheets = admin_client
    lead = context.crm.upsert_lead("Example Co", "https://example.com")
    draft = context.crm.save_outreach_draft(
        lead["id"],
        channel="whatsapp",
        recipient="+971500000000",
        message="Здравствуйте! Нашли подтверждённую точку роста.",
    )
    _login(client)
    visible = client.get("/api/admin/drafts").json()[0]

    mismatch = client.post(
        f"/api/admin/drafts/{draft['id']}/approve",
        json={
            "fingerprint": visible["fingerprint"],
            "confirmation": "APPROVE",
            "recipient": draft["recipient"],
            "message": "Изменённый текст",
        },
        headers=_csrf_headers(),
    )
    assert mismatch.status_code == 409

    approved = client.post(
        f"/api/admin/drafts/{draft['id']}/approve",
        json={
            "fingerprint": visible["fingerprint"],
            "confirmation": "APPROVE",
            "recipient": draft["recipient"],
            "message": draft["message"],
        },
        headers=_csrf_headers(),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert context.crm.list_pending_send_requests(limit=10) == []
    assert context.crm.list_interactions(lead["id"]) == []
    audit = context.crm.list_admin_audit(limit=10)
    assert audit[0]["action"] == "draft.approve_exact"
    assert draft["message"] not in repr(audit[0]["details"])
    assert draft["recipient"] not in repr(audit[0]["details"])

    assert (
        client.post(
            f"/api/admin/drafts/{draft['id']}/send", headers=_csrf_headers()
        ).status_code
        == 404
    )


def test_safe_cycle_and_sheet_sync_are_audited_background_jobs(admin_client) -> None:
    client, context, autopilot, sheets = admin_client
    _login(client)

    cycle = client.post("/api/admin/autopilot/run", headers=_csrf_headers())
    sync = client.post("/api/admin/sheets/sync", headers=_csrf_headers())

    assert cycle.status_code == 202
    assert sync.status_code == 202
    assert autopilot.run_calls == 1
    assert sheets.sync_calls == 1
    jobs = context.jobs.list(limit=10)
    assert {item["status"] for item in jobs} == {"completed"}
    actions = {item["action"] for item in context.crm.list_admin_audit(limit=20)}
    assert {"autopilot.run_cycle", "google_sheets.sync"}.issubset(actions)


def test_logout_requires_csrf(admin_client) -> None:
    client, _context, _autopilot, _sheets = admin_client
    _login(client)

    assert client.post("/auth/logout").status_code == 403
    assert client.get("/api/admin/bootstrap").status_code == 200
    assert (
        client.post(
            "/auth/logout", headers=_csrf_headers(), follow_redirects=False
        ).status_code
        == 303
    )
    assert client.get("/api/admin/bootstrap").status_code == 401


def test_versioned_api_and_workspace_invitation(admin_client) -> None:
    client, context, _autopilot, _sheets = admin_client
    _login(client)

    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["user"]["role"] == "owner"
    assert bootstrap.json()["workspace"]["id"] == "ollum-group"
    coordination = bootstrap.json()["agent_coordination"]
    assert coordination["execution_mode"] == "chatgpt_mcp_two_chat"
    assert coordination["lanes"]["inbox"]["responsibility"]
    assert coordination["lanes"]["prospecting"]["responsibility"]
    assert coordination["safety"]["private_message_text_included"] is False

    invited = client.post(
        "/api/v1/workspace/invitations",
        json={"email": "viewer@example.com", "role": "viewer"},
        headers=_csrf_headers(),
    )
    assert invited.status_code == 201
    assert invited.json()["email"] == "viewer@example.com"
    members = client.get("/api/v1/workspace/members").json()
    assert members["invitations"][0]["role"] == "viewer"
    assert (
        context.crm.list_workspace_invitations("ollum-group")[0]["status"] == "pending"
    )


def test_authenticated_whatsapp_qr_proxy_never_exposes_pairing_value(
    admin_client, monkeypatch
) -> None:
    client, _context, _autopilot, _sheets = admin_client
    _login(client)
    png = b"\x89PNG\r\n\x1a\npublic-image-only"
    monkeypatch.setattr(admin_module, "bridge_pairing_qr", lambda: png)

    response = client.get("/api/v1/whatsapp/qr")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.content == png


def test_company_memory_and_agent_inbox_apis_are_workspace_scoped(admin_client) -> None:
    client, context, _autopilot, _sheets = admin_client
    _login(client)

    profile = client.patch(
        "/api/v1/company/profile",
        headers=_csrf_headers(),
        json={
            "company_name": "Ollum Group",
            "industry": "Digital services",
            "geography": "RU / worldwide",
            "target_customer": "B2B companies",
            "positioning": "Grounded digital delivery",
        },
    )
    assert profile.status_code == 200
    assert profile.json()["profile"]["company_name"] == "Ollum Group"

    knowledge = client.post(
        "/api/v1/company/knowledge",
        headers=_csrf_headers(),
        json={
            "category": "service",
            "title": "Web applications",
            "content": {"details": "Personal cabinets and internal services"},
        },
    )
    assert knowledge.status_code == 201
    assert client.get("/api/v1/company/knowledge").json()[0]["category"] == "service"

    event, _created = context.crm.upsert_agent_inbox_event(
        "ollum-group",
        external_id="admin-inbox-1",
        chat_jid="79991234567@s.whatsapp.net",
        message_text="Нужна консультация",
        received_at="2026-08-23T10:00:00+00:00",
    )
    lead = context.crm.upsert_lead(
        "Inbox Lead", "https://inbox-lead.example", phones=["+79991234567"]
    )
    inbox = client.get("/api/v1/agent/inbox?status=new")
    assert inbox.status_code == 200
    assert inbox.json()[0]["id"] == event["id"]
    linked = client.patch(
        f"/api/v1/agent/inbox/{event['id']}",
        headers=_csrf_headers(),
        json={"lead_id": lead["id"]},
    )
    assert linked.status_code == 200
    assert linked.json()["lead_id"] == lead["id"]
    updated = client.patch(
        f"/api/v1/agent/inbox/{event['id']}",
        headers=_csrf_headers(),
        json={"status": "acknowledged"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "acknowledged"

    bootstrap = client.get("/api/v1/bootstrap").json()
    assert bootstrap["company_onboarding"]["profile"]["company_name"] == "Ollum Group"
    assert bootstrap["agent_inbox"]["acknowledged"] == 1

    retry_event, _created = context.crm.upsert_agent_inbox_event(
        "ollum-group",
        external_id="admin-inbox-retry",
        chat_jid="79991234567@s.whatsapp.net",
        message_text="Повторите обработку",
        received_at=datetime.now(UTC).isoformat(timespec="seconds"),
        lead_id=lead["id"],
    )
    context.crm.finish_agent_inbox_event(
        "ollum-group",
        retry_event["id"],
        status="needs_review",
        error="temporary failure",
    )
    bypass = client.patch(
        f"/api/v1/agent/inbox/{retry_event['id']}",
        headers=_csrf_headers(),
        json={"status": "new"},
    )
    assert bypass.status_code == 400
    missing_confirmation = client.post(
        f"/api/v1/agent/inbox/{retry_event['id']}/retry",
        headers=_csrf_headers(),
        json={},
    )
    assert missing_confirmation.status_code == 400
    retried = client.post(
        f"/api/v1/agent/inbox/{retry_event['id']}/retry",
        headers=_csrf_headers(),
        json={"confirm_retry": True},
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "new"
    assert retried.json()["agent_attempts"] == 0


def test_conversation_agent_api_is_configurable_but_cannot_send(
    admin_client, monkeypatch
) -> None:
    client, context, _autopilot, _sheets = admin_client
    _login(client)

    status = client.get("/api/v1/conversation-agent/settings")
    assert status.status_code == 200
    assert status.json()["safety"] == {
        "approves": False,
        "sends": False,
        "external_send": False,
        "draft_only": True,
    }

    assert (
        client.patch(
            "/api/v1/conversation-agent/settings",
            json={"niche": "e-commerce"},
        ).status_code
        == 403
    )
    updated = client.patch(
        "/api/v1/conversation-agent/settings",
        headers=_csrf_headers(),
        json={
            "enabled": True,
            "autonomy_mode": "draft",
            "niche": "e-commerce",
            "tone": "Кратко и уважительно",
            "qualification_questions": ["Какой объём каталога?"],
            "confidence_threshold": 74,
            "max_inbound_age_hours": 96,
            "response_sla_minutes": 45,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["settings"]["niche"] == "e-commerce"
    assert updated.json()["settings"]["max_inbound_age_hours"] == 96
    assert updated.json()["settings"]["response_sla_minutes"] == 45
    assert updated.json()["settings"]["send_enabled"] is False

    forbidden = client.patch(
        "/api/v1/conversation-agent/settings",
        headers=_csrf_headers(),
        json={"send_enabled": True},
    )
    assert forbidden.status_code == 400

    monkeypatch.setattr(
        admin_module,
        "sync_whatsapp_inbox",
        lambda *_args, **_kwargs: {
            "success": True,
            "new_events": 1,
            "existing_events": 0,
        },
    )
    queued = client.post(
        "/api/v1/conversation-agent/process",
        headers=_csrf_headers(),
        json={"scan_limit": 100},
    )
    assert queued.status_code == 200
    assert queued.json()["execution_mode"] == "chatgpt_mcp"
    assert queued.json()["new_events"] == 1
    assert context.conversation_agent is not None
    assert context.crm.list_pending_send_requests(limit=10) == []
    assert (
        client.post(
            "/api/v1/conversation-agent/send", headers=_csrf_headers()
        ).status_code
        == 404
    )
