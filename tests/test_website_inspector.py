from __future__ import annotations

import unittest
from unittest.mock import patch

from app.website_inspector import inspect_website

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
        download.return_value = ("https://alpha.example/", HTML, "text/html")
        result = inspect_website("https://alpha.example")
        self.assertEqual(result["title"], "Alpha Construction")
        self.assertTrue(result["mobile_viewport"])
        self.assertIn("sales@alpha.example", result["contacts"]["emails"])
        self.assertIn("Yandex Metrica", result["technologies"])
        self.assertEqual(result["forms"]["count"], 1)
        self.assertNotIn("mc.yandex.ru", result["visible_text"])


if __name__ == "__main__":
    unittest.main()
