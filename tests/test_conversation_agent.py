from __future__ import annotations

from dataclasses import replace

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


def _queue(crm: SalesCRM, workspace_id: str, lead: dict, external_id: str, text: str):
    return crm.upsert_agent_inbox_event(
        workspace_id,
        external_id=external_id,
        chat_jid="79991112233@s.whatsapp.net",
        message_text=text,
        received_at="2026-08-23T11:00:00+00:00",
        lead_id=lead["id"],
    )[0]


def _decision(**overrides) -> ConversationDecision:
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
    return ConversationDecision(**values)


def test_agent_creates_only_grounded_draft_and_persists_session(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-1", "Сколько стоит решение?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(
        crm, settings, decision_provider=lambda _payload: _decision()
    )

    result = agent.process_next(workspace_id)

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
    assert crm.list_interactions(lead["id"]) == []


def test_agent_keeps_memory_across_inbound_turns(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(
        crm, settings, decision_provider=lambda _payload: _decision()
    )

    _queue(crm, workspace_id, lead, "incoming-1", "Сколько стоит решение?")
    assert agent.process_next(workspace_id)["success"] is True
    _queue(crm, workspace_id, lead, "incoming-2", "Нужна автоматизация продаж")
    assert agent.process_next(workspace_id)["success"] is True

    session = crm.get_conversation_session(workspace_id, "79991112233@s.whatsapp.net")
    assert session is not None
    assert session["turn_count"] == 2
    assert len(crm.list_outreach_drafts(lead_id=lead["id"])) == 2


def test_low_confidence_escalates_without_creating_draft(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-low", "Нужен договор")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(
        crm,
        settings,
        decision_provider=lambda _payload: _decision(
            action="escalate",
            reply_text="",
            stage="handoff",
            intent="contract",
            confidence=52,
            escalation_reason="Нужна проверка условий договора менеджером.",
        ),
    )

    result = agent.process_next(workspace_id)

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
    agent = ConversationAgent(
        crm,
        settings,
        decision_provider=lambda _payload: _decision(
            action="acknowledge_opt_out",
            reply_text="Понял, больше не будем писать. Спасибо, что сообщили об этом.",
            stage="closed",
            intent="opt_out",
            confidence=99,
            unanswered_question="",
            next_action="Не инициировать новые сообщения.",
        ),
    )

    result = agent.process_next(workspace_id)

    assert result["quality"]["verdict"] == "pass"
    assert result["draft"]["status"] == "draft"
    assert result["sent"] is False


def test_quality_gate_requests_revision_before_saving(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    _queue(crm, workspace_id, lead, "incoming-repair", "Сколько стоит решение?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    calls = []

    def provider(payload):
        calls.append(payload)
        if len(calls) == 1:
            return _decision(reply_text="Гарантируем, что точно увеличим продажи!")
        return _decision()

    agent = ConversationAgent(crm, settings, decision_provider=provider)
    result = agent.process_next(workspace_id)

    assert len(calls) == 2
    assert calls[1]["quality_feedback"]
    assert result["draft"]["status"] == "draft"
    assert result["quality"]["verdict"] == "pass"


def test_repair_pass_can_escalate_without_saving_a_draft(tmp_path, monkeypatch) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    _queue(crm, workspace_id, lead, "incoming-repair-escalate", "Назовите гарантию")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    calls = []

    def provider(payload):
        calls.append(payload)
        if len(calls) == 1:
            return _decision(reply_text="Гарантируем рост продаж в два раза.")
        return _decision(
            action="escalate",
            reply_text="",
            stage="handoff",
            confidence=96,
            escalation_reason="Требуется согласовать допустимые обязательства.",
        )

    agent = ConversationAgent(crm, settings, decision_provider=provider)
    result = agent.process_next(workspace_id)

    assert len(calls) == 2
    assert result["escalated"] is True
    assert result["draft"] is None
    assert crm.list_outreach_drafts() == []


def test_observe_mode_classifies_but_does_not_create_draft(
    tmp_path, monkeypatch
) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    crm.update_conversation_agent_settings(workspace_id, autonomy_mode="observe")
    event = _queue(crm, workspace_id, lead, "incoming-observe", "Сколько стоит?")
    monkeypatch.setattr(agent_module, "list_messages", lambda **_kwargs: [])
    agent = ConversationAgent(
        crm, settings, decision_provider=lambda _payload: _decision()
    )

    result = agent.process_next(workspace_id)

    assert result["mode"] == "observe"
    assert result["draft"] is None
    assert result["sent"] is False
    assert (
        crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "acknowledged"
    )


def test_unconfigured_runtime_leaves_queue_untouched(tmp_path) -> None:
    crm, lead, workspace_id = _ready_crm(tmp_path)
    event = _queue(crm, workspace_id, lead, "incoming-no-key", "Сколько стоит?")
    no_key_settings = replace(settings, openai_api_key=None)
    agent = ConversationAgent(crm, no_key_settings)

    result = agent.process_next(workspace_id)

    assert result["reason"] == "openai_not_configured"
    assert result["sent"] is False
    assert crm.get_agent_inbox_event(workspace_id, event["id"])["status"] == "new"
