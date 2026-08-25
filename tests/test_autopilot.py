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
        "evidence_ttl_hours": 168,
        "retry_attempts": 3,
        "retry_base_delay_seconds": 0,
        "autopilot_default_mode": "safe",
        "autopilot_interval_minutes": 60,
        "autopilot_max_verticals_per_cycle": 1,
        "autopilot_leads_per_vertical": 5,
        "autopilot_score_threshold": 60,
        "autopilot_min_training_leads": 100,
        "chatgpt_prospecting_queue_limit": 6,
        "autopilot_server_discovery_enabled": True,
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

    def test_safe_cycle_only_collects_evidence_and_queues_for_chatgpt(self) -> None:
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
        metrics = result["cycle"]["metrics"]
        self.assertEqual(metrics["queued_for_chatgpt"], 1)
        self.assertEqual(metrics["analyzed"], 0)
        self.assertEqual(metrics["qualified"], 0)
        self.assertEqual(metrics["drafts_created"], 0)
        lead = self.crm.list_leads()[0]
        self.assertEqual(lead["status"], "new")
        self.assertEqual(lead["analysis"], {})
        self.assertIsNone(lead["score"])
        self.assertEqual(len(self.crm.list_outreach_drafts()), 0)
        self.assertEqual(send_calls, [])
        self.assertEqual(sheets.sync_calls, 1)

        status = service.status()
        self.assertEqual(status["reasoning_engine"], "chatgpt_mcp_only")
        self.assertFalse(status["server_llm_enabled"])
        self.assertFalse(status["server_analysis_enabled"])
        self.assertIn("never analyzes", status["safe_behavior"])

    def test_cycle_skips_duplicates_and_rejects_non_company_pages(self) -> None:
        self.crm.create_vertical(
            "ventilation", region="Moscow", daily_target=1, min_score=60
        )
        self.crm.upsert_lead("Known", "https://known.example/")

        def discoverer(*_args: object, **kwargs: object) -> dict[str, object]:
            self.assertEqual(kwargs["limit"], 4)
            return {
                "provider": "test",
                "results": [
                    {"company_name": "Known", "website_url": "https://known.example/x"},
                    {
                        "company_name": "Article",
                        "website_url": "https://article.example/",
                    },
                    {
                        "company_name": "Target",
                        "website_url": "https://target.example/",
                    },
                ],
            }

        def inspector(url: str, **_kwargs: object) -> dict[str, object]:
            if "article.example" in url:
                return {
                    "final_url": "https://article.example/blog/what-is-ventilation",
                    "title": "Что такое вентиляция: статья",
                    "visible_text": "Определение. Автор статьи. Новости и блог.",
                    "headings": [],
                    "contacts": {"phones": [], "emails": []},
                    "forms": {"count": 0},
                }
            return {
                "final_url": "https://target.example/",
                "title": "Target",
                "visible_text": "Наши услуги. Каталог. Оставить заявку. " * 50,
                "mobile_viewport": False,
                "headings": [],
                "contacts": {"phones": ["+79990000001"], "emails": []},
                "forms": {"count": 1},
                "technologies": [],
            }

        service = AutopilotService(
            self.crm,
            settings(),
            FakeSheets(),
            discoverer=discoverer,
            inspector=inspector,
        )
        self.assertTrue(service.start(mode="safe")["success"])
        result = service.run_cycle(force=True)
        metrics = result["cycle"]["metrics"]
        self.assertEqual(metrics["candidates_seen"], 3)
        self.assertEqual(metrics["duplicates_skipped"], 1)
        self.assertEqual(metrics["candidates_rejected"], 1)
        self.assertEqual(metrics["leads_found"], 1)
        self.assertEqual(metrics["queued_for_chatgpt"], 1)
        self.assertEqual(metrics["analyzed"], 0)
        self.assertIn("editorial_path", metrics["rejection_reasons"])

    def test_cycle_stops_discovery_when_chatgpt_queue_is_full(self) -> None:
        self.crm.create_vertical(
            "ventilation", region="Moscow", daily_target=5, min_score=60
        )
        for index in range(2):
            self.crm.upsert_lead(
                f"Pending {index}",
                f"https://pending-{index}.example/",
                source="autopilot:test",
            )

        discover_calls = 0

        def discoverer(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal discover_calls
            discover_calls += 1
            raise AssertionError("discovery must not run while the GPT queue is full")

        service = AutopilotService(
            self.crm,
            settings(chatgpt_prospecting_queue_limit=2),
            FakeSheets(),
            discoverer=discoverer,
        )
        self.assertTrue(service.start(mode="safe")["success"])
        result = service.run_cycle(force=True)

        self.assertTrue(result["success"])
        metrics = result["cycle"]["metrics"]
        self.assertEqual(discover_calls, 0)
        self.assertEqual(metrics["queue_before"], 2)
        self.assertEqual(metrics["queue_after"], 2)
        self.assertEqual(metrics["queue_limit"], 2)
        self.assertEqual(metrics["discovery_skipped_queue_full"], 1)
        self.assertEqual(metrics["queued_for_chatgpt"], 0)

    def test_chatgpt_directed_mode_never_runs_server_discovery(self) -> None:
        self.crm.create_vertical(
            "ventilation", region="Moscow", daily_target=5, min_score=60
        )
        discover_calls = 0

        def discoverer(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal discover_calls
            discover_calls += 1
            raise AssertionError("scheduled server discovery must remain disabled")

        sheets = FakeSheets()
        service = AutopilotService(
            self.crm,
            settings(autopilot_server_discovery_enabled=False),
            sheets,
            discoverer=discoverer,
        )
        self.assertTrue(service.start(mode="safe")["success"])
        result = service.run_cycle(force=True)

        self.assertTrue(result["success"])
        metrics = result["cycle"]["metrics"]
        self.assertEqual(discover_calls, 0)
        self.assertEqual(metrics["discovery_skipped_chatgpt_directed"], 1)
        self.assertEqual(metrics["queued_for_chatgpt"], 0)
        self.assertEqual(sheets.sync_calls, 1)
        status = service.status()
        self.assertFalse(status["server_discovery_enabled"])
        self.assertEqual(status["discovery_controller"], "chatgpt_scheduled_task")

    def test_autopilot_retries_transient_discovery_idempotently(self) -> None:
        self.crm.create_vertical(
            "services", region="Moscow", daily_target=1, min_score=60
        )
        discover_calls = 0

        def discoverer(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal discover_calls
            discover_calls += 1
            if discover_calls == 1:
                raise ConnectionError("temporary discovery failure")
            return {
                "provider": "test",
                "results": [
                    {
                        "company_name": "Retry Company",
                        "website_url": "https://retry-company.test",
                    }
                ],
            }

        def inspector(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "final_url": "https://retry-company.test",
                "title": "Retry Company",
                "visible_text": "Services catalog request form " * 80,
                "mobile_viewport": True,
                "headings": [],
                "contacts": {"phones": ["+79990000009"], "emails": []},
                "forms": {"count": 1},
                "technologies": [],
            }

        service = AutopilotService(
            self.crm,
            settings(retry_attempts=2),
            FakeSheets(),
            discoverer=discoverer,
            inspector=inspector,
        )
        self.assertTrue(service.start(mode="safe")["success"])
        result = service.run_cycle(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(discover_calls, 2)
        self.assertEqual(result["cycle"]["metrics"]["retry_count"], 1)
        self.assertEqual(len(self.crm.list_leads()), 1)
        lead = self.crm.list_leads()[0]
        with self.crm.connect() as connection:
            connection.execute(
                "UPDATE leads SET evidence_expires_at = '2000-01-01T00:00:00+00:00' "
                "WHERE id = ?",
                (lead["id"],),
            )
        repeated = service.run_cycle(force=True)
        repeated_metrics = repeated["cycle"]["metrics"]
        self.assertEqual(repeated_metrics["campaigns_reused"], 1)
        self.assertEqual(repeated_metrics["stale_evidence_refreshed"], 1)
        self.assertEqual(len(self.crm.list_campaigns()), 1)

    def test_google_sheets_request_retries_transient_failure(self) -> None:
        sync = GoogleSheetsSync(
            self.crm,
            enabled=False,
            spreadsheet_id=None,
            service_account_file=None,
            retry_attempts=2,
            retry_base_delay_seconds=0,
        )

        class FlakyRequest:
            calls = 0

            def execute(self) -> dict[str, bool]:
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("temporary sheets failure")
                return {"ok": True}

        request = FlakyRequest()
        self.assertEqual(sync._execute(request), {"ok": True})
        self.assertEqual(request.calls, 2)
        self.assertEqual(sync._retry_count, 1)

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

    def test_sheet_actions_require_approve_and_send_in_separate_syncs(self) -> None:
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
        self.assertEqual(result["send_requested_draft_ids"], [])
        self.assertIn(
            "previous Google Sheets sync", result["rejected_actions"][0]["reason"]
        )
        self.assertEqual(len(self.crm.list_pending_send_requests()), 0)

        lead_row[LEADS_HEADERS.index("APPROVE")] = ""
        result = sync._pull_actions(object())
        self.assertEqual(result["approved_draft_ids"], [])
        self.assertEqual(result["send_requested_draft_ids"], [draft["id"]])
        self.assertEqual(len(self.crm.list_pending_send_requests()), 1)

    def test_sheet_sync_updates_snapshot_before_clearing_stale_tails(self) -> None:
        events: list[tuple[str, object]] = []

        class Request:
            def __init__(self, name: str, body: object, result: object) -> None:
                self.name = name
                self.body = body
                self.result = result

            def execute(self) -> object:
                events.append((self.name, self.body))
                return self.result

        class Values:
            def batchUpdate(self, **kwargs: object) -> Request:
                return Request("update", kwargs["body"], {"totalUpdatedCells": 10})

            def batchClear(self, **kwargs: object) -> Request:
                return Request("clear", kwargs["body"], {})

        class Service:
            def __init__(self) -> None:
                self.values_api = Values()

            def spreadsheets(self) -> Service:
                return self

            def values(self) -> Values:
                return self.values_api

        credentials = Path(self.tempdir.name) / "credentials.json"
        credentials.write_text("{}", encoding="utf-8")
        sync = GoogleSheetsSync(
            self.crm,
            enabled=True,
            spreadsheet_id="sheet-id",
            service_account_file=str(credentials),
        )
        snapshot = {name: [["HEADER"], [f"{name}-row"]] for name in sync.tab_names}
        sync._build_service = lambda: Service()  # type: ignore[method-assign]
        sync._ensure_tabs = lambda _service: ({}, [])  # type: ignore[method-assign]
        sync._pull_actions = lambda _service: {}  # type: ignore[method-assign]
        sync._snapshot = lambda: snapshot  # type: ignore[method-assign]
        sync._format_new_tabs = lambda *_args: None  # type: ignore[method-assign]

        result = sync.sync()

        self.assertEqual([event[0] for event in events], ["update", "clear"])
        self.assertEqual(result["write_strategy"], "update_then_clear_tail")
        clear_body = events[1][1]
        self.assertEqual(
            clear_body["ranges"],
            [f"'{name}'!A3:Z" for name in sync.tab_names],
        )


if __name__ == "__main__":
    unittest.main()
