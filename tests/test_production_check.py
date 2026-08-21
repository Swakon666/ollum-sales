from __future__ import annotations

from types import SimpleNamespace

from app import production_check


def test_status_check_accepts_nested_google_sheets_sync_state(monkeypatch) -> None:
    crm = SimpleNamespace(
        get_autopilot_cycle=lambda cycle_id: {
            "id": cycle_id,
            "status": "completed",
        }
    )
    sheets = SimpleNamespace(status=lambda: {"last_sync": {"status": "success"}})
    autopilot = SimpleNamespace(
        status=lambda: {
            "running": True,
            "mode": "safe",
            "interval_minutes": 45,
            "whatsapp_send_flag": False,
            "non_safe_send_flag": False,
        }
    )
    monkeypatch.setattr(
        production_check,
        "_build_services",
        lambda: (crm, sheets, autopilot),
    )
    monkeypatch.setattr(production_check, "_sheet_row_counts", lambda _: {})

    result = production_check.status_check("cycle-1")

    assert result["success"] is True
    assert result["cycle_status"] == "completed"
    assert result["google_sheets"]["last_sync"]["status"] == "success"
