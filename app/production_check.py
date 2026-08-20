from __future__ import annotations

import argparse
import json
from typing import Any

from app.autopilot import AutopilotService
from app.config import settings
from app.crm import SalesCRM
from app.google_sheets import GoogleSheetsSync


def _never_send(_recipient: str, _message: str) -> dict[str, Any]:
    raise AssertionError("SAFE production verification attempted to call the sender")


def _build_services() -> tuple[SalesCRM, GoogleSheetsSync, AutopilotService]:
    crm = SalesCRM(settings.crm_db_path)
    sheets = GoogleSheetsSync(
        crm,
        enabled=settings.google_sheets_enabled,
        spreadsheet_id=settings.google_sheets_spreadsheet_id,
        service_account_file=settings.google_service_account_file,
    )
    autopilot = AutopilotService(crm, settings, sheets, sender=_never_send)
    return crm, sheets, autopilot


def _sheet_row_counts(sheets: GoogleSheetsSync) -> dict[str, int]:
    service = sheets._build_service()
    return {
        tab: max(0, len(sheets._get_values(service, f"'{tab}'!A:Z")) - 1)
        for tab in sheets.tab_names
    }


def _cycle_sheet_rows(crm: SalesCRM, cycle_id: str) -> dict[str, list[dict[str, Any]]]:
    with crm.connect() as connection:
        campaigns = [
            dict(row)
            for row in connection.execute(
                """
                SELECT c.id, c.name, c.industry, c.location, c.status
                FROM autopilot_campaigns ac
                JOIN campaigns c ON c.id = ac.campaign_id
                WHERE ac.cycle_id = ?
                ORDER BY c.created_at
                """,
                (cycle_id,),
            ).fetchall()
        ]
        leads = [
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT l.id, l.company_name, l.website_url, l.score, l.status
                FROM autopilot_campaigns ac
                JOIN campaign_leads cl ON cl.campaign_id = ac.campaign_id
                JOIN leads l ON l.id = cl.lead_id
                WHERE ac.cycle_id = ?
                ORDER BY l.score DESC, l.company_name
                """,
                (cycle_id,),
            ).fetchall()
        ]
        outreach = [
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT d.id, d.lead_id, l.company_name, d.channel, d.status
                FROM autopilot_campaigns ac
                JOIN campaign_leads cl ON cl.campaign_id = ac.campaign_id
                JOIN leads l ON l.id = cl.lead_id
                JOIN outreach_drafts d ON d.lead_id = l.id
                WHERE ac.cycle_id = ?
                ORDER BY d.created_at
                """,
                (cycle_id,),
            ).fetchall()
        ]
    return {"CAMPAIGNS": campaigns, "LEADS": leads, "OUTREACH": outreach}


def _verify_safe_send_guardrail(
    crm: SalesCRM, autopilot: AutopilotService
) -> dict[str, Any]:
    pending_before = crm.list_pending_send_requests(limit=200)
    blocked = autopilot._process_send_requests(mode="safe")
    pending_after = crm.list_pending_send_requests(limit=200)
    if not blocked["blocked"] or blocked["sent"] != 0 or blocked["failed"] != 0:
        raise RuntimeError("SAFE did not block send-request processing")
    if pending_after != pending_before:
        raise RuntimeError("SAFE send guardrail mutated pending send requests")
    return {
        "pending_requests": len(pending_before),
        "send_execution": blocked,
        "pending_requests_unchanged": True,
    }


def run_check() -> dict[str, Any]:
    crm, sheets, autopilot = _build_services()
    if settings.allow_whatsapp_send or settings.allow_autopilot_send:
        raise RuntimeError("production send flags must both remain false")
    if settings.autopilot_default_mode != "safe":
        raise RuntimeError("production default mode must be SAFE")
    if not sheets.configured:
        raise RuntimeError("Google Sheets is not fully configured")

    started = autopilot.start(mode="safe", interval_minutes=45)
    if not started["success"]:
        raise RuntimeError("could not start Autopilot in SAFE mode")
    initial_sync = sheets.sync()
    send_guardrail_check = _verify_safe_send_guardrail(crm, autopilot)
    cycle_result = autopilot.run_cycle(force=True)
    if not cycle_result.get("success"):
        raise RuntimeError(f"SAFE cycle failed: {cycle_result}")
    cycle = cycle_result["cycle"]
    metrics = cycle["metrics"]
    if cycle["mode"] != "safe" or metrics["send_requests"].get("sent") != 0:
        raise RuntimeError("SAFE cycle reported an invalid mode or a sent message")
    if not metrics["google_sheets"]["success"]:
        raise RuntimeError("cycle did not complete its Google Sheets sync")

    state = autopilot.status()
    if not state["running"] or state["mode"] != "safe":
        raise RuntimeError("Autopilot did not remain running in SAFE mode")
    if state["interval_minutes"] != 45:
        raise RuntimeError("Autopilot interval is not 45 minutes")
    if state["whatsapp_send_flag"] or state["non_safe_send_flag"]:
        raise RuntimeError("a production send flag became enabled")

    verticals = crm.list_verticals(enabled=True, limit=500)
    selected = set(cycle["selected_verticals"])
    rotation = [
        {
            "id": item["id"],
            "name": item["name"],
            "last_selected_at": item["last_selected_at"],
            "selected_in_cycle": item["id"] in selected,
        }
        for item in verticals
    ]
    return {
        "success": True,
        "cycle_id": cycle["id"],
        "cycle": {
            "mode": cycle["mode"],
            "status": cycle["status"],
            "verticals_processed": len(cycle["selected_verticals"]),
            "metrics": metrics,
        },
        "sheet_rows_before_cycle": initial_sync["tabs"],
        "sheet_rows_after_cycle": _sheet_row_counts(sheets),
        "sheet_rows_from_cycle": _cycle_sheet_rows(crm, cycle["id"]),
        "safe_send_guardrail_check": send_guardrail_check,
        "rotation": rotation,
        "autopilot": state,
    }


def status_check(expected_cycle_id: str) -> dict[str, Any]:
    crm, sheets, autopilot = _build_services()
    state = autopilot.status()
    cycle = crm.get_autopilot_cycle(expected_cycle_id)
    if not state["running"] or state["mode"] != "safe":
        raise RuntimeError("SAFE running state did not persist")
    if state["interval_minutes"] != 45:
        raise RuntimeError("45-minute interval did not persist")
    if state["whatsapp_send_flag"] or state["non_safe_send_flag"]:
        raise RuntimeError("send flags are not disabled after restart")
    if cycle["status"] != "completed":
        raise RuntimeError("verified cycle did not persist as completed")
    sheet_state = sheets.status()
    if sheet_state["last_sync_status"] != "success":
        raise RuntimeError("Google Sheets success state did not persist")
    return {
        "success": True,
        "persisted_cycle_id": cycle["id"],
        "cycle_status": cycle["status"],
        "autopilot": state,
        "google_sheets": sheet_state,
        "sheet_rows": _sheet_row_counts(sheets),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("expected_cycle_id")
    args = parser.parse_args()
    result = (
        run_check() if args.command == "run" else status_check(args.expected_cycle_id)
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
