from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest
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
        ]
    )
    app.state.admin_context = context
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
    assert ":focus-visible" in stylesheet.text
    assert "--paper: #efede6" in stylesheet.text
    assert "--accent: #11d873" in stylesheet.text
    assert "env(safe-area-inset-top)" in stylesheet.text
    assert "overscroll-behavior: contain" in stylesheet.text
    assert "outline: none" not in stylesheet.text


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
