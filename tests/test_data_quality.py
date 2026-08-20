from __future__ import annotations

import unittest

from app.data_quality import (
    company_domain_key,
    company_name_key,
    is_technical_whatsapp_jid,
    normalize_phone,
    normalize_whatsapp_jid,
    retry_call,
)


class DataQualityTests(unittest.TestCase):
    def test_company_identity_normalization(self) -> None:
        self.assertEqual(
            company_domain_key("http://WWW.Example.COM:80/path"), "example.com"
        )
        self.assertEqual(company_name_key("ООО «Example-Service»"), "example service")
        self.assertEqual(normalize_phone("8 (999) 000-00-00"), "79990000000")

    def test_whatsapp_jid_normalization_and_technical_filter(self) -> None:
        self.assertEqual(
            normalize_whatsapp_jid("79990000000:42@S.WHATSAPP.NET"),
            "79990000000@s.whatsapp.net",
        )
        self.assertTrue(is_technical_whatsapp_jid("0@s.whatsapp.net"))
        self.assertTrue(is_technical_whatsapp_jid("status@broadcast"))

    def test_retry_call_retries_only_transient_errors(self) -> None:
        calls = 0

        def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("temporary")
            return "ok"

        result = retry_call(
            flaky, attempts=3, base_delay_seconds=0, sleep=lambda _: None
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 3)

        with self.assertRaises(ValueError):
            retry_call(
                lambda: (_ for _ in ()).throw(ValueError("permanent")),
                attempts=3,
                base_delay_seconds=0,
                sleep=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
