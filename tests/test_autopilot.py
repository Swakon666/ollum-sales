from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.autopilot import AutopilotService
from app.crm import SalesCRM
from app.google_sheets import LEADS_HEADERS, OUTREACH_HEADERS, GoogleSheetsSync


class FakeSheets:
    def __init__(self) -> None:
        self.sync_calls = 0

    def status(self) -> dict[str, object]:
        return {"configured": False}

    def sync(self) -> dict[str, object]:
        self.sync_calls += 1
        return {"success": False, "blocked": True}


def settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "serper_api_key": None,
        "company_search_timeout": 5,
        "website_inspection_timeout": 5,
        "autopilot_default_mode": "safe",
        "autopilot_interval_minutes": 60,
        "autopilot_max_verticals_per_cycle": 1,
        "autopilot_leads_per_vertical": 5,
        "autopilot_score_threshold": 60,
        "autopilot_min_training_leads": 100,
        "allow_autopilot_send": False,
        "allow_whatsapp_send": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AutopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.crm = SalesCRM(Path(self.tempdir.name) / "sales.db")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_vertical_and_cycle_state(self) -> None:
        vertical = self.crm.create_vertical(
            "ventilation",
            region="Moscow",
            days=["monday"],
            daily_target=8,
            min_score=70,
        )
        updated = self.crm.update_vertical(vertical["id"], weight=2.5, enabled=False)
        self.assertEqual(updated["weight"], 2.5)
        self.assertFalse(updated["enabled"])

        self.crm.start_autopilot(mode="safe", interval_minutes=30)
        cycle = self.crm.begin_autopilot_cycle()
        self.assertIsNotNone(cycle)
        completed = self.crm.complete_autopilot_cycle(
            cycle["id"], metrics={"leads_found": 3}
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["metrics"]["leads_found"], 3)
        self.assertIsNone(self.crm.get_autopilot_state()["current_cycle_id"])

    def test_non_safe_start_is_blocked_by_default(self) -> None:
        service = AutopilotService(self.crm, settings(), FakeSheets())
        result = service.start(mode="autopilot", confirm_non_safe=True)
        self.assertTrue(result["blocked"])
        self.assertFalse(self.crm.get_autopilot_state()["running"])

    def test_safe_cycle_discovers_analyzes_and_drafts_without_sending(self) -> None:
        self.crm.create_vertical(
            "ventilation",
            region="Moscow",
            daily_target=1,
            min_score=60,
        )

        def discoverer(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "provider": "test",
                "results": [
                    {
                        "company_name": "Example Vent",
                        "website_url": "https://example.com/catalog",
                    }
                ],
            }

        def inspector(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "visible_text": "Каталог продукции прайс цены товар " * 100,
                "mobile_viewport": False,
                "forms": {"count": 0},
                "contacts": {"phones": ["+79990000000"], "social_links": []},
                "technologies": ["Yandex Metrica"],
            }

        send_calls: list[tuple[object, ...]] = []

        def sender(*args: object, **_kwargs: object) -> dict[str, object]:
            send_calls.append(args)
            return {"success": True}

        sheets = FakeSheets()
        service = AutopilotService(
            self.crm,
            settings(),
            sheets,
            discoverer=discoverer,
            inspector=inspector,
            sender=sender,
        )
        self.assertTrue(service.start(mode="safe")["success"])
        result = service.run_cycle(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["cycle"]["metrics"]["analyzed"], 1)
        self.assertEqual(result["cycle"]["metrics"]["drafts_created"], 1)
        self.assertEqual(len(self.crm.list_outreach_drafts()), 1)
        self.assertEqual(send_calls, [])
        self.assertEqual(sheets.sync_calls, 1)

    def test_sheet_approval_requires_exact_visible_draft(self) -> None:
        lead = self.crm.upsert_lead("Example", "https://example.org")
        draft = self.crm.save_outreach_draft(
            lead["id"],
            channel="whatsapp",
            recipient="+79990000001",
            message="Exact reviewed message",
        )
        sync = GoogleSheetsSync(
            self.crm,
            enabled=False,
            spreadsheet_id=None,
            service_account_file=None,
        )
        lead_row = [""] * len(LEADS_HEADERS)
        lead_row[LEADS_HEADERS.index("DRAFT_ID")] = draft["id"]
        lead_row[LEADS_HEADERS.index("DRAFT_RECIPIENT")] = draft["recipient"]
        lead_row[LEADS_HEADERS.index("DRAFT_MESSAGE")] = draft["message"]
        lead_row[LEADS_HEADERS.index("APPROVE")] = "YES"

        def values(_service: object, range_name: str) -> list[list[object]]:
            if range_name.startswith("LEADS"):
                return [LEADS_HEADERS, lead_row]
            return [OUTREACH_HEADERS]

        sync._get_values = values  # type: ignore[method-assign]
        result = sync._pull_actions(object())
        self.assertEqual(result["approved_draft_ids"], [draft["id"]])
        self.assertEqual(self.crm.get_outreach_draft(draft["id"])["status"], "approved")

        second = self.crm.save_outreach_draft(
            lead["id"],
            channel="whatsapp",
            recipient="+79990000002",
            message="Second exact message",
        )
        lead_row[LEADS_HEADERS.index("DRAFT_ID")] = second["id"]
        lead_row[LEADS_HEADERS.index("DRAFT_RECIPIENT")] = second["recipient"]
        lead_row[LEADS_HEADERS.index("DRAFT_MESSAGE")] = "tampered message"
        result = sync._pull_actions(object())
        self.assertEqual(result["approved_draft_ids"], [])
        self.assertEqual(len(result["rejected_actions"]), 1)
        self.assertEqual(self.crm.get_outreach_draft(second["id"])["status"], "draft")

    def test_sheet_actions_merge_approve_and_send_across_tabs(self) -> None:
        lead = self.crm.upsert_lead("Example", "https://example.org")
        draft = self.crm.save_outreach_draft(
            lead["id"],
            channel="whatsapp",
            recipient="+79990000003",
            message="One exact reviewed message",
        )
        sync = GoogleSheetsSync(
            self.crm,
            enabled=False,
            spreadsheet_id=None,
            service_account_file=None,
        )
        lead_row = [""] * len(LEADS_HEADERS)
        lead_row[LEADS_HEADERS.index("DRAFT_ID")] = draft["id"]
        lead_row[LEADS_HEADERS.index("DRAFT_RECIPIENT")] = draft["recipient"]
        lead_row[LEADS_HEADERS.index("DRAFT_MESSAGE")] = draft["message"]
        lead_row[LEADS_HEADERS.index("APPROVE")] = "YES"
        outreach_row = [""] * len(OUTREACH_HEADERS)
        outreach_row[0] = draft["id"]
        outreach_row[5] = draft["recipient"]
        outreach_row[6] = draft["message"]
        outreach_row[9] = "YES"

        def values(_service: object, range_name: str) -> list[list[object]]:
            if range_name.startswith("LEADS"):
                return [LEADS_HEADERS, lead_row]
            return [OUTREACH_HEADERS, outreach_row]

        sync._get_values = values  # type: ignore[method-assign]
        result = sync._pull_actions(object())
        self.assertEqual(result["approved_draft_ids"], [draft["id"]])
        self.assertEqual(result["send_requested_draft_ids"], [draft["id"]])
        self.assertEqual(len(self.crm.list_pending_send_requests()), 1)


if __name__ == "__main__":
    unittest.main()
