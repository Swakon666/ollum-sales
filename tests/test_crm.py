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

    def _complete_onboarding(self, workspace_id: str = "ollum-group") -> dict:
        state = self.crm.get_company_onboarding_state(workspace_id)
        return self.crm.complete_company_onboarding(
            workspace_id,
            confirm_ready=True,
            confirmed_revision=state["confirmation"]["required_revision"],
            summary_hash=state["confirmation"]["summary_hash"],
        )

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

    def test_overview_excludes_technical_whatsapp_contacts(self) -> None:
        self.crm.upsert_lead(
            "Grounded Prospect",
            "https://grounded-overview.test",
            source="autopilot:search",
        )
        self.crm.upsert_lead(
            "Technical WhatsApp Contact",
            "https://overview-contact.contact.invalid",
            industry="WhatsApp inbound",
            source="whatsapp_inbound",
            phones=["+79990000041"],
        )

        overview = self.crm.overview()

        self.assertEqual(overview["lead_count"], 1)
        self.assertEqual(overview["by_status"], {"new": 1})

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
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 14
            )
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
            self.crm.list_workspace_invitations("ollum-group", status="accepted")[0][
                "id"
            ],
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

    def test_company_onboarding_memory_is_structured_and_persistent(self) -> None:
        self.crm.ensure_workspace("ollum-group", "Ollum Group")
        initial = self.crm.get_company_onboarding_state("ollum-group")
        self.assertEqual(initial["completion_percent"], 0)
        self.assertLessEqual(len(initial["next_questions"]), 3)

        self.crm.update_company_profile(
            "ollum-group",
            company_name="Example Studio",
            website_url="https://example.com/about",
            industry="Digital services",
            geography="Russia",
            target_customer="B2B companies with an existing sales team",
            positioning="Grounded sales automation with human approval",
            sales_process="Discovery, qualification, proposal, close",
            tone_of_voice="Concise and respectful",
            primary_goal="Increase qualified conversations",
        )
        service = self.crm.save_company_knowledge(
            "ollum-group",
            category="service",
            title="Sales automation",
            content={"scope": "research, scoring and drafts"},
        )
        self.crm.save_company_knowledge(
            "ollum-group",
            category="price",
            title="Custom estimate",
            content={"rule": "after discovery"},
        )
        state = self.crm.get_company_onboarding_state("ollum-group")
        self.assertTrue(state["ready_for_sales"])
        completed = self.crm.complete_company_onboarding(
            "ollum-group",
            confirm_ready=True,
            confirmed_revision=state["confirmation"]["required_revision"],
            summary_hash=state["confirmation"]["summary_hash"],
        )
        self.assertEqual(completed["onboarding_status"], "ready")

        reopened = SalesCRM(self.crm.db_path)
        self.assertEqual(
            reopened.get_company_onboarding_state("ollum-group")["onboarding_status"],
            "ready",
        )
        archived = reopened.archive_company_knowledge("ollum-group", service["id"])
        self.assertEqual(archived["status"], "archived")

    def test_onboarding_confirmation_is_bound_to_exact_reviewed_revision(self) -> None:
        self.crm.ensure_workspace("ollum-group", "Ollum Group")
        self.crm.update_company_profile(
            "ollum-group",
            company_name="Example Studio",
            industry="Digital services",
            target_customer="B2B companies",
            positioning="Grounded sales automation",
        )
        for category, title in (("service", "Sales agent"), ("price", "Quote")):
            self.crm.save_company_knowledge(
                "ollum-group",
                category=category,
                title=title,
                content={"details": "Confirmed by operator"},
            )
        review = self.crm.get_company_onboarding_state("ollum-group")
        self.assertEqual(
            review["review_summary"]["profile"]["company_name"], "Example Studio"
        )
        self.assertEqual(len(review["confirmation"]["summary_hash"]), 64)

        self.crm.update_company_profile(
            "ollum-group", positioning="Updated grounded positioning"
        )
        with self.assertRaisesRegex(ValueError, "stale onboarding revision"):
            self.crm.complete_company_onboarding(
                "ollum-group",
                confirm_ready=True,
                confirmed_revision=review["confirmation"]["required_revision"],
                summary_hash=review["confirmation"]["summary_hash"],
            )

        current = self.crm.get_company_onboarding_state("ollum-group")
        completed = self.crm.complete_company_onboarding(
            "ollum-group",
            confirm_ready=True,
            confirmed_revision=current["confirmation"]["required_revision"],
            summary_hash=current["confirmation"]["summary_hash"],
        )
        self.assertTrue(completed["sales_ready"])
        self.assertEqual(
            completed["confirmation"]["confirmed_revision"],
            completed["profile"]["revision"],
        )
        self.assertEqual(
            completed["next_step"],
            "configure_primary_prospecting_and_create_whatsapp_monitor",
        )
        settings = self.crm.get_conversation_agent_settings("ollum-group")
        self.assertEqual(settings["niche"], "Digital services")
        self.assertEqual(settings["objective"], None)
        reopened = SalesCRM(self.crm.db_path).get_company_onboarding_state(
            "ollum-group"
        )
        self.assertTrue(reopened["sales_ready"])
        self.assertEqual(
            reopened["confirmation"]["confirmed_summary_hash"],
            current["confirmation"]["summary_hash"],
        )

    def test_required_knowledge_changes_reopen_completed_onboarding(self) -> None:
        self.crm.ensure_workspace("ollum-group", "Ollum Group")
        self.crm.update_company_profile(
            "ollum-group",
            company_name="Example Studio",
            industry="Digital services",
            target_customer="B2B companies",
            positioning="Grounded sales automation",
        )
        self.crm.save_company_knowledge(
            "ollum-group",
            category="service",
            title="Sales agent",
            content={"details": "Research and drafts"},
        )
        price = self.crm.save_company_knowledge(
            "ollum-group",
            category="price",
            title="Custom quote",
            content={"details": "Calculated after discovery"},
        )
        ready = self._complete_onboarding()
        self.assertTrue(ready["sales_ready"])

        self.crm.archive_company_knowledge("ollum-group", price["id"])
        reopened = self.crm.get_company_onboarding_state("ollum-group")

        self.assertFalse(reopened["ready_for_sales"])
        self.assertFalse(reopened["sales_ready"])
        self.assertEqual(reopened["onboarding_status"], "in_progress")
        self.assertIn("prices", reopened["missing"])

    def test_noop_profile_save_preserves_completed_onboarding(self) -> None:
        self.crm.ensure_workspace("ollum-group", "Ollum Group")
        profile = self.crm.update_company_profile(
            "ollum-group",
            company_name="Example Studio",
            industry="Digital services",
            target_customer="B2B companies",
            positioning="Grounded sales automation",
        )
        for category, title in (("service", "Sales agent"), ("price", "Quote")):
            self.crm.save_company_knowledge(
                "ollum-group",
                category=category,
                title=title,
                content={"details": "Confirmed by operator"},
            )
        ready = self._complete_onboarding()

        unchanged = self.crm.update_company_profile(
            "ollum-group",
            company_name=profile["company_name"],
            industry=profile["industry"],
            target_customer=profile["target_customer"],
            positioning=profile["positioning"],
        )

        self.assertEqual(unchanged["onboarding_status"], "ready")
        self.assertEqual(unchanged["revision"], ready["profile"]["revision"])
        self.assertEqual(unchanged["completed_at"], ready["profile"]["completed_at"])

    def test_empty_company_knowledge_cannot_satisfy_readiness(self) -> None:
        self.crm.ensure_workspace("ollum-group", "Ollum Group")
        for empty in (None, "", [], {}):
            with (
                self.subTest(empty=empty),
                self.assertRaisesRegex(ValueError, "grounded fact"),
            ):
                self.crm.save_company_knowledge(
                    "ollum-group",
                    category="price",
                    title="Placeholder",
                    content=empty,
                )

    def test_explicit_absence_answers_stop_optional_questions_repeating(self) -> None:
        self.crm.ensure_workspace("ollum-group", "Ollum Group")
        self.crm.update_company_profile(
            "ollum-group",
            company_name="Example Studio",
            industry="Digital services",
            geography="Worldwide",
            target_customer="B2B companies",
            positioning="Grounded sales automation",
            sales_process="Discovery, qualification, proposal",
            tone_of_voice="Concise",
            primary_goal="Qualified conversations",
        )
        for question_id in ("website", "customer_proof", "active_clients"):
            self.crm.record_company_onboarding_answer(
                "ollum-group",
                question_id=question_id,
                status="not_applicable",
                answer={"operator_confirmed": True},
            )

        state = self.crm.get_company_onboarding_state("ollum-group")
        question_ids = {item["id"] for item in state["next_questions"]}
        self.assertNotIn("website", question_ids)
        self.assertNotIn("closed_clients", question_ids)
        self.assertNotIn("pipeline", question_ids)
        self.assertNotIn("customer_proof", state["missing"])
        self.assertNotIn("active_clients", state["missing"])
        self.assertEqual(
            SalesCRM(self.crm.db_path).get_company_onboarding_state("ollum-group")[
                "answers"
            ]["website"]["status"],
            "not_applicable",
        )

    def test_agent_inbox_is_idempotent_and_tracks_draft_state(self) -> None:
        self.crm.ensure_workspace("ollum-group", "Ollum Group")
        lead = self.crm.upsert_lead(
            "Example Lead",
            "https://example-lead.test",
            phones=["+7 999 123-45-67"],
        )
        event, created = self.crm.upsert_agent_inbox_event(
            "ollum-group",
            external_id="wa-message-1",
            chat_jid="79991234567@s.whatsapp.net",
            message_text="Расскажите о сроках",
            received_at="2026-08-23T10:00:00+00:00",
            lead_id=lead["id"],
        )
        duplicate, created_again = self.crm.upsert_agent_inbox_event(
            "ollum-group",
            external_id="wa-message-1",
            chat_jid="79991234567@s.whatsapp.net",
            message_text="Расскажите о сроках",
            received_at="2026-08-23T10:00:00+00:00",
            lead_id=lead["id"],
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(duplicate["id"], event["id"])
        self.assertEqual(self.crm.agent_inbox_summary("ollum-group")["new"], 1)

        unmatched, _created = self.crm.upsert_agent_inbox_event(
            "ollum-group",
            external_id="wa-message-2",
            chat_jid="79990000000@s.whatsapp.net",
            message_text="Можно уточнить детали?",
            received_at="2026-08-23T10:01:00+00:00",
        )
        linked = self.crm.link_agent_inbox_event(
            "ollum-group", unmatched["id"], lead["id"]
        )
        self.assertEqual(linked["lead_id"], lead["id"])

        draft = self.crm.save_outreach_draft(
            lead["id"],
            channel="whatsapp",
            recipient="+79991234567",
            message="Подготовим подтверждённую оценку после короткого брифа.",
        )
        updated = self.crm.update_agent_inbox_event(
            "ollum-group", event["id"], status="drafted", draft_id=draft["id"]
        )
        self.assertEqual(updated["draft_id"], draft["id"])
        self.assertEqual(self.crm.agent_inbox_summary("ollum-group")["drafted"], 1)

        resolved_count = self.crm.resolve_agent_inbox_for_draft(
            "ollum-group", draft["id"]
        )
        self.assertEqual(resolved_count, 1)
        self.assertEqual(
            self.crm.get_agent_inbox_event("ollum-group", event["id"])["status"],
            "resolved",
        )

    def test_inbound_sync_tracks_replies_once_and_coordination_hides_messages(
        self,
    ) -> None:
        workspace_id = "ollum-group"
        self.crm.ensure_workspace(workspace_id, "Ollum Group")
        replied_lead = self.crm.upsert_lead(
            "Replied Company",
            "https://replied-company.test",
            phones=["+79990000011"],
        )
        silent_lead = self.crm.upsert_lead(
            "Silent Company",
            "https://silent-company.test",
            phones=["+79990000012"],
        )
        unsolicited_lead = self.crm.upsert_lead(
            "Inbound Company",
            "https://inbound-company.test",
            phones=["+79990000013"],
        )
        for lead, external_id in (
            (replied_lead, "outbound-replied"),
            (silent_lead, "outbound-silent"),
        ):
            self.crm.record_interaction(
                lead["id"],
                channel="whatsapp",
                direction="outbound",
                content="Sent outreach",
                external_id=external_id,
                occurred_at="2026-08-23T10:00:00+00:00",
            )

        event, created = self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="reply-message-1",
            chat_jid="79990000011@s.whatsapp.net",
            message_text="Private reply text must stay out of coordination reports",
            received_at="2026-08-23T11:00:00+00:00",
            lead_id=replied_lead["id"],
        )
        duplicate, created_again = self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="reply-message-1",
            chat_jid="79990000011@s.whatsapp.net",
            message_text="Private reply text must stay out of coordination reports",
            received_at="2026-08-23T11:00:00+00:00",
            lead_id=replied_lead["id"],
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(event["id"], duplicate["id"])
        self.assertEqual(self.crm.get_lead(replied_lead["id"])["status"], "replied")

        self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="unsolicited-message-1",
            chat_jid="79990000013@s.whatsapp.net",
            message_text="Unsolicited inbound",
            received_at="2026-08-23T11:05:00+00:00",
            lead_id=unsolicited_lead["id"],
        )
        self.assertEqual(self.crm.get_lead(unsolicited_lead["id"])["status"], "new")

        with self.crm.connect() as connection:
            inbox_interactions = connection.execute(
                """
                SELECT COUNT(*) FROM interactions
                WHERE lead_id = ? AND direction = 'inbound'
                  AND external_id LIKE 'agent_inbox:%'
                """,
                (replied_lead["id"],),
            ).fetchone()[0]
        self.assertEqual(inbox_interactions, 1)

        summary = self.crm.agent_coordination_summary(
            workspace_id, include_leads=True, limit=10
        )
        self.assertEqual(summary["execution_mode"], "chatgpt_mcp_two_chat")
        self.assertEqual(summary["responses"]["contacted"], 2)
        self.assertEqual(summary["responses"]["replied"], 1)
        self.assertEqual(summary["responses"]["never_replied"], 1)
        self.assertEqual(summary["responses"]["awaiting_reply"], 1)
        self.assertEqual(summary["responses"]["reply_rate_percent"], 50.0)
        self.assertEqual(
            summary["responses"]["replied_leads"][0]["company_name"],
            "Replied Company",
        )
        self.assertEqual(
            summary["responses"]["never_replied_leads"][0]["company_name"],
            "Silent Company",
        )
        self.assertNotIn("Private reply text", str(summary))
        self.assertFalse(summary["safety"]["private_message_text_included"])

    def test_coordination_keeps_whatsapp_contacts_out_of_prospecting(self) -> None:
        workspace_id = "ollum-group"
        self.crm.ensure_workspace(workspace_id, "Ollum Group")
        prospect = self.crm.upsert_lead(
            "Grounded Prospect",
            "https://grounded-prospect.test",
            source="autopilot:search",
        )
        inbound_contact = self.crm.upsert_lead(
            "Technical WhatsApp Contact",
            "https://wa-contact.contact.invalid",
            industry="WhatsApp inbound",
            source="whatsapp_inbound",
            phones=["+79990000021"],
        )
        self.crm.save_outreach_draft(
            prospect["id"],
            channel="whatsapp",
            message="Grounded prospecting draft",
            recipient="+79990000020",
        )
        reply_draft = self.crm.save_outreach_draft(
            inbound_contact["id"],
            channel="whatsapp",
            message="Inbound reply draft",
            recipient="+79990000021",
        )
        self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="technical-inbound-1",
            chat_jid="79990000021@s.whatsapp.net",
            message_text="Untrusted inbound text",
            received_at="2026-08-25T05:00:00+00:00",
            lead_id=inbound_contact["id"],
        )
        event = self.crm.list_agent_inbox_events(workspace_id, status="new", limit=1)[0]
        self.crm.update_agent_inbox_event(
            workspace_id,
            event["id"],
            status="drafted",
            draft_id=reply_draft["id"],
        )

        summary = self.crm.agent_coordination_summary(workspace_id)

        self.assertEqual(summary["lanes"]["prospecting"]["total_leads"], 1)
        self.assertEqual(summary["lanes"]["prospecting"]["unreviewed"], 0)
        self.assertEqual(summary["lanes"]["prospecting"]["drafts_waiting_review"], 1)
        self.assertEqual(summary["lanes"]["inbox"]["drafted"], 1)

    def test_conversation_agent_settings_sessions_and_queue_lease_are_persistent(
        self,
    ) -> None:
        workspace_id = "ollum-group"
        self.crm.ensure_workspace(workspace_id, "Ollum Group")
        defaults = self.crm.get_conversation_agent_settings(workspace_id)
        self.assertTrue(defaults["enabled"])
        self.assertFalse(defaults["send_enabled"])

        updated = self.crm.update_conversation_agent_settings(
            workspace_id,
            niche="e-commerce",
            tone="Кратко и по делу",
            qualification_questions=["Какой каталог?", "Какая география?"],
            forbidden_topics=["Неподтверждённые скидки"],
            escalation_rules=["Передать менеджеру вопросы по договору"],
            max_context_messages=999,
            max_reply_chars=20,
            response_sla_minutes=1,
            confidence_threshold=99,
        )
        self.assertEqual(updated["niche"], "e-commerce")
        self.assertEqual(updated["max_context_messages"], 30)
        self.assertEqual(updated["max_reply_chars"], 120)
        self.assertEqual(updated["response_sla_minutes"], 5)
        self.assertEqual(updated["confidence_threshold"], 95)
        self.assertEqual(len(updated["qualification_questions"]), 2)

        lead = self.crm.upsert_lead(
            "Conversation Lead",
            "https://conversation-lease.test",
            phones=["+79990000001"],
        )
        event, _created = self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="lease-event",
            chat_jid="79990000001@s.whatsapp.net",
            message_text="Есть вопрос",
            received_at="2026-08-23T12:00:00+00:00",
            lead_id=lead["id"],
        )
        claimed = self.crm.claim_next_agent_inbox_event(workspace_id)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["id"], event["id"])
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["agent_attempts"], 1)
        self.assertIsNone(self.crm.claim_next_agent_inbox_event(workspace_id))

        self.crm.finish_agent_inbox_event(
            workspace_id,
            event["id"],
            status="new",
            error="temporary",
        )
        retried = self.crm.claim_next_agent_inbox_event(workspace_id)
        assert retried is not None
        self.assertEqual(retried["agent_attempts"], 2)
        self.crm.finish_agent_inbox_event(
            workspace_id,
            event["id"],
            status="needs_review",
            decision={"action": "escalate", "approved": False, "sent": False},
        )

        first = self.crm.upsert_conversation_session(
            workspace_id,
            "79990000001@s.whatsapp.net",
            lead_id=lead["id"],
            stage="qualification",
            intent="price",
            summary="Уточняет стоимость",
            facts={"budget_known": False},
            increment_turn=True,
        )
        second = self.crm.upsert_conversation_session(
            workspace_id,
            "79990000001@s.whatsapp.net",
            lead_id=lead["id"],
            stage="interested",
            intent="demo",
            summary="Готов посмотреть решение",
            facts={"budget_known": False, "demo_requested": True},
            increment_turn=True,
        )
        self.assertEqual(first["turn_count"], 1)
        self.assertEqual(second["turn_count"], 2)

        reopened = SalesCRM(self.crm.db_path)
        self.assertEqual(
            reopened.get_conversation_agent_settings(workspace_id)["niche"],
            "e-commerce",
        )
        session = reopened.get_conversation_session(
            workspace_id, "79990000001@s.whatsapp.net"
        )
        assert session is not None
        self.assertEqual(session["stage"], "interested")
        self.assertTrue(session["facts"]["demo_requested"])

    def test_existing_conversation_settings_schema_is_migrated(self) -> None:
        workspace_id = "ollum-group"
        self.crm.ensure_workspace(workspace_id, "Ollum Group")
        self.crm.get_conversation_agent_settings(workspace_id)
        with self.crm.connect() as connection:
            connection.execute(
                "ALTER TABLE conversation_agent_settings "
                "DROP COLUMN max_inbound_age_hours"
            )
            connection.execute(
                "ALTER TABLE conversation_agent_settings "
                "DROP COLUMN response_sla_minutes"
            )
            connection.execute("PRAGMA user_version = 10")

        migrated = SalesCRM(self.crm.db_path)

        self.assertEqual(
            migrated.get_conversation_agent_settings(workspace_id)[
                "max_inbound_age_hours"
            ],
            168,
        )
        self.assertEqual(
            migrated.get_conversation_agent_settings(workspace_id)[
                "response_sla_minutes"
            ],
            60,
        )
        with migrated.connect() as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 14
            )

    def test_conversation_queue_watchdog_recovers_and_quarantines_safely(
        self,
    ) -> None:
        workspace_id = "ollum-group"
        self.crm.ensure_workspace(workspace_id, "Ollum Group")
        updated = self.crm.update_conversation_agent_settings(
            workspace_id, max_inbound_age_hours=9999
        )
        self.assertEqual(updated["max_inbound_age_hours"], 720)
        self.crm.update_conversation_agent_settings(
            workspace_id, max_inbound_age_hours=168
        )
        lead = self.crm.upsert_lead(
            "Queue Watchdog Lead",
            "https://queue-watchdog.test",
            phones=["+79990000002"],
        )
        now = datetime.now(UTC)

        def add_event(external_id: str, received_at: datetime) -> dict:
            return self.crm.upsert_agent_inbox_event(
                workspace_id,
                external_id=external_id,
                chat_jid=f"7999000{external_id[-4:]}@s.whatsapp.net",
                message_text="Test inbound",
                received_at=received_at.isoformat(timespec="seconds"),
                lead_id=lead["id"],
            )[0]

        stale = add_event("stale-0001", now - timedelta(days=8))
        requeue = add_event("requeue-0002", now - timedelta(hours=1))
        exhausted = add_event("exhausted-0003", now - timedelta(hours=1))
        active_stale = add_event("active-0004", now - timedelta(days=8))
        expired_at = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
        active_until = (now + timedelta(minutes=10)).isoformat(timespec="seconds")
        with self.crm.connect() as connection:
            connection.execute(
                "UPDATE agent_inbox_events SET status = 'processing', "
                "agent_attempts = 1, agent_lock_until = ? WHERE id = ?",
                (expired_at, requeue["id"]),
            )
            connection.execute(
                "UPDATE agent_inbox_events SET status = 'processing', "
                "agent_attempts = 3, agent_lock_until = ? WHERE id = ?",
                (expired_at, exhausted["id"]),
            )
            connection.execute(
                "UPDATE agent_inbox_events SET status = 'processing', "
                "agent_attempts = 1, agent_lock_until = ? WHERE id = ?",
                (active_until, active_stale["id"]),
            )

        result = self.crm.recover_expired_agent_inbox_leases(
            workspace_id, max_inbound_age_hours=168, max_attempts=3
        )

        self.assertEqual(result["stale_quarantined"], 1)
        self.assertEqual(result["leases_requeued"], 1)
        self.assertEqual(result["leases_exhausted"], 1)
        self.assertFalse(result["sent"])
        self.assertEqual(
            self.crm.get_agent_inbox_event(workspace_id, stale["id"])["status"],
            "needs_review",
        )
        self.assertEqual(
            self.crm.get_agent_inbox_event(workspace_id, requeue["id"])["status"],
            "new",
        )
        self.assertEqual(
            self.crm.get_agent_inbox_event(workspace_id, exhausted["id"])["status"],
            "needs_review",
        )
        self.assertEqual(
            self.crm.get_agent_inbox_event(workspace_id, active_stale["id"])["status"],
            "processing",
        )
        summary = self.crm.agent_inbox_summary(workspace_id)
        self.assertEqual(summary["processing_active"], 1)
        self.assertEqual(summary["processing_expired"], 0)
        self.assertEqual(summary["stale_actionable"], 1)

    def test_inbox_operations_report_sla_and_retry_only_fresh_reviewed_events(
        self,
    ) -> None:
        workspace_id = "ollum-group"
        self.crm.ensure_workspace(workspace_id, "Ollum Group")
        self.crm.update_conversation_agent_settings(
            workspace_id,
            response_sla_minutes=30,
            max_inbound_age_hours=24,
        )
        lead = self.crm.upsert_lead(
            "Inbox Operations Lead",
            "https://inbox-operations.test",
            phones=["+79990000003"],
        )
        now = datetime.now(UTC)

        fresh = self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="retry-fresh",
            chat_jid="79990000003@s.whatsapp.net",
            message_text="Fresh inbound",
            received_at=(now - timedelta(minutes=45)).isoformat(timespec="seconds"),
            lead_id=lead["id"],
        )[0]
        stale = self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="retry-stale",
            chat_jid="79990000004@s.whatsapp.net",
            message_text="Stale inbound",
            received_at=(now - timedelta(days=2)).isoformat(timespec="seconds"),
            lead_id=lead["id"],
        )[0]
        unlinked = self.crm.upsert_agent_inbox_event(
            workspace_id,
            external_id="retry-unlinked",
            chat_jid="79990000005@s.whatsapp.net",
            message_text="Unlinked inbound",
            received_at=(now - timedelta(minutes=5)).isoformat(timespec="seconds"),
        )[0]
        for event in (fresh, stale, unlinked):
            self.crm.finish_agent_inbox_event(
                workspace_id,
                event["id"],
                status="needs_review",
                decision={"action": "escalate"},
                error="requires operator",
            )
            with self.crm.connect() as connection:
                connection.execute(
                    "UPDATE agent_inbox_events SET agent_attempts = 3 WHERE id = ?",
                    (event["id"],),
                )

        operational = {
            item["id"]: item for item in self.crm.list_agent_inbox_events(workspace_id)
        }
        self.assertTrue(operational[fresh["id"]]["retryable"])
        self.assertEqual(operational[fresh["id"]]["sla_state"], "overdue")
        self.assertEqual(
            operational[stale["id"]]["retry_block_reason"], "inbound_expired"
        )
        self.assertEqual(
            operational[unlinked["id"]]["retry_block_reason"], "lead_not_linked"
        )

        summary = self.crm.agent_inbox_summary(workspace_id)
        self.assertEqual(summary["response_sla_minutes"], 30)
        self.assertEqual(summary["sla_overdue"], 2)
        self.assertGreaterEqual(summary["oldest_open_minutes"], 2 * 24 * 60)

        retried = self.crm.requeue_agent_inbox_event(workspace_id, fresh["id"])
        self.assertTrue(retried["requeued"])
        self.assertEqual(retried["status"], "new")
        self.assertEqual(retried["agent_attempts"], 0)
        self.assertIsNone(retried["agent_error"])
        self.assertEqual(retried["decision"], {})
        self.assertEqual(retried["previous_status"], "needs_review")
        duplicate = self.crm.requeue_agent_inbox_event(workspace_id, fresh["id"])
        self.assertTrue(duplicate["idempotent"])
        self.assertFalse(duplicate["requeued"])
        self.assertEqual(duplicate["previous_status"], "new")

        with self.assertRaisesRegex(ValueError, "outside max_inbound_age_hours"):
            self.crm.requeue_agent_inbox_event(workspace_id, stale["id"])
        with self.assertRaisesRegex(ValueError, "link the event"):
            self.crm.requeue_agent_inbox_event(workspace_id, unlinked["id"])


if __name__ == "__main__":
    unittest.main()
