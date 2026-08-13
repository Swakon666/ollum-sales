from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import Mock, patch

from app.candidate_quality import assess_company_candidate
from app.company_search import (
    _decode_bing_target,
    build_company_query,
    build_maps_query,
    parse_bing_results,
    parse_yandex_maps_results,
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
        self.assertIn("official company website", query)

    def test_parse_filters_directories_and_deduplicates_domains(self) -> None:
        results = parse_bing_results(SAMPLE_HTML, limit=10)
        self.assertEqual(
            [item["company_name"] for item in results],
            ["Alpha Logistics", "Beta Cargo"],
        )
        self.assertEqual(results[0]["website_url"], "https://alpha.example/")

    def test_parse_rejects_enterprise_and_editorial_results(self) -> None:
        html = """
        <li class="b_algo"><h2><a href="https://skillbox.ru/">Skillbox</a></h2></li>
        <li class="b_algo"><h2><a href="https://agency.example/blog/top-company">
          Топ компаний и рейтинг
        </a></h2></li>
        <li class="b_algo"><h2><a href="https://studio.example/services">
          Studio — официальный сайт
        </a></h2></li>
        """
        results = parse_bing_results(html, limit=10)
        self.assertEqual([item["company_name"] for item in results], ["Studio"])

    def test_yandex_maps_extracts_only_official_company_websites(self) -> None:
        payload = {
            "stack": [
                {
                    "results": {
                        "items": [
                            {
                                "id": "1",
                                "title": "Local School",
                                "address": "Moscow",
                                "urls": [
                                    "https://school.example/about?utm_source=maps"
                                ],
                                "categories": [{"name": "Private school"}],
                            },
                            {
                                "id": "2",
                                "title": "Federal Course",
                                "urls": ["https://skillbox.ru/course"],
                            },
                            {"id": "3", "title": "No site", "urls": []},
                        ]
                    }
                }
            ]
        }
        html = '<script type="application/json">' + json.dumps(payload) + "</script>"
        results = parse_yandex_maps_results(html, limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["company_name"], "Local School")
        self.assertEqual(results[0]["website_url"], "https://school.example/")
        self.assertIn("Private school", results[0]["snippet"])
        self.assertIn("частная школа", build_maps_query("образование", "Москва"))

    def test_post_fetch_quality_requires_a_commercial_company_site(self) -> None:
        accepted = assess_company_candidate(
            {"company_name": "Factory", "website_url": "https://factory.example/"},
            {
                "final_url": "https://factory.example/",
                "title": "Factory",
                "visible_text": "Наши услуги. Каталог продукции. Получить консультацию.",
                "headings": [],
                "contacts": {"phones": ["+79990000000"], "emails": []},
                "forms": {"count": 1},
            },
        )
        rejected = assess_company_candidate(
            {"company_name": "Guide", "website_url": "https://guide.example/"},
            {
                "final_url": "https://guide.example/article/what-is-crm",
                "title": "Что такое CRM: статья",
                "visible_text": "Определение. Автор статьи. Читать новости и блог.",
                "headings": [],
                "contacts": {"phones": [], "emails": []},
                "forms": {"count": 0},
            },
        )
        self.assertTrue(accepted["accepted"])
        self.assertFalse(rejected["accepted"])
        self.assertIn(rejected["reason"], {"editorial_path", "editorial_result"})

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
