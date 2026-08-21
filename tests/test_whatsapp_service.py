from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from app import whatsapp_service


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class TestWhatsAppService(unittest.TestCase):
    def test_bridge_status_whitelists_ready_payload(self) -> None:
        def fake_get(url: str, timeout: float) -> FakeResponse:
            assert url.endswith("/api/status")
            assert timeout == 1.5
            return FakeResponse(
                200,
                {
                    "status": "ready",
                    "ready": True,
                    "connected": True,
                    "logged_in": True,
                    "send_enabled": False,
                    "account_jid": "123456789@s.whatsapp.net",
                    "uptime_seconds": 42,
                    "session_secret": "must-not-leak",
                },
            )

        with patch.object(whatsapp_service.requests, "get", side_effect=fake_get):
            result = whatsapp_service.bridge_status(timeout_seconds=1.5)

        self.assertEqual(
            result,
            {
                "reachable": True,
                "http_status": 200,
                "status": "ready",
                "ready": True,
                "connected": True,
                "logged_in": True,
                "send_enabled": False,
                "account_jid": "123456789@s.whatsapp.net",
                "uptime_seconds": 42,
            },
        )

    def test_bridge_status_handles_unreachable_bridge(self) -> None:
        def fake_get(url: str, timeout: float) -> FakeResponse:
            raise requests.ConnectionError("bridge unavailable")

        with patch.object(whatsapp_service.requests, "get", side_effect=fake_get):
            result = whatsapp_service.bridge_status()

        self.assertFalse(result["reachable"])
        self.assertFalse(result["ready"])
        self.assertFalse(result["send_enabled"])
        self.assertEqual(result["error"], "ConnectionError")

    def test_pairing_status_whitelists_metadata_and_never_returns_code(self) -> None:
        with patch.object(
            whatsapp_service.requests,
            "get",
            return_value=FakeResponse(
                200,
                {
                    "state": "waiting_for_scan",
                    "needs_pairing": True,
                    "has_qr": True,
                    "generation": 4,
                    "updated_at": "2026-08-21T12:00:00Z",
                    "expires_at": "2026-08-21T12:00:30Z",
                    "qr_code": "must-not-leak",
                },
            ),
        ):
            result = whatsapp_service.bridge_pairing_status()

        self.assertTrue(result["reachable"])
        self.assertTrue(result["has_qr"])
        self.assertEqual(result["generation"], 4)
        self.assertNotIn("qr_code", result)
        self.assertNotIn("must-not-leak", repr(result))

    def test_pairing_qr_accepts_only_bounded_png_response(self) -> None:
        png = b"\x89PNG\r\n\x1a\nimage"
        with patch.object(
            whatsapp_service.requests,
            "get",
            return_value=FakeResponse(
                200,
                {},
                content=png,
                headers={"content-type": "image/png"},
            ),
        ):
            self.assertEqual(whatsapp_service.bridge_pairing_qr(), png)

        with patch.object(
            whatsapp_service.requests,
            "get",
            return_value=FakeResponse(
                200,
                {},
                content=b"not-an-image",
                headers={"content-type": "text/plain"},
            ),
        ):
            self.assertIsNone(whatsapp_service.bridge_pairing_qr())

    def test_contacts_are_normalized_and_technical_jids_are_filtered(self) -> None:
        contacts = [
            {
                "jid": "79990000000:17@s.whatsapp.net",
                "phone_number": "79990000000:17",
                "name": "Customer",
            },
            {"jid": "0@s.whatsapp.net", "phone_number": "0", "name": "System"},
            {"jid": "status@broadcast", "phone_number": "status", "name": None},
        ]
        with patch.object(
            whatsapp_service.wa, "search_contacts", return_value=contacts
        ):
            result = whatsapp_service.search_contacts("customer")

        self.assertEqual(
            result,
            [
                {
                    "jid": "79990000000@s.whatsapp.net",
                    "phone_number": "79990000000",
                    "name": "Customer",
                }
            ],
        )

    def test_technical_recipient_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Technical WhatsApp JIDs"):
            whatsapp_service.normalize_recipient("0@s.whatsapp.net")
