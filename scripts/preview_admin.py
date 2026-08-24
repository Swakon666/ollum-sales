"""Loopback-only visual preview for the closed-beta dashboard.

This process serves synthetic data and never opens the CRM or WhatsApp bridge. It is
intentionally unsuitable for production and rejects every mutation.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
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

COMPANY_ONBOARDING = {
    "profile": {
        "workspace_id": "ollum-group",
        "company_name": "Ollum Group",
        "website_url": "https://ollumgroup.ru",
        "industry": "Digital production",
        "geography": "Россия и worldwide",
        "positioning": "Digital-студия полного цикла",
        "target_customer": "Компании, которым нужен собственный канал продаж",
        "sales_process": "Диагностика, план проекта, предложение, запуск",
        "tone_of_voice": "Коротко, конкретно, без неподтверждённых обещаний",
        "primary_goal": "Квалифицированные обращения",
        "constraints": "Не раскрывать данные клиентов под NDA",
        "language": "ru",
        "onboarding_status": "in_progress",
        "revision": 7,
        "updated_at": "2026-08-23T10:20:00+00:00",
    },
    "knowledge_counts": {"service": 2, "price": 1, "case": 1},
    "completion_percent": 92,
    "ready_for_sales": True,
    "missing": ["active_clients"],
    "next_questions": [
        {
            "id": "pipeline",
            "prompt": "Какие клиенты сейчас в работе и как устроены стадии продажи?",
            "accepts": ["free_text", "file"],
        }
    ],
    "onboarding_status": "in_progress",
}

COMPANY_KNOWLEDGE = [
    {
        "id": "knowledge-service",
        "category": "service",
        "title": "Разработка сайтов",
        "content": {"description": "Стратегия, UX/UI, разработка и запуск под ключ"},
        "source_type": "chat",
        "status": "active",
    },
    {
        "id": "knowledge-price",
        "category": "price",
        "title": "Правило расчёта",
        "content": {
            "description": "Точная стоимость определяется после диагностики задачи"
        },
        "source_type": "file",
        "source_name": "Прайс.pdf",
        "status": "active",
    },
]


def preview_time(*, minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds"
    )


AGENT_INBOX = [
    {
        "id": "inbox-1",
        "chat_jid": "79990000001@s.whatsapp.net",
        "sender_label": "Тестовый клиент",
        "message_text": "Подскажите, с чего начинается работа и как оценивается проект?",
        "received_at": preview_time(minutes_ago=18),
        "status": "new",
        "lead_id": "lead-1",
        "age_minutes": 18,
        "response_sla_minutes": 60,
        "sla_state": "on_track",
        "retryable": False,
        "retry_block_reason": "event_not_in_review",
    },
    {
        "id": "inbox-2",
        "chat_jid": "79990000002@s.whatsapp.net",
        "sender_label": "Клиент на проверке",
        "message_text": "Можно получить примеры похожих проектов?",
        "received_at": preview_time(minutes_ago=75),
        "status": "needs_review",
        "lead_id": "lead-2",
        "age_minutes": 75,
        "response_sla_minutes": 60,
        "sla_state": "overdue",
        "retryable": True,
        "retry_block_reason": None,
        "agent_attempts": 3,
        "agent_error": "Ответ требует повторной проверки подтверждённых фактов",
    },
    {
        "id": "inbox-3",
        "chat_jid": "79990000003@s.whatsapp.net",
        "sender_label": "Новый контакт",
        "message_text": "Сможете оценить задачу по короткому описанию?",
        "received_at": preview_time(minutes_ago=12),
        "status": "needs_review",
        "lead_id": None,
        "age_minutes": 12,
        "response_sla_minutes": 60,
        "sla_state": "on_track",
        "retryable": False,
        "retry_block_reason": "lead_not_linked",
    },
]

AGENT_INBOX_SUMMARY = {
    "new": 1,
    "acknowledged": 0,
    "processing": 0,
    "drafted": 0,
    "needs_review": 2,
    "resolved": 0,
    "ignored": 0,
    "total": 3,
    "processing_active": 0,
    "processing_expired": 0,
    "stale_actionable": 0,
    "sla_overdue": 1,
    "response_sla_minutes": 60,
    "oldest_open_minutes": 75,
}

CONVERSATION_AGENT = {
    "settings": {
        "enabled": True,
        "autonomy_mode": "draft",
        "niche": "auto",
        "objective": "Квалифицировать запрос и подготовить следующий полезный ответ",
        "tone": "Коротко, конкретно и доброжелательно",
        "instructions": "Опирайся только на память компании и подтверждённый контекст",
        "qualification_questions": ["Какая задача?", "Какой ориентир по срокам?"],
        "forbidden_topics": ["Неподтверждённые гарантии"],
        "escalation_rules": ["Передать человеку договорные и платёжные вопросы"],
        "max_context_messages": 12,
        "max_reply_chars": 700,
        "max_inbound_age_hours": 168,
        "response_sla_minutes": 60,
        "confidence_threshold": 65,
        "auto_create_inbound_leads": True,
        "send_enabled": False,
    },
    "summary": {"inbox": AGENT_INBOX_SUMMARY, "active_sessions": 1},
    "runtime": {
        "ready": True,
        "health": "degraded",
        "health_reasons": ["response_sla_breached"],
        "company_ready": True,
    },
    "safety": {"safe_mode": True, "send_enabled": False},
}

CONVERSATION_SESSIONS = [
    {
        "id": "session-1",
        "lead_id": "lead-1",
        "external_chat_id": "79990000001@s.whatsapp.net",
        "stage": "qualification",
        "intent": "website_project",
        "summary": "Клиент уточняет процесс оценки проекта.",
        "next_action": "Уточнить задачу и желаемый срок запуска",
        "turn_count": 2,
        "escalation_status": "none",
        "updated_at": preview_time(minutes_ago=8),
    }
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
        "company_onboarding": COMPANY_ONBOARDING,
        "agent_inbox": AGENT_INBOX_SUMMARY,
        "conversation_agent": CONVERSATION_AGENT,
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
        return JSONResponse(
            {"error": "Preview mutations are disabled"}, status_code=405
        )
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
    if path == "/api/v1/company/profile":
        return JSONResponse(COMPANY_ONBOARDING)
    if path == "/api/v1/company/knowledge":
        return JSONResponse(COMPANY_KNOWLEDGE)
    if path == "/api/v1/agent/inbox":
        return JSONResponse(AGENT_INBOX)
    if path == "/api/v1/conversation-agent/sessions":
        return JSONResponse(CONVERSATION_SESSIONS)
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
