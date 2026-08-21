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
        self.assertEqual(scored["status"], "analyzed")
        scored = self.crm.score_lead(lead["id"], budget=70, timing=60, qualify_at=55)
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

    def test_deduplicates_by_domain_phone_and_cautious_name(self) -> None:
        domain_lead = self.crm.upsert_lead(
            "Example One", "http://www.example-dedupe.test/path"
        )
        by_domain = self.crm.upsert_lead(
            "Example One", "https://example-dedupe.test/other"
        )
        self.assertEqual(domain_lead["id"], by_domain["id"])

        phone_lead = self.crm.upsert_lead(
            "Phone Company",
            "https://phone-one.test",
            phones=["8 (999) 000-00-00"],
        )
        by_phone = self.crm.upsert_lead(
            "Different Name",
            "https://phone-two.test",
            phones=["+7 999 000 00 00"],
        )
        self.assertEqual(phone_lead["id"], by_phone["id"])
        matches = self.crm.find_leads_by_phone("+7 999 000-00-00")
        self.assertEqual(matches[0]["lead_id"], phone_lead["id"])

        name_lead = self.crm.upsert_lead(
            'ООО "Exact Company Name"',
            "https://name-one.test",
            location="Moscow",
        )
        by_name = self.crm.upsert_lead(
            "Exact Company Name",
            "https://name-two.test",
            location="moscow",
        )
        self.assertEqual(name_lead["id"], by_name["id"])

    def test_lookup_keys_are_indexed_and_rebuilt_by_migration(self) -> None:
        lead = self.crm.upsert_lead(
            "Indexed Company",
            "https://www.indexed-company.test/path",
            location="Moscow",
            phones=["+7 (999) 123-45-67"],
        )
        with self.crm.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
            index_names = {
                row["name"] for row in connection.execute("PRAGMA index_list(leads)")
            }
            self.assertIn("idx_leads_domain_key", index_names)
            self.assertIn("idx_leads_name_location", index_names)
            self.assertEqual(
                connection.execute(
                    "SELECT phone_key FROM lead_phone_keys WHERE lead_id = ?",
                    (lead["id"],),
                ).fetchone()["phone_key"],
                "79991234567",
            )
            connection.execute("PRAGMA user_version = 5")
            connection.execute(
                "UPDATE leads SET domain_key = NULL, name_key = NULL, location_key = NULL"
            )
            connection.execute("DELETE FROM lead_phone_keys")

        migrated = SalesCRM(Path(self.tempdir.name) / "sales.db")

        self.assertEqual(
            migrated.find_lead_by_website_url("http://indexed-company.test/other")[
                "id"
            ],
            lead["id"],
        )
        self.assertEqual(
            migrated.find_leads_by_phone("8 999 123-45-67")[0]["lead_id"],
            lead["id"],
        )

    def test_analysis_contact_changes_refresh_phone_lookup(self) -> None:
        lead = self.crm.upsert_lead("Analysis Contact", "https://analysis-contact.test")
        self.crm.save_analysis(
            lead["id"],
            {
                "company_name": "Analysis Contact",
                "summary": "Grounded summary",
                "contacts": {"phones": ["+7 999 555-44-33"]},
            },
        )

        matches = self.crm.find_leads_by_phone("8 (999) 555-44-33")

        self.assertEqual(matches[0]["lead_id"], lead["id"])

    def test_rank_filter_excludes_missing_and_expired_evidence(self) -> None:
        fresh = self.crm.upsert_lead("Fresh", "https://fresh-ranking.test")
        stale = self.crm.upsert_lead("Stale", "https://stale-ranking.test")
        missing = self.crm.upsert_lead("Missing", "https://missing-ranking.test")
        for lead in (fresh, stale, missing):
            self.crm.score_lead(lead["id"], fit=90, need=90, budget=90, timing=90)
        self.crm.save_inspection(
            fresh["id"], {"final_url": "https://fresh-ranking.test", "facts": ["x"]}
        )
        self.crm.save_inspection(
            stale["id"], {"final_url": "https://stale-ranking.test", "facts": ["x"]}
        )
        expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with self.crm.connect() as connection:
            connection.execute(
                "UPDATE leads SET evidence_expires_at = ? WHERE id = ?",
                (expired, stale["id"]),
            )

        ranked = self.crm.list_leads(fresh_evidence_only=True)

        self.assertEqual([item["id"] for item in ranked], [fresh["id"]])

    def test_cleanup_removes_only_legacy_production_check_artifacts(self) -> None:
        synthetic = self.crm.upsert_lead(
            "Synthetic",
            "https://synthetic-production-check.test",
            source="production-safe-check",
        )
        self.crm.save_outreach_draft(
            synthetic["id"],
            channel="whatsapp",
            recipient="79990000009",
            message="Synthetic draft",
        )
        real = self.crm.upsert_lead("Real", "https://real-company.test")

        removed = self.crm.remove_production_safe_check_artifacts()

        self.assertEqual(removed, 1)
        with self.assertRaisesRegex(ValueError, "lead not found"):
            self.crm.get_lead(synthetic["id"])
        self.assertEqual(self.crm.get_lead(real["id"])["id"], real["id"])

    def test_evidence_freshness_requires_reinspection_after_expiry(self) -> None:
        lead = self.crm.upsert_lead("Evidence", "https://evidence.test")
        self.crm.save_inspection(
            lead["id"],
            {"final_url": "https://evidence.test", "visible_text": "facts"},
            ttl_hours=24,
        )
        self.assertTrue(self.crm.require_fresh_evidence(lead["id"])["fresh"])
        expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with self.crm.connect() as connection:
            connection.execute(
                "UPDATE leads SET evidence_expires_at = ? WHERE id = ?",
                (expired, lead["id"]),
            )
        self.assertIsNone(self.crm.get_inspection(lead["id"]))
        self.assertFalse(self.crm.get_inspection(lead["id"], allow_stale=True)["fresh"])
        with self.assertRaisesRegex(ValueError, "Fresh website evidence"):
            self.crm.require_fresh_evidence(lead["id"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.crm.save_inspection(
                lead["id"], {"final_url": "https://unrelated.test"}
            )

    def test_expired_autopilot_lock_is_recovered(self) -> None:
        self.crm.start_autopilot(mode="safe")
        stale = self.crm.begin_autopilot_cycle()
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with self.crm.connect() as connection:
            connection.execute(
                "UPDATE autopilot_state SET lock_until = ? WHERE id = 1", (past,)
            )
        replacement = self.crm.begin_autopilot_cycle()
        self.assertNotEqual(stale["id"], replacement["id"])
        self.assertEqual(self.crm.get_autopilot_cycle(stale["id"])["status"], "failed")

    def test_autopilot_campaign_identity_is_reused(self) -> None:
        first, created = self.crm.get_or_create_campaign(
            "Autopilot — services — 2026-08-20",
            industry="services",
            location="Moscow",
        )
        second, created_again = self.crm.get_or_create_campaign(
            "Autopilot — services — 2026-08-20",
            industry="services",
            location="Moscow",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])

    def test_scoring_status_respects_qualification_threshold(self) -> None:
        lead = self.crm.upsert_lead("Threshold Example", "https://threshold.example")
        self.assertEqual(
            self.crm.find_lead_by_website_url("https://threshold.example/path")["id"],
            lead["id"],
        )
        below = self.crm.score_lead(
            lead["id"], fit=20, need=20, budget=20, timing=20, qualify_at=65
        )
        self.assertEqual(below["score"], 20)
        self.assertEqual(below["status"], "analyzed")
        above = self.crm.score_lead(
            lead["id"], fit=90, need=90, budget=90, timing=90, qualify_at=65
        )
        self.assertEqual(above["status"], "qualified")

    def test_reports_include_vertical_performance_and_conversion(self) -> None:
        vertical = self.crm.create_vertical("logistics", region="Moscow")
        campaign = self.crm.create_campaign(
            "Logistics", industry="logistics", location="Moscow"
        )
        cycle = self.crm.begin_autopilot_cycle(force=True)
        self.crm.register_autopilot_campaign(
            cycle_id=cycle["id"],
            campaign_id=campaign["id"],
            vertical_id=vertical["id"],
        )
        lead = self.crm.upsert_lead(
            "Logistics Example",
            "https://logistics.example",
            campaign_id=campaign["id"],
        )
        self.crm.score_lead(lead["id"], fit=90, need=80, budget=60, timing=50)
        self.crm.record_interaction(
            lead["id"],
            channel="whatsapp",
            direction="outbound",
            content="Hello",
        )
        self.crm.record_interaction(
            lead["id"],
            channel="whatsapp",
            direction="inbound",
            content="Interested",
        )
        performance = self.crm.vertical_performance()
        self.assertEqual(performance[0]["leads"], 1)
        self.assertEqual(performance[0]["replies"], 1)
        self.assertEqual(performance[0]["reply_rate"], 100.0)
        report = self.crm.conversion_report()
        self.assertEqual(report["stages"]["contacted"], 1)
        self.assertEqual(report["stages"]["replied"], 1)
        self.assertEqual(report["rates"]["reply_per_contacted"], 100.0)
        self.crm.complete_autopilot_cycle(cycle["id"], metrics={})

    def test_admin_audit_and_cycle_history_are_persistent_and_bounded(self) -> None:
        event = self.crm.record_admin_audit(
            actor="operator@example.com",
            action="lead.status.update",
            target_type="lead",
            target_id="lead-1",
            outcome="success",
            details={"status": "qualified"},
        )
        cycle = self.crm.begin_autopilot_cycle(force=True)
        self.crm.complete_autopilot_cycle(cycle["id"], metrics={"checked": 0})

        audit = self.crm.list_admin_audit(limit=1)
        cycles = self.crm.list_autopilot_cycles(limit=1)

        self.assertEqual(audit[0]["id"], event["id"])
        self.assertEqual(audit[0]["details"], {"status": "qualified"})
        self.assertEqual(cycles[0]["id"], cycle["id"])
        self.assertEqual(cycles[0]["status"], "completed")

    def test_workspace_bootstrap_invitation_and_roles_are_persistent(self) -> None:
        owner = self.crm.authorize_workspace_identity(
            workspace_id="ollum-group",
            workspace_name="Ollum Group",
            subject="auth0|owner",
            email="owner@example.com",
            display_name="Owner",
            bootstrap_allowed=True,
        )
        self.assertEqual(owner["role"], "owner")

        invitation = self.crm.invite_workspace_member(
            workspace_id="ollum-group",
            email="viewer@example.com",
            role="viewer",
            invited_by="owner@example.com",
        )
        viewer = self.crm.authorize_workspace_identity(
            workspace_id="ollum-group",
            workspace_name="Ollum Group",
            subject="auth0|viewer",
            email="viewer@example.com",
            display_name="Viewer",
            bootstrap_allowed=False,
        )
        self.assertEqual(viewer["role"], "viewer")
        self.assertEqual(
            self.crm.list_workspace_invitations(
                "ollum-group", status="accepted"
            )[0]["id"],
            invitation["id"],
        )

        with self.assertRaisesRegex(ValueError, "not invited"):
            self.crm.authorize_workspace_identity(
                workspace_id="ollum-group",
                workspace_name="Ollum Group",
                subject="auth0|outsider",
                email="outsider@example.com",
                display_name="Outsider",
                bootstrap_allowed=False,
            )
        with self.assertRaisesRegex(ValueError, "last workspace owner"):
            self.crm.update_workspace_member_role(
                workspace_id="ollum-group",
                member_id=owner["id"],
                role="operator",
            )


if __name__ == "__main__":
    unittest.main()
