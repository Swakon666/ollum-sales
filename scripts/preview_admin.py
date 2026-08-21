"""Loopback-only visual preview for the closed-beta dashboard.

This process serves synthetic data and never opens the CRM or WhatsApp bridge. It is
intentionally unsuitable for production and rejects every mutation.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "admin_static"

USER = {
    "member_id": "preview-owner",
    "email": "owner@ollumgroup.ru",
    "name": "Ollum Owner",
    "workspace_id": "ollum-group",
    "workspace_name": "Ollum Group",
    "role": "owner",
    "csrf": "preview-only",
    "capabilities": {"read": True, "write": True, "manage_members": True},
}

LEADS = [
    {
        "id": "lead-1",
        "company_name": "Northstar Logistics",
        "industry": "Logistics",
        "location": "Moscow",
        "website_url": "https://example.com",
        "status": "qualified",
        "score": 82,
        "evidence_expires_at": "2099-01-01T00:00:00+00:00",
        "updated_at": "2026-08-21T10:10:00+00:00",
    },
    {
        "id": "lead-2",
        "company_name": "Meridian Clinic",
        "industry": "Healthcare",
        "location": "Kazan",
        "website_url": "https://example.org",
        "status": "analyzed",
        "score": 73,
        "evidence_expires_at": "2099-01-01T00:00:00+00:00",
        "updated_at": "2026-08-21T09:20:00+00:00",
    },
]

MEMBERS = [
    {
        "id": "preview-owner",
        "email": USER["email"],
        "display_name": USER["name"],
        "role": "owner",
        "status": "active",
        "last_login_at": "2026-08-21T10:30:00+00:00",
    },
    {
        "id": "preview-viewer",
        "email": "tester@ollumgroup.ru",
        "display_name": "Beta Tester",
        "role": "viewer",
        "status": "active",
        "last_login_at": "2026-08-21T09:45:00+00:00",
    },
]


def bootstrap() -> dict[str, Any]:
    whatsapp = {
        "reachable": True,
        "ready": False,
        "connected": False,
        "logged_in": False,
        "send_enabled": False,
        "account_jid": None,
    }
    return {
        "user": USER,
        "workspace": {
            "id": "ollum-group",
            "name": "Ollum Group",
            "status": "active",
            "active_members": 2,
            "pending_invitations": 1,
        },
        "overview": {
            "lead_count": 24,
            "average_score": 68.4,
            "top_score": 82,
            "by_status": {"new": 9, "analyzed": 8, "qualified": 5, "drafted": 2},
        },
        "crm": {"leads": 24, "outreach_drafts": 2},
        "autopilot": {
            "running": True,
            "mode": "safe",
            "interval_minutes": 45,
            "max_verticals_per_cycle": 2,
            "leads_per_vertical": 10,
            "score_threshold": 65,
            "pending_send_requests": 0,
            "next_cycle_at": "2026-08-21T11:15:00+00:00",
        },
        "whatsapp": whatsapp,
        "whatsapp_pairing": {
            "state": "waiting_for_qr",
            "needs_pairing": True,
            "has_qr": False,
            "generation": 7,
        },
        "google_sheets": {"enabled": True, "status": "ready"},
        "top_leads": LEADS,
        "campaigns": [
            {
                "id": "campaign-1",
                "name": "Logistics RU",
                "industry": "Logistics",
                "location": "Russia",
                "status": "active",
                "lead_count": 14,
                "created_at": "2026-08-20T08:00:00+00:00",
            }
        ],
        "drafts": [],
        "verticals": [
            {
                "id": "vertical-1",
                "name": "Logistics",
                "region": "Russia",
                "min_score": 65,
                "weight": 1,
                "enabled": True,
            }
        ],
        "cycles": [
            {
                "id": "cycle-1",
                "started_at": "2026-08-21T09:00:00+00:00",
                "mode": "safe",
                "status": "completed",
                "selected_verticals": ["Logistics"],
                "error": None,
            }
        ],
        "audit": [
            {
                "created_at": "2026-08-21T10:30:00+00:00",
                "actor": USER["email"],
                "action": "admin.login",
                "target_type": None,
                "target_id": None,
                "outcome": "success",
            }
        ],
        "jobs": [
            {
                "name": "SAFE Autopilot cycle",
                "status": "completed",
                "created_at": "2026-08-21T09:00:00+00:00",
            }
        ],
        "plugin": {
            "name": "Ollum Sales",
            "description": "Grounded sales research and SAFE drafting.",
            "server_url": "https://mcp.ollumgroup.ru/mcp",
            "dashboard_url": "https://api.ollumgroup.ru",
            "authentication": "OAuth",
            "authorization_server": "https://login.example/",
            "protected_resource_metadata": "https://mcp.ollumgroup.ru/.well-known/oauth-protected-resource/mcp",
            "checks": {
                "https_resource_url": True,
                "oidc_mode": True,
                "issuer_configured": True,
                "audience_configured": True,
                "admin_client_configured": True,
                "beta_allowlist_configured": True,
                "session_secret_configured": True,
            },
            "ready": True,
        },
        "safety": {
            "safe_mode": True,
            "whatsapp_send_enabled": False,
            "autopilot_send_enabled": False,
            "send_controls_exposed": False,
        },
    }


async def index(_request) -> Response:
    return FileResponse(STATIC_ROOT / "index.html")


async def preview_api(request) -> Response:
    if request.method != "GET":
        return JSONResponse({"error": "Preview mutations are disabled"}, status_code=405)
    path = request.url.path
    payload = bootstrap()
    if path == "/api/v1/bootstrap":
        return JSONResponse(payload)
    if path == "/api/v1/leads":
        return JSONResponse(LEADS)
    if path == "/api/v1/campaigns":
        return JSONResponse(payload["campaigns"])
    if path == "/api/v1/drafts":
        return JSONResponse([])
    if path == "/api/v1/workspace/members":
        return JSONResponse(
            {
                "workspace": payload["workspace"],
                "members": MEMBERS,
                "invitations": [
                    {
                        "id": "invite-1",
                        "email": "new.tester@ollumgroup.ru",
                        "role": "viewer",
                        "created_at": "2026-08-21T10:00:00+00:00",
                        "expires_at": "2026-08-28T10:00:00+00:00",
                    }
                ],
            }
        )
    if path == "/api/v1/whatsapp/status":
        return JSONResponse(
            {
                "bridge": payload["whatsapp"],
                "pairing": payload["whatsapp_pairing"],
                "send_enabled": False,
            }
        )
    return JSONResponse({"error": "Unknown preview endpoint"}, status_code=404)


app = Starlette(
    routes=[
        Route("/", index),
        Route("/admin", index),
        Route("/api/v1/{path:path}", preview_api, methods=["GET", "POST", "PATCH"]),
        Mount("/assets", StaticFiles(directory=STATIC_ROOT), name="assets"),
    ]
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
