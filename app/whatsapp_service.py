from __future__ import annotations

import importlib.util
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import requests

from .config import settings
from .data_quality import (
    is_technical_whatsapp_jid,
    normalize_phone,
    normalize_whatsapp_jid,
    normalize_whatsapp_records,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_WHATSAPP_SERVER = (
    REPO_ROOT / "upstream" / "whatsapp-mcp" / "whatsapp-mcp-server"
)
UPSTREAM_WHATSAPP_MODULE = UPSTREAM_WHATSAPP_SERVER / "whatsapp.py"


def _load_upstream_whatsapp():
    """Load the original upstream whatsapp.py without modifying its source tree."""
    if not UPSTREAM_WHATSAPP_MODULE.exists():
        raise RuntimeError(
            f"Upstream WhatsApp module not found: {UPSTREAM_WHATSAPP_MODULE}"
        )

    # Upstream whatsapp.py imports audio.py as a top-level module.
    server_path = str(UPSTREAM_WHATSAPP_SERVER)
    if server_path not in sys.path:
        sys.path.insert(0, server_path)

    spec = importlib.util.spec_from_file_location(
        "ollum_upstream_whatsapp", UPSTREAM_WHATSAPP_MODULE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not create import spec for upstream WhatsApp module")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Keep upstream files pristine; adapt runtime paths here instead.
    module.MESSAGES_DB_PATH = settings.whatsapp_db_path
    module.WHATSAPP_API_BASE_URL = settings.whatsapp_api_base_url
    return module


wa = _load_upstream_whatsapp()


def bridge_status(timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Return a whitelisted bridge status without exposing session data."""
    status_url = f"{settings.whatsapp_api_base_url.rstrip('/')}/status"
    unavailable = {
        "reachable": False,
        "ready": False,
        "connected": False,
        "logged_in": False,
        "send_enabled": False,
        "test_send_enabled": False,
        "test_recipient_count": 0,
    }
    try:
        response = requests.get(status_url, timeout=timeout_seconds)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {**unavailable, "error": type(exc).__name__}

    if not isinstance(payload, dict):
        return {**unavailable, "error": "InvalidStatusPayload"}

    account_jid = payload.get("account_jid")
    if isinstance(account_jid, str):
        try:
            account_jid = normalize_whatsapp_jid(account_jid)
        except ValueError:
            account_jid = None
    uptime_seconds = payload.get("uptime_seconds")
    return {
        "reachable": True,
        "http_status": response.status_code,
        "status": payload.get("status")
        if isinstance(payload.get("status"), str)
        else "unknown",
        "ready": payload.get("ready") is True,
        "connected": payload.get("connected") is True,
        "logged_in": payload.get("logged_in") is True,
        "send_enabled": payload.get("send_enabled") is True,
        "test_send_enabled": payload.get("test_send_enabled") is True,
        "test_recipient_count": (
            payload.get("test_recipient_count")
            if isinstance(payload.get("test_recipient_count"), int)
            and not isinstance(payload.get("test_recipient_count"), bool)
            and payload.get("test_recipient_count") >= 0
            else 0
        ),
        "account_jid": account_jid if isinstance(account_jid, str) else None,
        "uptime_seconds": uptime_seconds
        if isinstance(uptime_seconds, int) and not isinstance(uptime_seconds, bool)
        else None,
    }


def bridge_pairing_status(timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Return only the public state of the current QR pairing window."""
    pairing_url = f"{settings.whatsapp_api_base_url.rstrip('/')}/pairing"
    unavailable = {
        "reachable": False,
        "state": "unavailable",
        "needs_pairing": False,
        "has_qr": False,
    }
    try:
        response = requests.get(pairing_url, timeout=timeout_seconds)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {**unavailable, "error": type(exc).__name__}
    if not isinstance(payload, dict):
        return {**unavailable, "error": "InvalidPairingPayload"}
    allowed_states = {
        "starting",
        "connected",
        "waiting_for_scan",
        "refreshing",
        "paired",
        "timed_out",
        "failed",
        "not_required",
    }
    state = payload.get("state")
    return {
        "reachable": response.status_code < 500,
        "http_status": response.status_code,
        "state": state
        if isinstance(state, str) and state in allowed_states
        else "unknown",
        "needs_pairing": payload.get("needs_pairing") is True,
        "has_qr": payload.get("has_qr") is True,
        "updated_at": payload.get("updated_at")
        if isinstance(payload.get("updated_at"), str)
        else None,
        "expires_at": payload.get("expires_at")
        if isinstance(payload.get("expires_at"), str)
        else None,
        "generation": payload.get("generation")
        if isinstance(payload.get("generation"), int)
        and not isinstance(payload.get("generation"), bool)
        else None,
    }


def bridge_pairing_qr(timeout_seconds: float = 3.0) -> bytes | None:
    """Fetch an ephemeral QR PNG from the private bridge network."""
    qr_url = f"{settings.whatsapp_api_base_url.rstrip('/')}/pairing/qr"
    try:
        response = requests.get(qr_url, timeout=timeout_seconds)
    except requests.RequestException:
        return None
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if response.status_code != 200 or content_type != "image/png":
        return None
    if not response.content or len(response.content) > 2_000_000:
        return None
    return bytes(response.content)


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


def normalize_recipient(recipient: str) -> str:
    if is_technical_whatsapp_jid(recipient):
        raise ValueError("Technical WhatsApp JIDs cannot be used as recipients")
    jid = normalize_whatsapp_jid(recipient)
    return jid.split("@", 1)[0] if jid.endswith("@s.whatsapp.net") else jid


def whatsapp_test_recipient_allowlist() -> tuple[str, ...]:
    """Return a normalized, deduplicated allowlist without exposing it in status."""
    recipients: set[str] = set()
    for raw_recipient in settings.whatsapp_test_recipients:
        try:
            recipients.add(normalize_recipient(raw_recipient))
        except ValueError:
            continue
    return tuple(sorted(recipients))


def search_contacts(query: str) -> list[dict[str, Any]]:
    return normalize_whatsapp_records(_serialize(wa.search_contacts(query)))


def list_chats(query: str | None = None, limit: int = 20) -> Any:
    return normalize_whatsapp_records(
        _serialize(
            wa.list_chats(query=query, limit=limit, page=0, include_last_message=True)
        )
    )


def list_messages(
    phone: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> Any:
    normalized_phone = normalize_phone(phone) if phone else None
    normalized_jid = normalize_whatsapp_jid(chat_jid) if chat_jid else None
    if normalized_jid and is_technical_whatsapp_jid(normalized_jid):
        raise ValueError("Technical WhatsApp chats are excluded")

    bounded_limit = max(1, min(int(limit), 100))
    database_path = Path(str(wa.MESSAGES_DB_PATH)).resolve()
    if not database_path.is_file():
        return []

    query_parts = [
        """
        SELECT
            m.timestamp,
            m.sender,
            c.name AS chat_name,
            m.content,
            m.is_from_me,
            c.jid AS chat_jid,
            m.id,
            m.media_type
        FROM messages AS m
        JOIN chats AS c ON m.chat_jid = c.jid
        """
    ]
    where_clauses: list[str] = []
    params: list[Any] = []

    if normalized_phone:
        where_clauses.append("(m.sender = ? OR m.sender = ? OR m.sender LIKE ?)")
        params.extend(
            [
                normalized_phone,
                f"{normalized_phone}@s.whatsapp.net",
                f"{normalized_phone}:%@s.whatsapp.net",
            ]
        )
    if normalized_jid:
        if normalized_jid.endswith("@s.whatsapp.net"):
            local = normalized_jid.split("@", 1)[0]
            where_clauses.append("(m.chat_jid = ? OR m.chat_jid LIKE ?)")
            params.extend([normalized_jid, f"{local}:%@s.whatsapp.net"])
        else:
            where_clauses.append("m.chat_jid = ?")
            params.append(normalized_jid)
    clean_query = " ".join(str(query or "").split())
    if clean_query:
        where_clauses.append("LOWER(COALESCE(m.content, '')) LIKE LOWER(?)")
        params.append(f"%{clean_query[:200]}%")
    if where_clauses:
        query_parts.append("WHERE " + " AND ".join(where_clauses))
    query_parts.append("ORDER BY m.timestamp DESC LIMIT ?")
    params.append(bounded_limit)

    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        rows = connection.execute(" ".join(query_parts), params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if "connection" in locals():
            connection.close()

    records: list[dict[str, Any]] = []
    for row in rows:
        raw_jid = str(row["chat_jid"] or "")
        if is_technical_whatsapp_jid(raw_jid):
            continue
        records.append(
            {
                "timestamp": str(row["timestamp"] or ""),
                "sender": str(row["sender"] or ""),
                "chat_name": row["chat_name"],
                "content": str(row["content"] or ""),
                "is_from_me": bool(row["is_from_me"]),
                "chat_jid": normalize_whatsapp_jid(raw_jid),
                "id": str(row["id"] or ""),
                "media_type": row["media_type"],
            }
        )
    return records


def get_latest_unanswered_inbound_message(
    jid: str,
    *,
    scan_limit: int = 20,
) -> dict[str, Any] | None:
    """Return one minimal inbound message only when it is the latest chat event."""
    normalized_jid = normalize_whatsapp_jid(jid)
    if is_technical_whatsapp_jid(normalized_jid):
        raise ValueError("Technical WhatsApp chats are excluded")

    records = list_messages(
        chat_jid=normalized_jid,
        limit=max(1, min(int(scan_limit), 50)),
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        content = " ".join(str(record.get("content") or "").split())
        if not content:
            continue
        if bool(record.get("is_from_me")):
            return None
        return {
            "id": str(record.get("id") or ""),
            "timestamp": str(record.get("timestamp") or ""),
            "chat_jid": normalized_jid,
            "content": content,
            "media_type": record.get("media_type"),
        }
    return None


def get_last_interaction(jid: str) -> Any:
    normalized = normalize_whatsapp_jid(jid)
    if is_technical_whatsapp_jid(normalized):
        raise ValueError("Technical WhatsApp chats are excluded")
    return _serialize(wa.get_last_interaction(normalized))


def send_message(recipient: str, message: str) -> dict[str, Any]:
    normalized = normalize_recipient(recipient)
    test_recipient = normalized in whatsapp_test_recipient_allowlist()
    if not settings.allow_whatsapp_send and not test_recipient:
        return {
            "success": False,
            "blocked": True,
            "message": (
                "WhatsApp sending is disabled for this recipient. Global sending remains "
                "off and the recipient is not in OLLUM_WHATSAPP_TEST_RECIPIENTS."
            ),
        }

    success, status = wa.send_message(normalized, message)
    return {
        "success": bool(success),
        "message": status,
        "recipient": normalized,
        "send_policy": "global" if settings.allow_whatsapp_send else "test_recipient",
    }
