from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
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
    @staticmethod
    def _create_message_database(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE chats (
                jid TEXT PRIMARY KEY,
                name TEXT,
                last_message_time TIMESTAMP
            );
            CREATE TABLE messages (
                id TEXT,
                chat_jid TEXT,
                sender TEXT,
                content TEXT,
                timestamp TIMESTAMP,
                is_from_me BOOLEAN,
                media_type TEXT,
                PRIMARY KEY (id, chat_jid)
            );
            """
        )
        connection.execute(
            "INSERT INTO chats VALUES (?, ?, ?)",
            (
                "79990000000:17@s.whatsapp.net",
                "Test contact",
                "2026-08-22T10:01:00+00:00",
            ),
        )
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "in-1",
                    "79990000000:17@s.whatsapp.net",
                    "79990000000@s.whatsapp.net",
                    "Сколько стоит такой проект?",
                    "2026-08-22T10:00:00+00:00",
                    0,
                    None,
                ),
                (
                    "out-1",
                    "79990000000:17@s.whatsapp.net",
                    "79991111111@s.whatsapp.net",
                    "Уточню объём и подготовлю оценку.",
                    "2026-08-22T10:01:00+00:00",
                    1,
                    None,
                ),
            ],
        )
        connection.commit()
        connection.close()

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
                    "test_send_enabled": True,
                    "test_recipient_count": 1,
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
                "test_send_enabled": True,
                "test_recipient_count": 1,
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

    def test_send_message_blocks_recipient_outside_test_allowlist(self) -> None:
        test_settings = replace(
            whatsapp_service.settings,
            allow_whatsapp_send=False,
            whatsapp_test_recipients=("79779335513",),
        )
        with (
            patch.object(whatsapp_service, "settings", test_settings),
            patch.object(whatsapp_service.wa, "send_message") as send,
        ):
            result = whatsapp_service.send_message("79990000000", "test")

        self.assertTrue(result["blocked"])
        self.assertFalse(result["success"])
        send.assert_not_called()

    def test_send_message_allows_only_normalized_test_recipient(self) -> None:
        test_settings = replace(
            whatsapp_service.settings,
            allow_whatsapp_send=False,
            whatsapp_test_recipients=("+7 (977) 933-55-13",),
        )
        with (
            patch.object(whatsapp_service, "settings", test_settings),
            patch.object(
                whatsapp_service.wa,
                "send_message",
                return_value=(True, "sent"),
            ) as send,
        ):
            result = whatsapp_service.send_message(
                "79779335513@s.whatsapp.net",
                "test",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["recipient"], "79779335513")
        self.assertEqual(result["send_policy"], "test_recipient")
        send.assert_called_once_with("79779335513", "test")

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

    def test_list_messages_returns_structured_records_in_latest_first_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "messages.db"
            self._create_message_database(database_path)
            with patch.object(
                whatsapp_service.wa,
                "MESSAGES_DB_PATH",
                str(database_path),
            ):
                records = whatsapp_service.list_messages(
                    chat_jid="79990000000@s.whatsapp.net",
                    limit=1_000,
                )

        self.assertEqual([record["id"] for record in records], ["out-1", "in-1"])
        self.assertTrue(records[0]["is_from_me"])
        self.assertFalse(records[1]["is_from_me"])
        self.assertEqual(
            records[0]["chat_jid"],
            "79990000000@s.whatsapp.net",
        )
        self.assertIsInstance(records[0]["content"], str)

    def test_latest_unanswered_inbound_does_not_reuse_an_already_replied_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "messages.db"
            self._create_message_database(database_path)
            with patch.object(
                whatsapp_service.wa,
                "MESSAGES_DB_PATH",
                str(database_path),
            ):
                self.assertIsNone(
                    whatsapp_service.get_latest_unanswered_inbound_message(
                        "79990000000@s.whatsapp.net"
                    )
                )

                connection = sqlite3.connect(database_path)
                connection.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "in-2",
                        "79990000000:17@s.whatsapp.net",
                        "79990000000@s.whatsapp.net",
                        "Можно увидеть пример?",
                        "2026-08-22T10:02:00+00:00",
                        0,
                        None,
                    ),
                )
                connection.commit()
                connection.close()

                inbound = whatsapp_service.get_latest_unanswered_inbound_message(
                    "79990000000@s.whatsapp.net"
                )

        self.assertIsNotNone(inbound)
        assert inbound is not None
        self.assertEqual(inbound["id"], "in-2")
        self.assertEqual(inbound["content"], "Можно увидеть пример?")
        self.assertEqual(
            set(inbound),
            {"id", "timestamp", "chat_jid", "content", "media_type"},
        )
