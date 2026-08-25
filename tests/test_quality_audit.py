from __future__ import annotations

import json
from datetime import UTC, datetime

from app.crm import SalesCRM
from app.quality_audit import build_safe_quality_audit


def test_safe_quality_audit_is_aggregate_and_private(tmp_path) -> None:
    crm = SalesCRM(tmp_path / "quality-audit.db")
    workspace_id = "quality-workspace"
    crm.ensure_workspace(workspace_id, "Quality workspace")
    lead = crm.upsert_lead(
        "Private company",
        "https://private-quality.example",
        phones=["+7 999 000-00-01"],
    )
    event = crm.upsert_agent_inbox_event(
        workspace_id,
        external_id="quality-1",
        chat_jid="79990000001@s.whatsapp.net",
        message_text="PRIVATE MESSAGE MUST NEVER LEAK",
        received_at=datetime.now(UTC).isoformat(timespec="seconds"),
        lead_id=lead["id"],
    )[0]
    crm.finish_agent_inbox_event(
        workspace_id,
        event["id"],
        status="ignored",
        decision={
            "action": "ignore",
            "message_quality": "noise",
            "quality_reason": "Non-actionable test noise",
        },
    )
    crm.upsert_agent_inbox_event(
        workspace_id,
        external_id="quality-2",
        chat_jid="79990000002@s.whatsapp.net",
        message_text="PRIVATE MESSAGE MUST NEVER LEAK",
        received_at=datetime.now(UTC).isoformat(timespec="seconds"),
        lead_id=None,
    )

    result = build_safe_quality_audit(
        crm,
        workspace_id,
        whatsapp_send_enabled=False,
        autopilot_send_enabled=False,
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["safety"]["safe"] is True
    assert result["inbox"]["message_quality"]["noise"] == 1
    assert result["inbox"]["repeated_message_patterns"] == 1
    assert result["inbox"]["unlinked"] == 1
    assert result["privacy"]["private_message_text_included"] is False
    assert result["privacy"]["recipient_identifiers_included"] is False
    assert "PRIVATE MESSAGE MUST NEVER LEAK" not in serialized
    assert "79990000001" not in serialized
    assert result["approved"] is False
    assert result["sent"] is False


def test_safe_quality_audit_blocks_if_any_send_flag_is_enabled(tmp_path) -> None:
    crm = SalesCRM(tmp_path / "unsafe-quality-audit.db")
    workspace_id = "unsafe-workspace"
    crm.ensure_workspace(workspace_id, "Unsafe workspace")

    result = build_safe_quality_audit(
        crm,
        workspace_id,
        whatsapp_send_enabled=True,
        autopilot_send_enabled=False,
    )

    assert result["overall_status"] == "blocked"
    assert result["safety"]["safe"] is False
    assert any(issue["code"] == "unsafe_send_flags" for issue in result["issues"])
    assert result["sent"] is False
