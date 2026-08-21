from __future__ import annotations

import unittest
from typing import ClassVar
from unittest.mock import patch

from app.website_inspector import MAX_HTML_BYTES, _download_html, inspect_website

HTML = """
<!doctype html>
<html lang="ru">
<head>
  <title>Alpha Construction</title>
  <meta name="description" content="We build residential projects">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://mc.yandex.ru/metrika/tag.js"></script>
</head>
<body>
  <h1>Residential construction</h1>
  <h2>Our projects</h2>
  <p>Call +7 (999) 000-00-00 or email sales@alpha.example.</p>
  <form><input type="tel"><button type="submit">Request a call</button></form>
  <a href="https://vk.com/alpha">VK</a>
</body>
</html>
"""


class WebsiteInspectorTests(unittest.TestCase):
    @patch("app.website_inspector._download_html")
    def test_extracts_grounded_evidence(self, download) -> None:
        download.return_value = ("https://alpha.example/", HTML, "text/html", False)
        result = inspect_website("https://alpha.example")
        self.assertEqual(result["title"], "Alpha Construction")
        self.assertTrue(result["mobile_viewport"])
        self.assertIn("sales@alpha.example", result["contacts"]["emails"])
        self.assertIn("Yandex Metrica", result["technologies"])
        self.assertEqual(result["forms"]["count"], 1)
        self.assertNotIn("mc.yandex.ru", result["visible_text"])
        self.assertFalse(result["html_truncated"])

    @patch("app.website_inspector.validate_public_http_url")
    @patch("app.website_inspector.requests.Session")
    def test_large_html_is_truncated_instead_of_rejected(
        self, session_factory, validate_url
    ) -> None:
        validate_url.side_effect = lambda value: value

        class Response:
            is_redirect = False
            is_permanent_redirect = False
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": "text/html; charset=utf-8"
            }
            encoding = "utf-8"
            apparent_encoding = "utf-8"

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def iter_content(*, chunk_size: int):
                self.assertEqual(chunk_size, 65536)
                yield b"a" * (MAX_HTML_BYTES + 100)

            @staticmethod
            def close() -> None:
                return None

        session_factory.return_value.get.return_value = Response()

        _, html, content_type, truncated = _download_html(
            "https://large.example", timeout=5
        )

        self.assertEqual(content_type, "text/html")
        self.assertEqual(len(html.encode("utf-8")), MAX_HTML_BYTES)
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()
