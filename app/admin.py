from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from .agent_inbox import resolve_target_chat_jid, sync_whatsapp_inbox
from .auth import AuthenticationError, OIDCSessionManager
from .autopilot import AutopilotService
from .chatgpt_playbook import (
    first_connection_plan,
    lane_prompts,
    reasoning_boundary,
    two_chat_handoff,
)
from .config import Settings
from .conversation_agent import ConversationAgent
from .crm import SalesCRM, utc_now
from .google_sheets import GoogleSheetsSync
from .oauth_server import POST_LOGIN_REDIRECT_KEY, safe_post_login_redirect
from .whatsapp_service import (
    bridge_pairing_qr,
    bridge_pairing_status,
    bridge_status,
)

logger = logging.getLogger("ollum-sales-admin")
STATIC_ROOT = Path(__file__).with_name("admin_static")
OAUTH_STATIC_ROOT = Path(__file__).with_name("admin_assets")
ROLE_RANK = {"viewer": 0, "operator": 1, "owner": 2}
mimetypes.add_type("font/woff2", ".woff2")


class AdminRequestError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _safe_job_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"success": True}
    summary: dict[str, Any] = {
        key: result[key]
        for key in (
            "success",
            "blocked",
            "message",
            "tabs",
            "processed",
            "drafted",
            "needs_review",
            "ignored",
            "failed",
        )
        if key in result
    }
    cycle = result.get("cycle")
    if isinstance(cycle, dict):
        summary["cycle"] = {
            key: cycle[key]
            for key in ("id", "mode", "status", "started_at", "completed_at")
            if key in cycle
        }
    return summary


class AdminJobRegistry:
    """Small in-process job registry for long-running SAFE operations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def create(self, *, name: str, actor: str) -> dict[str, Any]:
        job = {
            "id": str(uuid.uuid4()),
            "name": name,
            "actor": actor,
            "status": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job["id"]] = job
            if len(self._jobs) > 100:
                oldest = sorted(
                    self._jobs.values(), key=lambda item: item["created_at"]
                )[:-100]
                for item in oldest:
                    self._jobs.pop(item["id"], None)
        return dict(job)

    def run(
        self,
        job_id: str,
        operation: Callable[[], Any],
        crm: SalesCRM,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = utc_now()
            actor = job["actor"]
            name = job["name"]
        try:
            result = operation()
            summary = _safe_job_result(result)
            status = "blocked" if summary.get("blocked") else "completed"
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = status
                job["result"] = summary
                job["completed_at"] = utc_now()
            crm.record_admin_audit(
                actor=actor,
                action=name,
                target_type="background_job",
                target_id=job_id,
                outcome=status,
                details=summary,
            )
        except Exception as exc:
            logger.exception("Admin background job failed: %s", name)
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["error"] = "Operation failed; inspect server logs"
                job["completed_at"] = utc_now()
            crm.record_admin_audit(
                actor=actor,
                action=name,
                target_type="background_job",
                target_id=job_id,
                outcome="failed",
                details={"error_type": type(exc).__name__},
            )

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(), key=lambda item: item["created_at"], reverse=True
            )[: max(1, min(int(limit), 100))]
            return [dict(job) for job in jobs]


@dataclass(frozen=True)
class AdminContext:
    crm: SalesCRM
    autopilot: AutopilotService
    sheets: GoogleSheetsSync
    settings: Settings
    sessions: OIDCSessionManager
    jobs: AdminJobRegistry
    conversation_agent: ConversationAgent | None = None


class SecurityHeadersMiddleware:
    """Set browser hardening headers for the dashboard and JSON API."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                default_security_headers = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"permissions-policy",
                        b"camera=(), microphone=(), geolocation=(), payment=()",
                    ),
                    (
                        b"content-security-policy",
                        (
                            b"default-src 'self'; base-uri 'self'; form-action 'self'; "
                            b"frame-ancestors 'none'; object-src 'none'; "
                            b"img-src 'self' data:; style-src 'self'; "
                            b"script-src 'self'; connect-src 'self'"
                        ),
                    ),
                ]
                existing_header_names = {name.lower() for name, _value in headers}
                headers.extend(
                    (name, value)
                    for name, value in default_security_headers
                    if name not in existing_header_names
                )
                request_headers = dict(scope.get("headers", []))
                forwarded_proto = request_headers.get(b"x-forwarded-proto", b"")
                if scope.get("scheme") == "https" or forwarded_proto == b"https":
                    headers.append(
                        (
                            b"strict-transport-security",
                            b"max-age=31536000; includeSubDomains",
                        )
                    )
                path = str(scope.get("path") or "")
                if path == "/admin" or path.startswith(
                    ("/api/admin", "/api/v1", "/auth/", "/oauth/")
                ):
                    existing_cache_control = [
                        value
                        for name, value in headers
                        if name.lower() == b"cache-control"
                    ]
                    if not any(
                        b"no-store" in value.lower() for value in existing_cache_control
                    ):
                        headers = [
                            (name, value)
                            for name, value in headers
                            if name.lower() != b"cache-control"
                        ]
                        headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _get_user(request: Request) -> dict[str, Any] | None:
    try:
        value = request.session.get("user")
    except AssertionError:
        return None
    if not isinstance(value, dict):
        return None
    expires_at = value.get("expires_at")
    if expires_at is not None and int(expires_at) <= int(time.time()):
        request.session.clear()
        return None
    if value.get("role") not in ROLE_RANK or not value.get("workspace_id"):
        request.session.clear()
        return None
    return value


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    role = str(user.get("role") or "viewer")
    return {
        "email": user.get("email"),
        "name": user.get("name"),
        "scopes": list(user.get("scopes") or []),
        "csrf": user.get("csrf"),
        "member_id": user.get("member_id"),
        "workspace_id": user.get("workspace_id"),
        "workspace_name": user.get("workspace_name"),
        "role": role,
        "capabilities": {
            "write": ROLE_RANK.get(role, -1) >= ROLE_RANK["operator"],
            "manage_members": role == "owner",
            "send_whatsapp": False,
        },
    }


def _require_operational_company(
    context: AdminContext, user: dict[str, Any]
) -> dict[str, Any]:
    onboarding = context.crm.get_company_onboarding_state(str(user["workspace_id"]))
    if not onboarding["sales_ready"]:
        raise AdminRequestError(
            409,
            "Complete and confirm the company onboarding before starting operational work",
        )
    return onboarding


def _require_user(request: Request) -> dict[str, Any]:
    user = _get_user(request)
    if user is None:
        raise AdminRequestError(401, "Authentication required")
    return user


def _require_csrf(request: Request, user: dict[str, Any]) -> None:
    expected = str(user.get("csrf") or "")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise AdminRequestError(403, "Invalid CSRF token")


def _require_write(request: Request, user: dict[str, Any], settings: Settings) -> None:
    required_scope = settings.admin_write_scope
    scopes = {str(item) for item in user.get("scopes") or []}
    if required_scope and required_scope not in scopes:
        raise AdminRequestError(403, f"Missing required scope: {required_scope}")
    if ROLE_RANK.get(str(user.get("role")), -1) < ROLE_RANK["operator"]:
        raise AdminRequestError(403, "Workspace operator role is required")
    _require_csrf(request, user)


def _require_owner(request: Request, user: dict[str, Any], settings: Settings) -> None:
    _require_write(request, user, settings)
    if user.get("role") != "owner":
        raise AdminRequestError(403, "Workspace owner role is required")


def admin_endpoint(*, write: bool = False, owner: bool = False):
    def decorator(handler):
        @wraps(handler)
        async def wrapped(request: Request) -> Response:
            context: AdminContext = request.app.state.admin_context
            try:
                user = _require_user(request)
                if owner:
                    _require_owner(request, user, context.settings)
                elif write:
                    _require_write(request, user, context.settings)
                return await handler(request, context, user)
            except AdminRequestError as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
            except (TypeError, ValueError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except Exception:
                logger.exception("Admin request failed: %s", request.url.path)
                return JSONResponse(
                    {"error": "Internal operation failed; inspect server logs"},
                    status_code=500,
                )

        return wrapped

    return decorator


async def _read_json(request: Request) -> dict[str, Any]:
    length = int(request.headers.get("content-length") or 0)
    if length > 65_536:
        raise AdminRequestError(413, "Request body is too large")
    if "application/json" not in request.headers.get("content-type", ""):
        raise AdminRequestError(415, "Content-Type must be application/json")
    value = await request.json()
    if not isinstance(value, dict):
        raise TypeError("JSON body must be an object")
    return value


def _bounded_int(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _draft_fingerprint(draft: dict[str, Any]) -> str:
    payload = f"{draft.get('recipient') or ''}\0{draft.get('message') or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _draft_view(draft: dict[str, Any]) -> dict[str, Any]:
    return {**draft, "fingerprint": _draft_fingerprint(draft)}


def _metadata_url(resource_url: str | None) -> str | None:
    if not resource_url:
        return None
    parsed = urlsplit(resource_url)
    return (
        f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource"
        f"{parsed.path.rstrip('/')}"
    )


def _plugin_status(
    settings: Settings,
    *,
    onboarding: dict[str, Any] | None = None,
    whatsapp_connected: bool | None = None,
) -> dict[str, Any]:
    resource_url = settings.mcp_resource_url
    if not resource_url and settings.public_base_url:
        resource_url = f"{settings.public_base_url.rstrip('/')}/mcp"
    checks = {
        "https_resource_url": bool(
            resource_url and resource_url.startswith("https://")
        ),
        "oidc_mode": settings.auth_mode == "oidc",
        "issuer_configured": bool(settings.oidc_issuer_url),
        "audience_configured": bool(settings.oidc_audience),
        "admin_client_configured": bool(
            settings.admin_oidc_client_id and settings.admin_oidc_client_secret
        ),
        "beta_allowlist_configured": bool(settings.admin_allowed_emails),
        "session_secret_configured": bool(settings.admin_session_secret),
    }
    prompts = lane_prompts()
    return {
        "name": "Ollum Sales",
        "description": (
            "ChatGPT-native sales agent: grounded research, scoring and SAFE "
            "WhatsApp drafting without a server-side LLM API."
        ),
        "brain": "ChatGPT through Ollum Sales MCP",
        "server_llm_enabled": False,
        "reasoning_boundary": reasoning_boundary(),
        "prospecting_queue": {
            "producer": "server_public_fact_collector",
            "consumer": "primary_chat_chatgpt",
            "max_pending": max(1, settings.chatgpt_prospecting_queue_limit),
            "backpressure": "pause_discovery_when_full",
        },
        "server_sync_interval_minutes": 15,
        "recommended_chatgpt_schedule": "hourly_in_chat",
        "tenant_mode": "single_company_closed_beta",
        "external_tenant_onboarding_supported": False,
        "scheduled_prompt": prompts["inbox"],
        "scheduled_prompts": prompts,
        "chat_handoff": two_chat_handoff(),
        "first_connection": first_connection_plan(
            onboarding
            or {
                "ready_for_sales": False,
                "sales_ready": False,
                "onboarding_status": "not_started",
                "next_questions": [],
            },
            whatsapp_connected=whatsapp_connected,
        ),
        "server_url": resource_url,
        "authentication": "OAuth",
        "authorization_server": settings.oidc_issuer_url,
        "protected_resource_metadata": _metadata_url(resource_url),
        "required_scopes": list(settings.mcp_required_scopes),
        "dashboard_url": settings.dashboard_base_url or settings.public_base_url,
        "checks": checks,
        "ready": all(checks.values()),
    }


async def admin_home(request: Request) -> Response:
    if _get_user(request) is None:
        return RedirectResponse("/auth/login", status_code=303)
    return FileResponse(STATIC_ROOT / "index.html")


async def auth_login(request: Request) -> Response:
    context: AdminContext = request.app.state.admin_context
    if context.sessions.login_must_start_on_redirect_host(request):
        return RedirectResponse(context.sessions.login_start_url(), status_code=303)
    return await context.sessions.begin_login(request)


async def auth_callback(request: Request) -> Response:
    context: AdminContext = request.app.state.admin_context
    post_login_redirect = safe_post_login_redirect(
        request.session.get(POST_LOGIN_REDIRECT_KEY)
    )
    try:
        user = await context.sessions.complete_login(request)
    except AuthenticationError as exc:
        context.crm.record_admin_audit(
            actor="unknown",
            action="admin.login",
            outcome="denied",
            details={"reason": str(exc)},
        )
        return JSONResponse({"error": str(exc)}, status_code=403)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="admin.login",
        outcome="success",
    )
    if context.sessions.uses_cross_origin_handoff:
        code = context.sessions.issue_login_handoff(user)
        return RedirectResponse(
            f"{context.sessions.dashboard_url('/auth/handoff')}?code={code}",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
    destination = post_login_redirect or "/admin"
    return RedirectResponse(
        context.sessions.dashboard_url(destination),
        status_code=303,
    )


async def auth_handoff(request: Request) -> Response:
    context: AdminContext = request.app.state.admin_context
    post_login_redirect = safe_post_login_redirect(
        request.session.get(POST_LOGIN_REDIRECT_KEY)
    )
    user = context.sessions.consume_login_handoff(
        str(request.query_params.get("code") or "")
    )
    if user is None:
        context.crm.record_admin_audit(
            actor="unknown",
            action="admin.login_handoff",
            outcome="denied",
        )
        return JSONResponse(
            {"error": "The login handoff expired or was already used"},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    request.session.clear()
    request.session["user"] = user
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="admin.login_handoff",
        outcome="success",
    )
    destination = post_login_redirect or "/admin"
    return RedirectResponse(
        context.sessions.dashboard_url(destination),
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


async def auth_logout(request: Request) -> Response:
    context: AdminContext = request.app.state.admin_context
    user = _get_user(request)
    if user:
        try:
            _require_csrf(request, user)
        except AdminRequestError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        context.crm.record_admin_audit(
            actor=str(user.get("email") or "unknown"),
            action="admin.logout",
            outcome="success",
        )
    context.sessions.logout(request)
    return RedirectResponse(
        context.sessions.dashboard_url("/auth/login"),
        status_code=303,
    )


@admin_endpoint()
async def api_bootstrap(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    state = context.autopilot.status()
    wa = bridge_status()
    pairing = bridge_pairing_status()
    workspace_id = str(user["workspace_id"])
    company_onboarding = context.crm.get_company_onboarding_state(workspace_id)
    workspace = context.crm.get_workspace(workspace_id)
    members = context.crm.list_workspace_members(workspace_id)
    invitations = context.crm.list_workspace_invitations(workspace_id)
    return JSONResponse(
        {
            "user": _public_user(user),
            "workspace": {
                **workspace,
                "active_members": sum(
                    1 for member in members if member.get("status") == "active"
                ),
                "pending_invitations": len(invitations),
            },
            "company_onboarding": company_onboarding,
            "agent_inbox": context.crm.agent_inbox_summary(workspace_id),
            "agent_coordination": context.crm.agent_coordination_summary(
                workspace_id, include_leads=True, limit=12
            ),
            "conversation_agent": (
                context.conversation_agent.status(workspace_id)
                if context.conversation_agent is not None
                else {
                    "settings": context.crm.get_conversation_agent_settings(
                        workspace_id
                    ),
                    "summary": context.crm.conversation_agent_summary(workspace_id),
                    "runtime": {
                        "ready": False,
                        "reason": "conversation agent runtime is unavailable",
                    },
                    "safety": {"approves": False, "sends": False},
                }
            ),
            "overview": context.crm.overview(),
            "crm": context.crm.stats(),
            "autopilot": state,
            "whatsapp": {
                key: wa.get(key)
                for key in (
                    "reachable",
                    "ready",
                    "connected",
                    "logged_in",
                    "send_enabled",
                    "account_jid",
                )
            },
            "whatsapp_pairing": pairing,
            "google_sheets": context.sheets.status(),
            "top_leads": context.crm.list_leads(limit=12, order_by_score=True),
            "campaigns": context.crm.list_campaigns(limit=12),
            "drafts": [
                _draft_view(item) for item in context.crm.list_outreach_drafts(limit=12)
            ],
            "verticals": context.crm.list_verticals(limit=100),
            "cycles": context.crm.list_autopilot_cycles(limit=12),
            "audit": context.crm.list_admin_audit(limit=20),
            "jobs": context.jobs.list(limit=20),
            "plugin": _plugin_status(
                context.settings,
                onboarding=company_onboarding,
                whatsapp_connected=bool(wa.get("connected") and wa.get("logged_in")),
            ),
            "safety": {
                "safe_mode": state.get("mode") == "safe",
                "whatsapp_send_enabled": bool(context.settings.allow_whatsapp_send),
                "autopilot_send_enabled": bool(context.settings.allow_autopilot_send),
                "send_controls_exposed": False,
            },
        }
    )


@admin_endpoint()
async def api_session(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    return JSONResponse(
        {
            "user": _public_user(user),
            "workspace": context.crm.get_workspace(str(user["workspace_id"])),
        }
    )


@admin_endpoint()
async def api_whatsapp_status(
    _request: Request, _context: AdminContext, _user: dict[str, Any]
) -> Response:
    return JSONResponse(
        {
            "bridge": bridge_status(),
            "pairing": bridge_pairing_status(),
            "send_enabled": False,
        }
    )


@admin_endpoint()
async def api_whatsapp_qr(
    _request: Request, _context: AdminContext, _user: dict[str, Any]
) -> Response:
    image = bridge_pairing_qr()
    if image is None:
        return JSONResponse({"error": "No active WhatsApp pairing QR"}, status_code=404)
    return Response(
        image,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@admin_endpoint()
async def api_company_profile(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    workspace_id = str(user["workspace_id"])
    return JSONResponse(context.crm.get_company_onboarding_state(workspace_id))


@admin_endpoint(write=True)
async def api_update_company_profile(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    allowed = {
        "company_name",
        "website_url",
        "industry",
        "geography",
        "positioning",
        "target_customer",
        "sales_process",
        "tone_of_voice",
        "primary_goal",
        "constraints",
        "language",
    }
    unexpected = set(body) - allowed
    if unexpected:
        raise ValueError(
            f"unsupported company profile fields: {', '.join(sorted(unexpected))}"
        )
    workspace_id = str(user["workspace_id"])
    context.crm.update_company_profile(workspace_id, **body)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="company.profile_update",
        target_type="workspace",
        target_id=workspace_id,
        outcome="success",
        details={"fields": sorted(body)},
    )
    return JSONResponse(context.crm.get_company_onboarding_state(workspace_id))


@admin_endpoint(write=True)
async def api_complete_company_onboarding(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    workspace_id = str(user["workspace_id"])
    result = context.crm.complete_company_onboarding(
        workspace_id,
        confirm_ready=body.get("confirm_ready") is True,
        confirmed_revision=int(body["confirmed_revision"]),
        summary_hash=str(body.get("summary_hash") or ""),
        confirmed_by=str(user["email"]),
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="company.onboarding_confirm",
        target_type="workspace",
        target_id=workspace_id,
        outcome="success",
        details={"confirmed_revision": result["profile"]["revision"]},
    )
    return JSONResponse(result)


@admin_endpoint()
async def api_company_knowledge(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    return JSONResponse(
        context.crm.list_company_knowledge(
            str(user["workspace_id"]),
            category=request.query_params.get("category") or None,
            status=request.query_params.get("status") or "active",
            limit=_bounded_int(
                request.query_params.get("limit", 200),
                name="limit",
                minimum=1,
                maximum=500,
            ),
        )
    )


@admin_endpoint(write=True)
async def api_save_company_knowledge(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    result = context.crm.save_company_knowledge(
        str(user["workspace_id"]),
        category=str(body.get("category") or ""),
        title=str(body.get("title") or ""),
        content=body.get("content"),
        source_type=str(body.get("source_type") or "dashboard"),
        source_name=str(body.get("source_name") or "") or None,
        item_id=str(body.get("item_id") or "") or None,
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="company.knowledge_save",
        target_type="company_knowledge",
        target_id=result["id"],
        outcome="success",
        details={"category": result["category"]},
    )
    return JSONResponse(result, status_code=201 if not body.get("item_id") else 200)


@admin_endpoint(write=True)
async def api_archive_company_knowledge(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    item_id = str(request.path_params["item_id"])
    result = context.crm.archive_company_knowledge(str(user["workspace_id"]), item_id)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="company.knowledge_archive",
        target_type="company_knowledge",
        target_id=item_id,
        outcome="success",
    )
    return JSONResponse(result)


@admin_endpoint()
async def api_agent_inbox(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    target_chat_jid = resolve_target_chat_jid(
        phone=request.query_params.get("phone") or None,
        chat_jid=request.query_params.get("chat_jid") or None,
    )
    return JSONResponse(
        context.crm.list_agent_inbox_events(
            str(user["workspace_id"]),
            status=request.query_params.get("status") or None,
            chat_jid=target_chat_jid,
            limit=_bounded_int(
                request.query_params.get("limit", 100),
                name="limit",
                minimum=1,
                maximum=500,
            ),
        )
    )


@admin_endpoint(write=True)
async def api_sync_agent_inbox(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    result = sync_whatsapp_inbox(
        context.crm,
        str(user["workspace_id"]),
        scan_limit=_bounded_int(
            body.get("scan_limit", 100),
            name="scan_limit",
            minimum=1,
            maximum=100,
        ),
        phone=body.get("phone") or None,
        chat_jid=body.get("chat_jid") or None,
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="agent.inbox_sync",
        target_type="workspace",
        target_id=str(user["workspace_id"]),
        outcome="success",
        details={key: result[key] for key in ("new_events", "existing_events")},
    )
    return JSONResponse(result)


@admin_endpoint(write=True)
async def api_update_agent_inbox(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    workspace_id = str(user["workspace_id"])
    event_id = str(request.path_params["event_id"])
    status = str(body.get("status") or "").strip()
    lead_id = str(body.get("lead_id") or "").strip()
    if not status and not lead_id:
        raise ValueError("status or lead_id is required")
    if status and status not in {"acknowledged", "resolved", "ignored"}:
        raise ValueError(
            "status must be acknowledged, resolved or ignored; use the retry endpoint"
        )
    if body.get("draft_id") is not None:
        raise ValueError("draft_id cannot be changed through this endpoint")
    result = context.crm.get_agent_inbox_event(workspace_id, event_id)
    if lead_id:
        result = context.crm.link_agent_inbox_event(workspace_id, event_id, lead_id)
    if status:
        result = context.crm.update_agent_inbox_event(
            workspace_id,
            event_id,
            status=status,
            draft_id=str(body.get("draft_id") or "") or None,
        )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="agent.inbox_status",
        target_type="agent_inbox_event",
        target_id=result["id"],
        outcome="success",
        details={
            "status": result["status"],
            "lead_linked": bool(lead_id),
        },
    )
    return JSONResponse(result)


@admin_endpoint(write=True)
async def api_retry_agent_inbox(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    if body.get("confirm_retry") is not True:
        raise ValueError("confirm_retry=true is required")
    workspace_id = str(user["workspace_id"])
    event_id = str(request.path_params["event_id"])
    result = context.crm.requeue_agent_inbox_event(workspace_id, event_id)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="agent.inbox_retry",
        target_type="agent_inbox_event",
        target_id=result["id"],
        outcome="success",
        details={
            "previous_status": result["previous_status"],
            "status": result["status"],
            "idempotent": result["idempotent"],
            "sent": False,
        },
    )
    return JSONResponse(result)


@admin_endpoint()
async def api_conversation_agent_settings(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    workspace_id = str(user["workspace_id"])
    return JSONResponse(
        context.conversation_agent.status(workspace_id)
        if context.conversation_agent is not None
        else {
            "settings": context.crm.get_conversation_agent_settings(workspace_id),
            "summary": context.crm.conversation_agent_summary(workspace_id),
            "runtime": {"ready": False, "reason": "runtime unavailable"},
            "safety": {"approves": False, "sends": False},
        }
    )


@admin_endpoint(write=True)
async def api_update_conversation_agent_settings(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    allowed = {
        "enabled",
        "autonomy_mode",
        "niche",
        "objective",
        "instructions",
        "tone",
        "qualification_questions",
        "forbidden_topics",
        "escalation_rules",
        "max_context_messages",
        "max_reply_chars",
        "max_inbound_age_hours",
        "response_sla_minutes",
        "confidence_threshold",
        "auto_create_inbound_leads",
    }
    unexpected = set(body) - allowed
    if unexpected:
        raise ValueError(
            "unsupported conversation agent fields: " + ", ".join(sorted(unexpected))
        )
    workspace_id = str(user["workspace_id"])
    settings = context.crm.update_conversation_agent_settings(workspace_id, **body)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="conversation_agent.settings_update",
        target_type="workspace",
        target_id=workspace_id,
        outcome="success",
        details={"fields": sorted(body), "send_enabled": False},
    )
    return JSONResponse(
        context.conversation_agent.status(workspace_id)
        if context.conversation_agent is not None
        else {
            "settings": settings,
            "summary": context.crm.conversation_agent_summary(workspace_id),
            "runtime": {"ready": False, "reason": "runtime unavailable"},
            "safety": {"approves": False, "sends": False},
        }
    )


@admin_endpoint()
async def api_conversation_sessions(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    return JSONResponse(
        context.crm.list_conversation_sessions(
            str(user["workspace_id"]),
            stage=request.query_params.get("stage") or None,
            limit=_bounded_int(
                request.query_params.get("limit", 100),
                name="limit",
                minimum=1,
                maximum=500,
            ),
        )
    )


@admin_endpoint(write=True)
async def api_process_conversations(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    scan_limit = _bounded_int(
        body.get("scan_limit", 100),
        name="scan_limit",
        minimum=1,
        maximum=100,
    )
    workspace_id = str(user["workspace_id"])
    result = sync_whatsapp_inbox(
        context.crm,
        workspace_id,
        scan_limit=scan_limit,
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="conversation_agent.queue_sync",
        target_type="workspace",
        target_id=workspace_id,
        outcome="success",
        details={
            "scan_limit": scan_limit,
            "new_events": result["new_events"],
            "existing_events": result["existing_events"],
            "chatgpt_reasoning_started": False,
            "approves": False,
            "sends": False,
        },
    )
    return JSONResponse(
        {
            **result,
            "execution_mode": "chatgpt_mcp",
            "next_action": "Open ChatGPT and run the Ollum Sales agent cycle.",
        }
    )


@admin_endpoint()
async def api_workspace_members(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    workspace_id = str(user["workspace_id"])
    payload: dict[str, Any] = {
        "workspace": context.crm.get_workspace(workspace_id),
        "members": context.crm.list_workspace_members(workspace_id),
        "invitations": [],
    }
    if user.get("role") == "owner":
        payload["invitations"] = context.crm.list_workspace_invitations(workspace_id)
    return JSONResponse(payload)


@admin_endpoint(owner=True)
async def api_invite_workspace_member(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    result = context.crm.invite_workspace_member(
        workspace_id=str(user["workspace_id"]),
        email=str(body.get("email") or ""),
        role=str(body.get("role") or "viewer"),
        invited_by=str(user["email"]),
        expires_in_days=_bounded_int(
            body.get("expires_in_days", 7),
            name="expires_in_days",
            minimum=1,
            maximum=30,
        ),
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="workspace.member_invite",
        target_type="workspace_invitation",
        target_id=result["id"],
        outcome="success",
        details={"role": result["role"]},
    )
    return JSONResponse(result, status_code=201)


@admin_endpoint(owner=True)
async def api_update_workspace_member(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    member_id = request.path_params["member_id"]
    requested_role = str(body.get("role") or "")
    if member_id == user.get("member_id") and requested_role != "owner":
        raise AdminRequestError(409, "Transfer ownership before changing your own role")
    result = context.crm.update_workspace_member_role(
        workspace_id=str(user["workspace_id"]),
        member_id=member_id,
        role=requested_role,
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="workspace.member_role",
        target_type="workspace_member",
        target_id=member_id,
        outcome="success",
        details={"role": result["role"]},
    )
    return JSONResponse(result)


@admin_endpoint()
async def api_leads(
    request: Request, context: AdminContext, _user: dict[str, Any]
) -> Response:
    params = request.query_params
    status = params.get("status") or None
    min_score = int(params["min_score"]) if params.get("min_score") else None
    limit = _bounded_int(params.get("limit", 100), name="limit", minimum=1, maximum=200)
    fresh_only = params.get("fresh_only", "false").lower() in {"1", "true", "yes"}
    return JSONResponse(
        context.crm.list_leads(
            status=status,
            min_score=min_score,
            limit=limit,
            fresh_evidence_only=fresh_only,
        )
    )


@admin_endpoint()
async def api_campaigns(
    request: Request, context: AdminContext, _user: dict[str, Any]
) -> Response:
    status = request.query_params.get("status") or None
    return JSONResponse(context.crm.list_campaigns(status=status, limit=200))


@admin_endpoint()
async def api_drafts(
    request: Request, context: AdminContext, _user: dict[str, Any]
) -> Response:
    status = request.query_params.get("status") or None
    return JSONResponse(
        [
            _draft_view(item)
            for item in context.crm.list_outreach_drafts(status=status, limit=200)
        ]
    )


@admin_endpoint()
async def api_verticals(
    _request: Request, context: AdminContext, _user: dict[str, Any]
) -> Response:
    return JSONResponse(context.crm.list_verticals(limit=500))


@admin_endpoint()
async def api_audit(
    request: Request, context: AdminContext, _user: dict[str, Any]
) -> Response:
    limit = _bounded_int(
        request.query_params.get("limit", 100),
        name="limit",
        minimum=1,
        maximum=500,
    )
    return JSONResponse(context.crm.list_admin_audit(limit=limit))


@admin_endpoint()
async def api_jobs(
    _request: Request, context: AdminContext, _user: dict[str, Any]
) -> Response:
    return JSONResponse(context.jobs.list(limit=100))


@admin_endpoint(write=True)
async def api_autopilot_start(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    requested_mode = str(body.get("mode") or "safe").strip().lower()
    if requested_mode != "safe":
        raise AdminRequestError(409, "Closed beta only permits SAFE mode")
    _require_operational_company(context, user)
    result = context.autopilot.start(
        mode="safe",
        interval_minutes=_bounded_int(
            body.get("interval_minutes", context.settings.autopilot_interval_minutes),
            name="interval_minutes",
            minimum=15,
            maximum=1440,
        ),
        max_verticals_per_cycle=_bounded_int(
            body.get(
                "max_verticals_per_cycle",
                context.settings.autopilot_max_verticals_per_cycle,
            ),
            name="max_verticals_per_cycle",
            minimum=1,
            maximum=10,
        ),
        leads_per_vertical=_bounded_int(
            body.get(
                "leads_per_vertical", context.settings.autopilot_leads_per_vertical
            ),
            name="leads_per_vertical",
            minimum=1,
            maximum=50,
        ),
        score_threshold=_bounded_int(
            body.get("score_threshold", context.settings.autopilot_score_threshold),
            name="score_threshold",
            minimum=0,
            maximum=100,
        ),
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="autopilot.start",
        target_type="autopilot",
        target_id="1",
        outcome="success" if result.get("success") else "blocked",
        details={"mode": "safe"},
    )
    return JSONResponse(result)


@admin_endpoint(write=True)
async def api_autopilot_stop(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    result = context.autopilot.stop()
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="autopilot.stop",
        target_type="autopilot",
        target_id="1",
        outcome="success",
    )
    return JSONResponse(result)


@admin_endpoint(write=True)
async def api_autopilot_run(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    state = context.autopilot.status()
    if state.get("mode") != "safe":
        raise AdminRequestError(409, "Manual beta cycles require SAFE mode")
    _require_operational_company(context, user)
    job = context.jobs.create(name="autopilot.run_cycle", actor=str(user["email"]))
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="autopilot.run_cycle",
        target_type="background_job",
        target_id=job["id"],
        outcome="queued",
        details={"force": True, "mode": "safe"},
    )
    return JSONResponse(
        {"job": job},
        status_code=202,
        background=BackgroundTask(
            context.jobs.run,
            job["id"],
            lambda: context.autopilot.run_cycle(force=True),
            context.crm,
        ),
    )


@admin_endpoint(write=True)
async def api_sheets_sync(
    _request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    job = context.jobs.create(name="google_sheets.sync", actor=str(user["email"]))
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="google_sheets.sync",
        target_type="background_job",
        target_id=job["id"],
        outcome="queued",
    )
    return JSONResponse(
        {"job": job},
        status_code=202,
        background=BackgroundTask(
            context.jobs.run,
            job["id"],
            context.sheets.sync,
            context.crm,
        ),
    )


@admin_endpoint(write=True)
async def api_update_lead(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    lead_id = request.path_params["lead_id"]
    status = str(body.get("status") or "").strip()
    result = context.crm.update_lead_status(lead_id, status)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="lead.update_status",
        target_type="lead",
        target_id=lead_id,
        outcome="success",
        details={"status": status},
    )
    return JSONResponse(result)


@admin_endpoint(write=True)
async def api_approve_draft(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    draft_id = request.path_params["draft_id"]
    draft = context.crm.get_outreach_draft(draft_id)
    expected_fingerprint = _draft_fingerprint(draft)
    supplied_fingerprint = str(body.get("fingerprint") or "")
    if not hmac.compare_digest(expected_fingerprint, supplied_fingerprint):
        raise AdminRequestError(409, "The draft changed; review the exact text again")
    if body.get("confirmation") != "APPROVE":
        raise AdminRequestError(
            409, "Type APPROVE to confirm the exact recipient and text"
        )
    if body.get("recipient") != draft.get("recipient") or body.get(
        "message"
    ) != draft.get("message"):
        raise AdminRequestError(
            409, "Recipient and text must exactly match the saved draft"
        )
    result = context.crm.approve_outreach_draft(draft_id)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="draft.approve_exact",
        target_type="outreach_draft",
        target_id=draft_id,
        outcome="success",
        details={"fingerprint": expected_fingerprint},
    )
    return JSONResponse(_draft_view(result))


@admin_endpoint(write=True)
async def api_create_campaign(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    result = context.crm.create_campaign(
        str(body.get("name") or "").strip(),
        industry=str(body.get("industry") or "").strip() or None,
        location=str(body.get("location") or "").strip() or None,
        search_query=str(body.get("search_query") or "").strip() or None,
        target_count=_bounded_int(
            body.get("target_count", 20), name="target_count", minimum=1, maximum=200
        ),
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="campaign.create",
        target_type="campaign",
        target_id=result["id"],
        outcome="success",
    )
    return JSONResponse(result, status_code=201)


@admin_endpoint(write=True)
async def api_create_vertical(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    result = context.crm.create_vertical(
        str(body.get("name") or "").strip(),
        region=str(body.get("region") or "").strip(),
        search_query=str(body.get("search_query") or "").strip() or None,
        days=body.get("days"),
        daily_target=_bounded_int(
            body.get("daily_target", 10), name="daily_target", minimum=1, maximum=200
        ),
        min_score=_bounded_int(
            body.get("min_score", 65), name="min_score", minimum=0, maximum=100
        ),
        weight=float(body.get("weight", 1.0)),
        enabled=bool(body.get("enabled", True)),
    )
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="vertical.create",
        target_type="vertical",
        target_id=result["id"],
        outcome="success",
    )
    return JSONResponse(result, status_code=201)


@admin_endpoint(write=True)
async def api_update_vertical(
    request: Request, context: AdminContext, user: dict[str, Any]
) -> Response:
    body = await _read_json(request)
    vertical_id = request.path_params["vertical_id"]
    allowed = {
        "name",
        "region",
        "search_query",
        "days",
        "daily_target",
        "min_score",
        "weight",
        "enabled",
    }
    unexpected = set(body) - allowed
    if unexpected:
        raise ValueError(f"Unsupported fields: {', '.join(sorted(unexpected))}")
    result = context.crm.update_vertical(vertical_id, **body)
    context.crm.record_admin_audit(
        actor=str(user["email"]),
        action="vertical.update",
        target_type="vertical",
        target_id=vertical_id,
        outcome="success",
        details={"fields": sorted(body)},
    )
    return JSONResponse(result)


def create_admin_routes(context: AdminContext) -> list[Any]:
    if not STATIC_ROOT.is_dir():
        raise RuntimeError(f"Admin static assets are missing: {STATIC_ROOT}")
    return [
        Route("/admin", admin_home, methods=["GET"]),
        Route("/auth/login", auth_login, methods=["GET"]),
        Route("/auth/callback", auth_callback, methods=["GET"]),
        Route("/auth/handoff", auth_handoff, methods=["GET"]),
        Route("/auth/logout", auth_logout, methods=["POST"]),
        Route("/api/admin/bootstrap", api_bootstrap, methods=["GET"]),
        Route("/api/admin/session", api_session, methods=["GET"]),
        Route("/api/admin/whatsapp/status", api_whatsapp_status, methods=["GET"]),
        Route("/api/admin/whatsapp/qr", api_whatsapp_qr, methods=["GET"]),
        Route("/api/admin/company/profile", api_company_profile, methods=["GET"]),
        Route(
            "/api/admin/company/profile",
            api_update_company_profile,
            methods=["PATCH"],
        ),
        Route(
            "/api/admin/company/onboarding/complete",
            api_complete_company_onboarding,
            methods=["POST"],
        ),
        Route("/api/admin/company/knowledge", api_company_knowledge, methods=["GET"]),
        Route(
            "/api/admin/company/knowledge",
            api_save_company_knowledge,
            methods=["POST"],
        ),
        Route(
            "/api/admin/company/knowledge/{item_id:str}",
            api_archive_company_knowledge,
            methods=["DELETE"],
        ),
        Route("/api/admin/agent/inbox", api_agent_inbox, methods=["GET"]),
        Route("/api/admin/agent/inbox/sync", api_sync_agent_inbox, methods=["POST"]),
        Route(
            "/api/admin/agent/inbox/{event_id:str}",
            api_update_agent_inbox,
            methods=["PATCH"],
        ),
        Route(
            "/api/admin/agent/inbox/{event_id:str}/retry",
            api_retry_agent_inbox,
            methods=["POST"],
        ),
        Route(
            "/api/admin/conversation-agent/settings",
            api_conversation_agent_settings,
            methods=["GET"],
        ),
        Route(
            "/api/admin/conversation-agent/settings",
            api_update_conversation_agent_settings,
            methods=["PATCH"],
        ),
        Route(
            "/api/admin/conversation-agent/sessions",
            api_conversation_sessions,
            methods=["GET"],
        ),
        Route(
            "/api/admin/conversation-agent/process",
            api_process_conversations,
            methods=["POST"],
        ),
        Route("/api/admin/workspace/members", api_workspace_members, methods=["GET"]),
        Route(
            "/api/admin/workspace/invitations",
            api_invite_workspace_member,
            methods=["POST"],
        ),
        Route(
            "/api/admin/workspace/members/{member_id:str}",
            api_update_workspace_member,
            methods=["PATCH"],
        ),
        Route("/api/admin/leads", api_leads, methods=["GET"]),
        Route("/api/admin/leads/{lead_id:str}", api_update_lead, methods=["PATCH"]),
        Route("/api/admin/campaigns", api_campaigns, methods=["GET"]),
        Route("/api/admin/campaigns", api_create_campaign, methods=["POST"]),
        Route("/api/admin/drafts", api_drafts, methods=["GET"]),
        Route(
            "/api/admin/drafts/{draft_id:str}/approve",
            api_approve_draft,
            methods=["POST"],
        ),
        Route("/api/admin/verticals", api_verticals, methods=["GET"]),
        Route("/api/admin/verticals", api_create_vertical, methods=["POST"]),
        Route(
            "/api/admin/verticals/{vertical_id:str}",
            api_update_vertical,
            methods=["PATCH"],
        ),
        Route("/api/admin/audit", api_audit, methods=["GET"]),
        Route("/api/admin/jobs", api_jobs, methods=["GET"]),
        Route("/api/admin/autopilot/start", api_autopilot_start, methods=["POST"]),
        Route("/api/admin/autopilot/stop", api_autopilot_stop, methods=["POST"]),
        Route("/api/admin/autopilot/run", api_autopilot_run, methods=["POST"]),
        Route("/api/admin/sheets/sync", api_sheets_sync, methods=["POST"]),
        Route("/api/v1/bootstrap", api_bootstrap, methods=["GET"]),
        Route("/api/v1/session", api_session, methods=["GET"]),
        Route("/api/v1/whatsapp/status", api_whatsapp_status, methods=["GET"]),
        Route("/api/v1/whatsapp/qr", api_whatsapp_qr, methods=["GET"]),
        Route("/api/v1/company/profile", api_company_profile, methods=["GET"]),
        Route(
            "/api/v1/company/profile",
            api_update_company_profile,
            methods=["PATCH"],
        ),
        Route(
            "/api/v1/company/onboarding/complete",
            api_complete_company_onboarding,
            methods=["POST"],
        ),
        Route("/api/v1/company/knowledge", api_company_knowledge, methods=["GET"]),
        Route(
            "/api/v1/company/knowledge",
            api_save_company_knowledge,
            methods=["POST"],
        ),
        Route(
            "/api/v1/company/knowledge/{item_id:str}",
            api_archive_company_knowledge,
            methods=["DELETE"],
        ),
        Route("/api/v1/agent/inbox", api_agent_inbox, methods=["GET"]),
        Route("/api/v1/agent/inbox/sync", api_sync_agent_inbox, methods=["POST"]),
        Route(
            "/api/v1/agent/inbox/{event_id:str}",
            api_update_agent_inbox,
            methods=["PATCH"],
        ),
        Route(
            "/api/v1/agent/inbox/{event_id:str}/retry",
            api_retry_agent_inbox,
            methods=["POST"],
        ),
        Route(
            "/api/v1/conversation-agent/settings",
            api_conversation_agent_settings,
            methods=["GET"],
        ),
        Route(
            "/api/v1/conversation-agent/settings",
            api_update_conversation_agent_settings,
            methods=["PATCH"],
        ),
        Route(
            "/api/v1/conversation-agent/sessions",
            api_conversation_sessions,
            methods=["GET"],
        ),
        Route(
            "/api/v1/conversation-agent/process",
            api_process_conversations,
            methods=["POST"],
        ),
        Route("/api/v1/workspace/members", api_workspace_members, methods=["GET"]),
        Route(
            "/api/v1/workspace/invitations",
            api_invite_workspace_member,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workspace/members/{member_id:str}",
            api_update_workspace_member,
            methods=["PATCH"],
        ),
        Route("/api/v1/leads", api_leads, methods=["GET"]),
        Route("/api/v1/leads/{lead_id:str}", api_update_lead, methods=["PATCH"]),
        Route("/api/v1/campaigns", api_campaigns, methods=["GET"]),
        Route("/api/v1/campaigns", api_create_campaign, methods=["POST"]),
        Route("/api/v1/drafts", api_drafts, methods=["GET"]),
        Route(
            "/api/v1/drafts/{draft_id:str}/approve",
            api_approve_draft,
            methods=["POST"],
        ),
        Route("/api/v1/verticals", api_verticals, methods=["GET"]),
        Route("/api/v1/verticals", api_create_vertical, methods=["POST"]),
        Route(
            "/api/v1/verticals/{vertical_id:str}",
            api_update_vertical,
            methods=["PATCH"],
        ),
        Route("/api/v1/audit", api_audit, methods=["GET"]),
        Route("/api/v1/jobs", api_jobs, methods=["GET"]),
        Route("/api/v1/autopilot/start", api_autopilot_start, methods=["POST"]),
        Route("/api/v1/autopilot/stop", api_autopilot_stop, methods=["POST"]),
        Route("/api/v1/autopilot/run", api_autopilot_run, methods=["POST"]),
        Route("/api/v1/sheets/sync", api_sheets_sync, methods=["POST"]),
        Mount(
            "/oauth-assets",
            app=StaticFiles(directory=OAUTH_STATIC_ROOT),
            name="oauth-assets",
        ),
        Mount("/assets", app=StaticFiles(directory=STATIC_ROOT), name="admin-assets"),
    ]
