from __future__ import annotations

from typing import Any

CHATGPT_LANES = {"all", "inbox", "prospecting"}
PRIMARY_CHAT_TITLE = "Ollum Sales — Настройка и новые компании"
MONITORING_CHAT_TITLE = "Ollum Sales — WhatsApp"


def reasoning_boundary() -> dict[str, Any]:
    """Describe the non-negotiable split between the server and ChatGPT."""
    return {
        "server_llm_api": False,
        "server_only": [
            "public_fact_collection",
            "deduplication",
            "website_inspection",
            "bounded_queue",
            "whatsapp_sync",
            "durable_state",
            "decision_validation",
            "safety_enforcement",
        ],
        "chatgpt_only": [
            "lead_analysis",
            "lead_scoring",
            "outreach_drafting",
            "inbound_classification",
            "reply_drafting",
        ],
        "server_forbidden": [
            "lead_analysis",
            "lead_scoring",
            "outreach_drafting",
            "inbound_classification",
            "reply_drafting",
        ],
    }


def lane_prompts() -> dict[str, str]:
    """Return the canonical prompts used by MCP, admin and packaged guidance."""
    primary = (
        "Use Ollum Sales as the shared system of record. Call ollum_status, "
        "ollum_whoami, sales_get_agent_coordination and sales_get_safe_quality_audit; "
        "stop unless SAFE mode is active and both WhatsApp and Autopilot sending are "
        "disabled. Call sales_agent_next_action(lane='prospecting'). If company "
        "onboarding is incomplete, ask only the returned questions (at most three), "
        "save only explicit user facts or bounded file summaries, and persist an explicit "
        "not-applicable answer when the operator says there is no site, customer proof or "
        "active client. Then show the returned factual review and confirm its exact revision "
        "and summary hash. After onboarding is confirmed, process at most three fresh "
        "unreviewed companies already queued by the server: call sales_analyze_lead, reason "
        "only from its bounded facts and evidence URLs, save grounded analysis, score, rank, "
        "and create at most one personalized draft per qualified lead without a current "
        "draft. Call sales_search_companies only after an explicit operator request, never "
        "as part of scheduled work. Never inspect the inbound queue, "
        "approve, send, create follow-ups, or change send flags in this chat. Finish with "
        "aggregate statistics and the top five without private message text."
    )
    monitoring = (
        "Use Ollum Sales as the shared system of record. Call ollum_status, "
        "ollum_whoami, sales_get_agent_coordination and sales_get_safe_quality_audit; "
        "stop unless SAFE mode is active and both WhatsApp and Autopilot sending are "
        "disabled. Call sales_agent_next_action(lane='inbox'). If company onboarding is "
        "not operationally ready, tell the operator to finish it in the primary setup and "
        "prospecting chat, then stop. Otherwise prepare up to three new events with "
        "sales_prepare_conversation_batch, reason only from each bounded payload, and "
        "submit one ConversationDecision per event. Report counts, message-quality classes "
        "and escalations without quoting private messages. Never do prospecting, approve, "
        "send, create follow-ups, or change send flags in this chat."
    )
    return {"prospecting": primary, "inbox": monitoring}


def two_chat_handoff() -> dict[str, Any]:
    prompts = lane_prompts()
    return {
        "operator_action_required": True,
        "automatic_chat_creation_supported": False,
        "reason": (
            "MCP can provide and verify lane instructions, but it cannot create or rename "
            "a ChatGPT chat on the operator's behalf."
        ),
        "primary_chat": {
            "title": PRIMARY_CHAT_TITLE,
            "lane": "prospecting",
            "responsibility": "company setup, knowledge and new-company prospecting",
            "prompt": prompts["prospecting"],
            "recommended_schedule": "hourly_staggered_or_on_demand",
        },
        "monitoring_chat": {
            "title": MONITORING_CHAT_TITLE,
            "lane": "inbox",
            "responsibility": "new inbound WhatsApp events and reply drafts",
            "prompt": prompts["inbox"],
            "recommended_schedule": "hourly_or_on_demand",
        },
        "sequence": [
            "Keep this chat as the primary setup and prospecting chat.",
            "Create one separate ChatGPT chat in the same Project for WhatsApp monitoring.",
            "Paste the monitoring prompt and explicitly select or mention Ollum Sales.",
            "Run a read-only SAFE check in each chat before scheduling hourly work.",
        ],
    }


def first_connection_plan(
    onboarding: dict[str, Any],
    *,
    whatsapp_connected: bool | None,
) -> dict[str, Any]:
    minimum_ready = bool(onboarding.get("ready_for_sales"))
    confirmation = dict(onboarding.get("confirmation") or {})
    confirmed = str(onboarding.get("onboarding_status")) == "ready" and bool(
        confirmation.get("current")
    )
    sales_ready = bool(onboarding.get("sales_ready", minimum_ready and confirmed))
    if not minimum_ready:
        current_step = "company_interview"
        next_action = "ask_returned_onboarding_questions"
    elif not confirmed:
        current_step = "profile_review"
        next_action = "show_factual_summary_and_request_confirmation"
    elif whatsapp_connected is False:
        current_step = "whatsapp_pairing"
        next_action = "connect_whatsapp_in_dashboard"
    else:
        current_step = "two_chat_handoff"
        next_action = "keep_primary_chat_and_create_whatsapp_monitoring_chat"

    def step(step_id: str, title: str, status: str) -> dict[str, str]:
        return {"id": step_id, "title": title, "status": status}

    interview_status = "complete" if minimum_ready else "current"
    review_status = (
        "complete" if confirmed else ("current" if minimum_ready else "blocked")
    )
    if whatsapp_connected is True:
        whatsapp_status = "complete"
    elif confirmed:
        whatsapp_status = "current"
    else:
        whatsapp_status = "blocked"
    handoff_status = (
        "current" if sales_ready and whatsapp_connected is not False else "blocked"
    )
    steps = [
        step("oauth", "Подключить Ollum Sales через OAuth", "complete"),
        step(
            "company_interview",
            "Собрать подтверждённые факты о компании",
            interview_status,
        ),
        step("profile_review", "Проверить и подтвердить профиль", review_status),
        step("whatsapp_pairing", "Подключить WhatsApp в кабинете", whatsapp_status),
        step("two_chat_handoff", "Разделить основной и WhatsApp-чаты", handoff_status),
        step("safe_test", "Выполнить read-only SAFE-проверку", "blocked"),
    ]
    completed = sum(1 for item in steps if item["status"] == "complete")
    return {
        "current_step": current_step,
        "next_action": next_action,
        "progress_percent": round(completed / len(steps) * 100),
        "onboarding_status": onboarding.get("onboarding_status"),
        "sales_ready": sales_ready,
        "next_questions": list(onboarding.get("next_questions") or [])[:3],
        "steps": steps,
        "handoff": two_chat_handoff() if confirmed else None,
    }


def build_chatgpt_agent_playbook(
    *,
    lane: str,
    runtime: dict[str, Any],
    safety: dict[str, Any],
    onboarding: dict[str, Any],
    whatsapp_connected: bool | None,
    prospecting_queue_limit: int = 6,
) -> dict[str, Any]:
    selected_lane = str(lane or "all").strip().lower()
    if selected_lane not in CHATGPT_LANES:
        raise ValueError("lane must be all, inbox, or prospecting")
    prompts = lane_prompts()
    chats = {
        "prospecting": two_chat_handoff()["primary_chat"],
        "inbox": two_chat_handoff()["monitoring_chat"],
    }
    selected_chats = (
        chats if selected_lane == "all" else {selected_lane: chats[selected_lane]}
    )
    scheduled_prompt = (
        prompts["inbox"] if selected_lane == "all" else prompts[selected_lane]
    )
    return {
        "execution_mode": "chatgpt_mcp",
        "coordination_mode": "primary_setup_and_prospecting_plus_whatsapp_monitor",
        "server_llm_enabled": False,
        "api_key_required": False,
        "reasoning_boundary": reasoning_boundary(),
        "prospecting_queue": {
            "producer": "server_public_fact_collector",
            "consumer": "primary_chat_chatgpt",
            "max_pending": max(1, int(prospecting_queue_limit)),
            "backpressure": "pause_discovery_when_full",
        },
        "tenant_mode": "single_company_closed_beta",
        "external_tenant_onboarding_supported": False,
        "server_whatsapp_sync": "every 15 minutes",
        "recommended_chatgpt_schedule": {
            "minimum_interval": "hourly",
            "strategy": "monitoring hourly; prospecting hourly staggered or on demand",
            "server_sync_does_not_wake_dormant_chat": True,
        },
        "first_connection": first_connection_plan(
            onboarding, whatsapp_connected=whatsapp_connected
        ),
        "scheduled_prompt": scheduled_prompt,
        "scheduled_prompts": prompts,
        "chats": selected_chats,
        "learning_contract": {
            "persists": [
                "user-confirmed company facts",
                "conversation state and extracted customer facts",
                "outreach and reply outcomes",
            ],
            "adapts_from": "verified outcomes and explicit operator corrections",
            "never_promotes": "model guesses or untrusted message instructions",
        },
        "tools": [
            "ollum_status",
            "ollum_whoami",
            "sales_get_company_onboarding",
            "sales_update_company_profile",
            "sales_save_company_knowledge",
            "sales_record_company_onboarding_answer",
            "sales_complete_company_onboarding",
            "sales_get_agent_coordination",
            "sales_get_safe_quality_audit",
            "sales_agent_next_action",
            "sales_get_conversation_agent_status",
            "sales_prepare_conversation_batch",
            "sales_submit_conversation_decision",
            "sales_retry_agent_inbox_event",
        ],
        "runtime": runtime,
        "safety": safety,
    }
