from __future__ import annotations

import pytest

from app import agent_inbox
from app.crm import SalesCRM


def _confirm_onboarding(crm: SalesCRM, workspace_id: str = "ollum-group") -> dict:
    state = crm.get_company_onboarding_state(workspace_id)
    return crm.complete_company_onboarding(
        workspace_id,
        confirm_ready=True,
        confirmed_revision=state["confirmation"]["required_revision"],
        summary_hash=state["confirmation"]["summary_hash"],
    )


def test_sync_whatsapp_inbox_queues_only_latest_unanswered_private_chats(
    tmp_path, monkeypatch
) -> None:
    crm = SalesCRM(tmp_path / "sales.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    lead = crm.upsert_lead(
        "Known Lead",
        "https://known-lead.test",
        phones=["+7 999 123-45-67"],
    )
    messages = [
        {
            "id": "m-1",
            "timestamp": "2026-08-23T10:00:00+00:00",
            "chat_jid": "79991234567@s.whatsapp.net",
            "chat_name": "Known contact",
            "content": "Какие сроки запуска?",
            "is_from_me": False,
            "media_type": None,
        },
        {
            "id": "m-2",
            "timestamp": "2026-08-23T09:59:00+00:00",
            "chat_jid": "78880000000@s.whatsapp.net",
            "chat_name": "Already answered",
            "content": "Наш ответ",
            "is_from_me": True,
            "media_type": None,
        },
        {
            "id": "m-3",
            "timestamp": "2026-08-23T09:58:00+00:00",
            "chat_jid": "78880000000@s.whatsapp.net",
            "chat_name": "Already answered",
            "content": "Старый вопрос",
            "is_from_me": False,
            "media_type": None,
        },
        {
            "id": "m-4",
            "timestamp": "2026-08-23T09:57:00+00:00",
            "chat_jid": "77770000000@s.whatsapp.net",
            "chat_name": "New contact",
            "content": "",
            "is_from_me": False,
            "media_type": "image",
        },
        {
            "id": "m-5",
            "timestamp": "2026-08-23T09:56:00+00:00",
            "chat_jid": "12345@g.us",
            "chat_name": "Group",
            "content": "Групповое сообщение",
            "is_from_me": False,
            "media_type": None,
        },
    ]
    monkeypatch.setattr(agent_inbox, "list_messages", lambda limit: messages[:limit])

    first = agent_inbox.sync_whatsapp_inbox(crm, "ollum-group")
    second = agent_inbox.sync_whatsapp_inbox(crm, "ollum-group")

    assert first["new_events"] == 2
    assert first["matched_leads"] == 2
    assert first["unmatched_leads"] == 0
    assert first["created_inbound_contacts"] == 1
    assert second["new_events"] == 0
    queued = crm.list_agent_inbox_events("ollum-group", status="new")
    assert {item["external_id"] for item in queued} == {"m-1", "m-4"}
    known = next(item for item in queued if item["external_id"] == "m-1")
    assert known["lead_id"] == lead["id"]
    attachment = next(item for item in queued if item["external_id"] == "m-4")
    assert attachment["message_text"] == "[WhatsApp attachment: image]"
    assert attachment["lead_id"] is not None


def test_targeted_sync_and_queue_selection_never_fall_back_to_another_contact(
    tmp_path, monkeypatch
) -> None:
    crm = SalesCRM(tmp_path / "targeted.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    target_jid = "79779335513@s.whatsapp.net"
    other_jid = "79990000000@s.whatsapp.net"
    calls: list[tuple[str | None, int]] = []

    def fake_list_messages(
        *, chat_jid: str | None = None, limit: int
    ) -> list[dict[str, object]]:
        calls.append((chat_jid, limit))
        return [
            {
                "id": "target-in-1",
                "timestamp": "2026-08-24T16:41:01+00:00",
                "chat_jid": target_jid,
                "chat_name": "Test contact",
                "content": "Расскажите подробнее",
                "is_from_me": False,
                "media_type": None,
            }
        ]

    monkeypatch.setattr(agent_inbox, "list_messages", fake_list_messages)
    synced = agent_inbox.sync_whatsapp_inbox(
        crm,
        "ollum-group",
        phone="+7 (977) 933-55-13",
    )
    crm.upsert_agent_inbox_event(
        "ollum-group",
        external_id="other-in-1",
        chat_jid=other_jid,
        message_text="Другой диалог",
        received_at="2026-08-24T16:42:00+00:00",
    )

    assert calls == [(target_jid, 100)]
    assert synced["target_chat_jid"] == target_jid
    assert synced["new_events"] == 1
    targeted = crm.list_agent_inbox_events(
        "ollum-group", status="new", chat_jid=target_jid
    )
    missing = crm.list_agent_inbox_events(
        "ollum-group",
        status="new",
        chat_jid="78880000000@s.whatsapp.net",
    )
    assert [item["external_id"] for item in targeted] == ["target-in-1"]
    assert missing == []
    assert (
        crm.claim_next_agent_inbox_event(
            "ollum-group", chat_jid="78880000000@s.whatsapp.net"
        )
        is None
    )
    claimed = crm.claim_next_agent_inbox_event("ollum-group", chat_jid=target_jid)
    assert claimed is not None
    assert claimed["external_id"] == "target-in-1"


def test_target_resolution_rejects_conflicting_phone_and_jid() -> None:
    try:
        agent_inbox.resolve_target_chat_jid(
            phone="+79779335513",
            chat_jid="79990000000@s.whatsapp.net",
        )
    except ValueError as exc:
        assert "different WhatsApp contacts" in str(exc)
    else:
        raise AssertionError("conflicting target identifiers must be rejected")


def test_next_action_moves_from_interview_to_inbound_reply_without_sending(
    tmp_path,
) -> None:
    crm = SalesCRM(tmp_path / "workflow.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    first = agent_inbox.next_agent_action(crm, "ollum-group")
    assert first["action"] == "continue_company_onboarding"
    assert len(first["onboarding"]["next_questions"]) <= 3

    crm.update_company_profile(
        "ollum-group",
        company_name="Example Studio",
        industry="Digital services",
        target_customer="B2B companies",
        positioning="Grounded sales automation",
    )
    crm.save_company_knowledge(
        "ollum-group",
        category="service",
        title="Sales agent",
        content={"details": "research, scoring and reply drafts"},
    )
    crm.save_company_knowledge(
        "ollum-group",
        category="price",
        title="Custom estimate",
        content={"details": "calculated after discovery"},
    )
    review = agent_inbox.next_agent_action(crm, "ollum-group")
    assert review["action"] == "review_company_onboarding"
    _confirm_onboarding(crm)

    lead = crm.upsert_lead(
        "Prospect",
        "https://prospect.test",
        phones=["+7 999 111-22-33"],
    )
    event, _created = crm.upsert_agent_inbox_event(
        "ollum-group",
        external_id="reply-1",
        chat_jid="79991112233@s.whatsapp.net",
        message_text="Сколько это стоит?",
        received_at="2026-08-23T11:00:00+00:00",
        lead_id=lead["id"],
    )
    reply = agent_inbox.next_agent_action(crm, "ollum-group")
    assert reply["action"] == "prepare_whatsapp_reply"
    assert reply["inbox_event"]["id"] == event["id"]
    assert reply["external_side_effect"] is False
    assert "two separate" in reply["instruction"]

    crm.update_agent_inbox_event("ollum-group", event["id"], status="acknowledged")
    idle = agent_inbox.next_agent_action(crm, "ollum-group")
    assert idle["action"] == "continue_safe_lead_work"
    assert idle["external_side_effect"] is False


def test_two_chat_lanes_never_cross_responsibilities(tmp_path) -> None:
    crm = SalesCRM(tmp_path / "two-chat.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    onboarding = agent_inbox.next_agent_action(crm, "ollum-group", lane="prospecting")
    assert onboarding["action"] == "continue_company_onboarding"
    assert onboarding["lane"] == "prospecting"

    crm.update_company_profile(
        "ollum-group",
        company_name="Example Studio",
        industry="Digital services",
        target_customer="B2B companies",
        positioning="Grounded sales automation",
    )
    for category, title in (("service", "Sales agent"), ("price", "Estimate")):
        crm.save_company_knowledge(
            "ollum-group",
            category=category,
            title=title,
            content={"details": "confirmed by operator"},
        )
    _confirm_onboarding(crm)
    lead = crm.upsert_lead(
        "Prospect", "https://lane-prospect.test", phones=["+7 999 111-22-33"]
    )
    event = crm.upsert_agent_inbox_event(
        "ollum-group",
        external_id="lane-reply-1",
        chat_jid="79991112233@s.whatsapp.net",
        message_text="Interested in the offer",
        received_at="2026-08-23T11:00:00+00:00",
        lead_id=lead["id"],
    )[0]

    inbox = agent_inbox.next_agent_action(crm, "ollum-group", lane="inbox")
    prospecting = agent_inbox.next_agent_action(crm, "ollum-group", lane="prospecting")
    assert inbox["action"] == "prepare_whatsapp_reply"
    assert inbox["inbox_event"]["id"] == event["id"]
    assert inbox["lane"] == "inbox"
    assert prospecting["action"] == "continue_safe_lead_work"
    assert prospecting["lane"] == "prospecting"
    assert "inbox_event" not in prospecting
    assert "inbox" not in prospecting

    crm.update_agent_inbox_event("ollum-group", event["id"], status="acknowledged")
    clear = agent_inbox.next_agent_action(crm, "ollum-group", lane="inbox")
    assert clear["action"] == "inbox_clear"
    assert "qualified_leads" not in clear


def test_ready_status_cannot_bypass_missing_required_knowledge(tmp_path) -> None:
    crm = SalesCRM(tmp_path / "onboarding-gate.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    crm.update_company_profile(
        "ollum-group",
        company_name="Example Studio",
        industry="Digital services",
        target_customer="B2B companies",
        positioning="Grounded sales automation",
    )
    crm.save_company_knowledge(
        "ollum-group",
        category="service",
        title="Sales agent",
        content={"details": "Research and drafts"},
    )
    price = crm.save_company_knowledge(
        "ollum-group",
        category="price",
        title="Custom quote",
        content={"details": "Calculated after discovery"},
    )
    _confirm_onboarding(crm)
    crm.archive_company_knowledge("ollum-group", price["id"])

    blocked = agent_inbox.next_agent_action(crm, "ollum-group", lane="prospecting")

    assert blocked["action"] == "continue_company_onboarding"
    assert blocked["onboarding"]["sales_ready"] is False
    assert blocked["external_side_effect"] is False


def test_completed_onboarding_lane_returns_two_chat_handoff(tmp_path) -> None:
    crm = SalesCRM(tmp_path / "onboarding-handoff.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    crm.update_company_profile(
        "ollum-group",
        company_name="Example Studio",
        industry="Digital services",
        target_customer="B2B companies",
        positioning="Grounded sales automation",
    )
    for category, title in (("service", "Sales agent"), ("price", "Quote")):
        crm.save_company_knowledge(
            "ollum-group",
            category=category,
            title=title,
            content={"details": "Confirmed by operator"},
        )
    _confirm_onboarding(crm)

    handoff = agent_inbox.next_agent_action(crm, "ollum-group", lane="onboarding")

    assert handoff["action"] == "setup_two_chat_operation"
    assert handoff["handoff"]["primary_chat"]["lane"] == "prospecting"
    assert handoff["handoff"]["monitoring_chat"]["lane"] == "inbox"
    assert handoff["handoff"]["operator_action_required"] is True
    assert handoff["external_side_effect"] is False


def test_lane_validation_rejects_cross_lane_contact_targeting(tmp_path) -> None:
    crm = SalesCRM(tmp_path / "lane-validation.db")
    crm.ensure_workspace("ollum-group", "Ollum Group")
    with pytest.raises(ValueError, match="lane must be"):
        agent_inbox.next_agent_action(crm, "ollum-group", lane="unknown")
    with pytest.raises(ValueError, match="only for the inbox lane"):
        agent_inbox.next_agent_action(
            crm,
            "ollum-group",
            lane="prospecting",
            phone="+79991112233",
        )
