from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.schemas import LeadAnalysis


class LeadAnalysisTests(unittest.TestCase):
    def test_codex_fallback_shape_is_validated(self) -> None:
        analysis = LeadAnalysis.model_validate(
            {
                "company_name": "Example Logistics",
                "industry": "Logistics",
                "summary": "Freight services supported by the inspected website.",
                "contacts": {
                    "phones": ["+7 999 000-00-00"],
                    "emails": [],
                    "messengers": [],
                    "social_links": [],
                },
                "website_strengths": ["Service catalogue is present"],
                "website_problems": ["No quote form was found"],
                "detected_tools": [],
                "opportunities": ["Add a structured quote flow"],
                "recommended_ollum_services": ["Website redesign"],
                "outreach_angles": ["Reduce friction in freight quote requests"],
                "lead_score": 64,
                "score_reason": "Visible need with a public contact.",
            }
        )
        self.assertEqual(analysis.lead_score, 64)

    def test_lead_score_is_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            LeadAnalysis.model_validate({"lead_score": 101})


if __name__ == "__main__":
    unittest.main()
