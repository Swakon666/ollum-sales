from __future__ import annotations

from app.chatgpt_playbook import build_chatgpt_agent_playbook, first_connection_plan


def _onboarding(*, status: str = "ready", ready: bool = True) -> dict:
    return {
        "onboarding_status": status,
        "ready_for_sales": ready,
        "sales_ready": status == "ready" and ready,
        "confirmation": {"current": status == "ready" and ready},
        "next_questions": [],
    }


def test_selected_lane_gets_its_own_scheduled_prompt() -> None:
    common = {
        "runtime": {"ready": True},
        "safety": {"sends": False},
        "onboarding": _onboarding(),
        "whatsapp_connected": True,
    }

    prospecting = build_chatgpt_agent_playbook(lane="prospecting", **common)
    inbox = build_chatgpt_agent_playbook(lane="inbox", **common)

    assert "lane='prospecting'" in prospecting["scheduled_prompt"]
    assert "lane='inbox'" not in prospecting["scheduled_prompt"]
    assert "lane='inbox'" in inbox["scheduled_prompt"]
    assert prospecting["scheduled_prompt"] != inbox["scheduled_prompt"]
    assert "sales_search_companies" in prospecting["scheduled_prompt"]
    assert (
        "do not wait for a separate operator request" in prospecting["scheduled_prompt"]
    )
    assert "already queued by the server" not in prospecting["scheduled_prompt"]
    assert "never as part of scheduled work" not in prospecting["scheduled_prompt"]

    boundary = prospecting["reasoning_boundary"]
    assert boundary["server_llm_api"] is False
    assert "public_search_execution" in boundary["server_only"]
    assert "search_strategy" in boundary["chatgpt_only"]
    assert "query_formulation" in boundary["chatgpt_only"]
    assert "lead_analysis" in boundary["chatgpt_only"]
    assert "lead_scoring" in boundary["chatgpt_only"]
    assert "outreach_drafting" in boundary["chatgpt_only"]
    assert "lead_analysis" in boundary["server_forbidden"]
    assert "autonomous_company_discovery" in boundary["server_forbidden"]
    assert prospecting["prospecting_queue"]["max_pending"] == 6
    assert (
        prospecting["prospecting_queue"]["producer"]
        == "primary_chat_chatgpt_via_sales_search_companies"
    )
    assert prospecting["prospecting_queue"]["server_autonomous_discovery"] is False


def test_first_connection_moves_from_interview_to_review_and_handoff() -> None:
    interview = first_connection_plan(
        _onboarding(status="in_progress", ready=False),
        whatsapp_connected=False,
    )
    review = first_connection_plan(
        _onboarding(status="in_progress", ready=True),
        whatsapp_connected=False,
    )
    pairing = first_connection_plan(_onboarding(), whatsapp_connected=False)
    handoff = first_connection_plan(_onboarding(), whatsapp_connected=True)

    assert interview["current_step"] == "company_interview"
    assert review["current_step"] == "profile_review"
    assert pairing["current_step"] == "whatsapp_pairing"
    assert handoff["current_step"] == "two_chat_handoff"
    assert handoff["handoff"]["primary_chat"]["lane"] == "prospecting"
    assert handoff["handoff"]["monitoring_chat"]["lane"] == "inbox"
    assert handoff["handoff"]["automatic_chat_creation_supported"] is False
