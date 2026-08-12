from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.crm import SalesCRM, canonical_company_url


class SalesCRMTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.crm = SalesCRM(Path(self.tempdir.name) / "sales.db")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_campaign_lead_analysis_scoring_and_overview(self) -> None:
        campaign = self.crm.create_campaign(
            "Logistics Moscow",
            industry="logistics",
            location="Moscow",
            target_count=50,
        )
        lead = self.crm.upsert_lead(
            "Example Logistics",
            "https://EXAMPLE.com/services?utm_source=test",
            campaign_id=campaign["id"],
            source_rank=1,
        )
        duplicate = self.crm.upsert_lead(
            "Example Logistics Updated",
            "https://example.com/about",
            campaign_id=campaign["id"],
            source_rank=2,
        )
        self.assertEqual(lead["id"], duplicate["id"])
        self.assertEqual(self.crm.get_campaign(campaign["id"])["lead_count"], 1)

        saved = self.crm.save_analysis(
            lead["id"],
            {
                "company_name": "Example Logistics",
                "summary": "Regional logistics operator.",
                "contacts": {"phones": ["+7 999 000-00-00"], "emails": []},
                "website_problems": ["Weak mobile navigation", "No quote form"],
                "opportunities": ["Shipment calculator"],
                "recommended_ollum_services": ["Website redesign", "Web application"],
                "outreach_angles": ["Improve mobile quote requests"],
            },
        )
        self.assertEqual(saved["status"], "analyzed")
        scored = self.crm.score_lead(lead["id"], budget=70, timing=60)
        self.assertEqual(scored["status"], "qualified")
        self.assertGreater(scored["score"], 0)
        self.assertEqual(self.crm.overview(campaign["id"])["lead_count"], 1)

    def test_outreach_interaction_and_followup_lifecycle(self) -> None:
        lead = self.crm.upsert_lead("Example", "https://example.org")
        draft = self.crm.save_outreach_draft(
            lead["id"],
            channel="whatsapp",
            recipient="79990000000",
            message="Hello from Ollum",
        )
        approved = self.crm.approve_outreach_draft(draft["id"])
        self.assertEqual(approved["status"], "approved")
        claimed = self.crm.claim_outreach_draft_for_send(draft["id"])
        self.assertEqual(claimed["status"], "sending")
        self.assertIsNone(self.crm.claim_outreach_draft_for_send(draft["id"]))
        sent = self.crm.mark_outreach_sent(draft["id"], success=True)
        self.assertEqual(sent["status"], "sent")
        interaction = self.crm.record_interaction(
            lead["id"],
            channel="whatsapp",
            direction="outbound",
            content="Hello from Ollum",
            status="sent",
        )
        self.assertEqual(interaction["direction"], "outbound")

        due_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        followup = self.crm.schedule_followup(
            lead["id"],
            due_at=due_at,
            action="Check response",
        )
        self.assertEqual(len(self.crm.list_due_followups()), 1)
        completed = self.crm.complete_followup(followup["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.crm.list_due_followups(), [])

    def test_blocked_send_claim_can_be_released(self) -> None:
        lead = self.crm.upsert_lead("Example", "https://example.net")
        draft = self.crm.save_outreach_draft(
            lead["id"],
            channel="whatsapp",
            recipient="79990000001",
            message="Reviewed text",
        )
        self.crm.approve_outreach_draft(draft["id"])
        self.crm.claim_outreach_draft_for_send(draft["id"])
        released = self.crm.release_outreach_send_claim(draft["id"])
        self.assertEqual(released["status"], "approved")

    def test_canonical_company_url_uses_site_root(self) -> None:
        self.assertEqual(
            canonical_company_url("HTTPS://Example.COM:443/path?q=1#fragment"),
            "https://example.com/",
        )


if __name__ == "__main__":
    unittest.main()
