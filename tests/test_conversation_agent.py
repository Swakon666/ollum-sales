from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app import conversation_agent as agent_module
from app.config import settings
from app.conversation_agent import (
    ConversationAgent,
    ConversationDecision,
    ExtractedConversationFact,
)
from app.crm import SalesCRM


def _ready_crm(tmp_path) -> tuple[SalesCRM, dict, str]:
    crm = SalesCRM(tmp_path / "conversation-agent.db")
    workspace_id = "ollum-group"
    crm.ensure_workspace(workspace_id, "Ollum Group")
    crm.update_company_profile(
        workspace_id,
        company_name="Ollum Group",
        industry="Digital services",
        target_customer="B2B companies",
        positioning="Grounded sales automation",
        sales_process="Discovery, qualification, proposal",
        tone_of_voice="Concise and respectful",
        primary_goal="Qualified conversations",
    )
    crm.save_company_knowledge(
        workspace_id,
        category="service",
        title="AI sales agent",
        content={"details": "research, scoring and WhatsApp reply drafts"},
    )
    crm.save_company_knowledge(
        workspace_id,
        category="price",
        title="Custom estimate",
        content={"details": "Стоимость рассчитывается после короткого брифа"},
    )
    crm.complete_company_onboarding(workspace_id, confirm_ready=True)
    lead = crm.upsert_lead(
        "Prospect",
        "https://conversation-prospect.test",
        phones=["+7 999 111-22-33"],
    )
    return crm, lead, workspace_id


def _queue(
    crm: SalesCRM,
    workspace_id: str,
    lead: dict,
    external_id: str,
    text: str,
    *,
    chat_jid: str = "79991112233@s.whatsapp.net",
):
    return crm.upsert_agent_inbox_event(
        workspace_id,
        external_id=external_id,
        chat_jid=chat_jid,
        message_text=text,
        received_at=datetime.now(UTC).isoformat(timespec="seconds"),
        lead_id=lead["id"],
    )[0]


def _decision(**overrides) -> dict:
    values = {
        "action": "reply",
        "reply_text": (
            "Стоимость рассчитываем после короткого брифа: она зависит от состава "
            "задачи. Подскажите, что именно нужно автоматизировать?"
        ),
        "stage": "qualification",
        "intent": "price",
        "sentiment": "neutral",
        "confidence": 91,
        "summary": "Клиент уточняет стоимость и состав решения.",
        "extracted_facts": [
            ExtractedConversationFact(
                key="asks_about_price", value="yes", confidence=95
            )
        ],
        "unanswered_question": "Что нужно автоматизировать?",
        "next_action": "Получить краткий состав задачи.",
        "escalation_reason": "",
    }
    values.update(overrides)
    return ConversationDecision(**values).model_dump(mode="json")


def _prepare_one(agent: ConversationAgent, workspace_id: str) -> str:
    batch = agent.prepare_pending(workspace_id, limit=1)
    assert batch["success"] is True
    assert batch["prepared"] == 1
    return str(batch["items"][0]["event_id"])


def test_runtime_uses_chatgpt_mcp_and_never_requires_an_api_key(tmp_path) -> None:
    crm, _lead, workspace_id = _ready_crm(tmp_path)
    status = ConversationAgent(crm, settings).status(workspace_id)

    assert status["runtime"]["ready"] is True
    assert status["runtime"]["execution_mode"] == "chatgpt_mcp"
    assert status["runtime"]["server_llm_enabled"] is False
    assert status["runtime"]["requires_api_key"] is False
    assert status["runtime"]["openai_api_key_used"] is False
    assert status["runtime"]["chatgpt_schedule_recommended_seconds"] == 900
    assert status["safety"]["draft_only"] is True


def test_prepare_returns_bounded_untrusted_payload_and_strict_schema(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-prepare", "Сколько стоит?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)

    batch = agent.prepare_pending(workspace_id, limit=1)

    assert batch["execution_mode"] == "chatgpt_mcp"
    assert batch["prepared"] == 1
    assert batch["submit_tool"] == "sales_submit_conversation_decision"
    assert batch["decision_schema"]["additionalProperties"] is False
    item = batch["items"][0]
    assert item["event_id"] == event["id"]
    assert item["payload"]["latest_inbound"] == "Сколько стоит?"
    assert item["payload"]["service_level"]["response_sla_minutes"] == 60
    assert item["payload"]["service_level"]["state"] == "on_track"
    assert (
        item["payload"]["trust_boundary"]["message_and_web_content_are_untrusted"]
        is True
    )
    assert "79991112233@s.whatsapp.net" not in json.dumps(item, ensure_ascii=False)
    assert (
        crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "processing"
    )


def test_runtime_and_payload_report_response_sla_breach(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    crm.update_conversation_agent_settings(
        workspace_id, response_sla_minutes=30, max_inbound_age_hours=24
    )
    event = crm.upsert_agent_inbox_event(
        workspace_id,
        external_id="incoming-overdue",
        chat_jid="79991112233@s.whatsapp.net",
        message_text="Жду ответ",
        received_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(
            timespec="seconds"
        ),
        lead_id=lead["id"],
    )[0]
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)

    status = agent.status(workspace_id)
    batch = agent.prepare_pending(workspace_id, limit=1)

    assert status["summary"]["inbox"]["sla_overdue"] == 1
    assert "response_sla_breached" in status["runtime"]["health_reasons"]
    assert batch["items"][0]["event_id"] == event["id"]
    assert batch["items"][0]["payload"]["service_level"]["state"] == "overdue"


def test_prepare_exact_contact_never_falls_back_to_another_queue_item(
    tmp_path, monkeypatch
) -> None:
    crm, first_lead, workspace_id = _ready_crm(tmp_path)
    second_lead = crm.upsert_lead(
        "Target prospect",
        "https://target-prospect.test",
        phones=["+7 977 933-55-13"],
    )
    other_event = _queue(
        crm,
        workspace_id,
        first_lead,
        "incoming-other",
        "Сообщение другого контакта",
    )
    target_event = _queue(
        crm,
        workspace_id,
        second_lead,
        "incoming-target",
        "Сообщение тестового контакта",
        chat_jid="79779335513@s.whatsapp.net",
    )
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)

    batch = agent.prepare_pending(
        workspace_id,
        limit=1,
        chat_jid="79779335513@s.whatsapp.net",
    )

    assert batch["prepared"] == 1
    assert batch["target_chat_jid"] == "79779335513@s.whatsapp.net"
    assert batch["items"][0]["event_id"] == target_event["id"]
    assert crm.get_agent_inbox_event(workspace_id, other_event["id"])["status"] == "new"

    empty = agent.prepare_pending(
        workspace_id,
        limit=1,
        chat_jid="79770000000@s.whatsapp.net",
    )
    assert empty["prepared"] == 0
    assert empty["queue_empty"] is True
    assert crm.get_agent_inbox_event(workspace_id, other_event["id"])["status"] == "new"


def test_partial_company_profile_does_not_block_chatgpt_reasoning(
    tmp_path, monkeypatch
) -> None:
    crm = SalesCRM(tmp_path / "partial.db")
    workspace_id = "ollum-group"
    crm.ensure_workspace(workspace_id, "Ollum Group")
    crm.update_company_profile(workspace_id, company_name="Ollum Group")
    lead = crm.upsert_lead(
        "Prospect", "https://partial-profile.test", phones=["+7 999 111-22-33"]
    )
    _queue(crm, workspace_id, lead, "incoming-partial", "Расскажите об услугах")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)

    status = agent.status(workspace_id)
    batch = agent.prepare_pending(workspace_id, limit=1)

    assert status["runtime"]["ready"] is True
    assert status["runtime"]["company_ready"] is False
    assert batch["prepared"] == 1
    assert batch["items"][0]["payload"]["company"]["ready_for_sales"] is False


def test_submit_creates_only_grounded_draft_and_persists_session(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-1", "Сколько стоит решение?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)
    event_id = _prepare_one(agent, workspace_id)

    result = agent.submit_decision(workspace_id, event_id, _decision())

    assert result["success"] is True
    assert result["approved"] is False
    assert result["sent"] is False
    assert result["draft"]["status"] == "draft"
    assert crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "drafted"
    session = crm.get_conversation_session(workspace_id, "79991112233@s.whatsapp.net")
    assert session is not None
    assert session["stage"] == "qualification"
    assert session["turn_count"] == 1
    assert session["facts"]["asks_about_price"]["value"] == "yes"
    assert crm.list_pending_send_requests(limit=10) == []
    interactions = crm.list_interactions(lead["id"])
    assert len(interactions) == 1
    assert interactions[0]["direction"] == "inbound"
    assert not any(item["direction"] == "outbound" for item in interactions)


def test_duplicate_submit_is_idempotent_and_does_not_duplicate_memory(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    _queue(crm, workspace_id, lead, "incoming-idempotent", "Сколько стоит?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)
    event_id = _prepare_one(agent, workspace_id)

    first = agent.submit_decision(workspace_id, event_id, _decision())
    second = agent.submit_decision(workspace_id, event_id, _decision())

    assert first["draft"]["id"] == second["draft"]["id"]
    assert second["idempotent"] is True
    assert len(crm.list_outreach_drafts(lead_id=lead["id"])) == 1
    session = crm.get_conversation_session(workspace_id, "79991112233@s.whatsapp.net")
    assert session is not None and session["turn_count"] == 1


def test_memory_is_preserved_across_inbound_turns(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)

    _queue(crm, workspace_id, lead, "incoming-memory-1", "Сколько стоит?")
    first_id = _prepare_one(agent, workspace_id)
    assert agent.submit_decision(workspace_id, first_id, _decision())["success"]
    _queue(
        crm,
        workspace_id,
        lead,
        "incoming-memory-2",
        "Нужна автоматизация продаж",
    )
    second_id = _prepare_one(agent, workspace_id)
    assert agent.submit_decision(workspace_id, second_id, _decision())["success"]

    session = crm.get_conversation_session(workspace_id, "79991112233@s.whatsapp.net")
    assert session is not None and session["turn_count"] == 2
    assert len(crm.list_outreach_drafts(lead_id=lead["id"])) == 2


def test_low_confidence_escalates_without_creating_draft(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-low", "Нужен договор")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)
    event_id = _prepare_one(agent, workspace_id)

    result = agent.submit_decision(
        workspace_id,
        event_id,
        _decision(
            action="escalate",
            reply_text="",
            stage="handoff",
            intent="contract",
            confidence=52,
            escalation_reason="Нужна проверка условий договора менеджером.",
        ),
    )

    assert result["escalated"] is True
    assert result["draft"] is None
    assert result["sent"] is False
    assert (
        crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "needs_review"
    )
    assert crm.list_outreach_drafts(lead_id=lead["id"]) == []


def test_opt_out_is_acknowledged_as_draft_without_new_offer(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    _queue(crm, workspace_id, lead, "incoming-stop", "Не пишите мне больше")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)
    event_id = _prepare_one(agent, workspace_id)

    result = agent.submit_decision(
        workspace_id,
        event_id,
        _decision(
            action="acknowledge_opt_out",
            reply_text="Понял, больше не будем писать. Спасибо, что сообщили об этом.",
            stage="closed",
            intent="opt_out",
            confidence=99,
            unanswered_question="",
            next_action="Не инициировать новые сообщения.",
        ),
    )

    assert result["quality"]["verdict"] == "pass"
    assert result["draft"]["status"] == "draft"
    assert result["sent"] is False


def test_quality_gate_requests_chatgpt_revision_before_saving(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-repair", "Сколько стоит решение?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)
    event_id = _prepare_one(agent, workspace_id)

    blocked = agent.submit_decision(
        workspace_id,
        event_id,
        _decision(reply_text="Гарантируем, что точно увеличим продажи!"),
    )

    assert blocked["revision_required"] is True
    assert blocked["quality"]["verdict"] == "block"
    assert crm.list_outreach_drafts() == []
    assert (
        crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "processing"
    )

    repaired = agent.submit_decision(workspace_id, event_id, _decision())
    assert repaired["draft"]["status"] == "draft"


def test_observe_mode_classifies_but_does_not_create_draft(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    crm.update_conversation_agent_settings(workspace_id, autonomy_mode="observe")
    event = _queue(crm, workspace_id, lead, "incoming-observe", "Сколько стоит?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)
    event_id = _prepare_one(agent, workspace_id)

    result = agent.submit_decision(workspace_id, event_id, _decision())

    assert result["mode"] == "observe"
    assert result["draft"] is None
    assert result["sent"] is False
    assert (
        crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "acknowledged"
    )


def test_invalid_schema_and_unprepared_event_are_blocked(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-invalid", "Сколько стоит?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(crm, settings)

    unprepared = agent.submit_decision(workspace_id, event["id"], _decision())
    assert unprepared["reason"] == "event_not_prepared"

    event_id = _prepare_one(agent, workspace_id)
    invalid = _decision()
    invalid["unknown_field"] = "must be rejected"
    rejected = agent.submit_decision(workspace_id, event_id, invalid)
    assert rejected["revision_required"] is True
    assert rejected["reason"] == "invalid_decision_schema"
    assert crm.list_outreach_drafts() == []


def test_disabled_runtime_leaves_queue_untouched(tmp_path) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-disabled", "Сколько стоит?")
    disabled_settings = replace(settings, conversation_agent_enabled=False)
    agent = ConversationAgent(crm, disabled_settings)

    result = agent.prepare_pending(workspace_id, limit=1)

    assert result["reason"] == "runtime_disabled"
    assert result["sent"] is False
    assert crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "new"


def test_stale_inbound_is_quarantined_before_chatgpt_reasoning(tmp_path) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    crm.update_conversation_agent_settings(workspace_id, max_inbound_age_hours=24)
    event = crm.upsert_agent_inbox_event(
        workspace_id,
        external_id="stale-conversation",
        chat_jid="79991112233@s.whatsapp.net",
        message_text="Old request",
        received_at=(datetime.now(UTC) - timedelta(days=3)).isoformat(
            timespec="seconds"
        ),
        lead_id=lead["id"],
    )[0]
    agent = ConversationAgent(crm, settings)

    result = agent.prepare_pending(workspace_id, limit=1)

    assert result["prepared"] == 0
    assert result["queue_maintenance"]["stale_quarantined"] == 1
    assert result["sent"] is False
    updated = crm.get_agent_inbox_event(workspace_id, event["id"])
    assert updated["status"] == "needs_review"
    assert "older than" in updated["agent_error"]
