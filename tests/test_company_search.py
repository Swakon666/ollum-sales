from __future__ import annotations

import base64
import unittest
from unittest.mock import Mock, patch

from app.company_search import (
    _decode_bing_target,
    build_company_query,
    parse_bing_results,
    search_company_websites,
)

SAMPLE_HTML = """
<html><body>
  <li class="b_algo">
    <h2><a href="https://alpha.example/services">Alpha Logistics</a></h2>
    <div class="b_caption"><p>Freight and warehouse services.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://vk.com/alpha">Alpha on VK</a></h2>
  </li>
  <li class="b_algo">
    <h2><a href="https://alpha.example/about">Duplicate Alpha</a></h2>
  </li>
  <li class="b_algo">
    <h2><a href="https://beta.example/">Beta Cargo</a></h2>
  </li>
</body></html>
"""


class CompanySearchTests(unittest.TestCase):
    def test_decodes_bing_tracking_target(self) -> None:
        target = "https://alpha.example/services"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        redirect = f"https://www.bing.com/ck/a?u=a1{encoded}&ntb=1"
        self.assertEqual(_decode_bing_target(redirect), target)

    def test_query_contains_market_and_location(self) -> None:
        query = build_company_query("логистика", "Москва", "B2B")
        self.assertIn("логистика", query)
        self.assertIn("Москва", query)
        self.assertIn("B2B", query)

    def test_parse_filters_directories_and_deduplicates_domains(self) -> None:
        results = parse_bing_results(SAMPLE_HTML, limit=10)
        self.assertEqual(
            [item["company_name"] for item in results],
            ["Alpha Logistics", "Beta Cargo"],
        )
        self.assertEqual(results[0]["website_url"], "https://alpha.example/")

    @patch("app.company_search.requests.post")
    def test_serper_results_are_filtered_and_normalized(self, post: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "organic": [
                {
                    "title": "Alpha Logistics Moscow",
                    "link": "https://alpha.example/services",
                    "snippet": "Logistics and freight services in Moscow.",
                },
                {
                    "title": "Alpha on LinkedIn",
                    "link": "https://linkedin.com/company/alpha",
                },
            ]
        }
        response.raise_for_status.return_value = None
        post.return_value = response

        result = search_company_websites(
            "logistics",
            "Moscow",
            serper_api_key="test-key",
            limit=10,
        )

        self.assertEqual(result["provider"], "serper")
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["results"][0]["website_url"], "https://alpha.example/")


if __name__ == "__main__":
    unittest.main()
