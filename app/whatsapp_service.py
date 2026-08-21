from __future__ import annotations

import importlib.util
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
    records = _serialize(
        wa.list_messages(
            sender_phone_number=normalized_phone,
            chat_jid=normalized_jid,
            query=query,
            limit=limit,
            page=0,
            include_context=True,
            context_before=2,
            context_after=2,
        )
    )
    if not isinstance(records, list):
        return []
    return [
        item
        for item in records
        if not isinstance(item, dict)
        or not item.get("chat_jid")
        or not is_technical_whatsapp_jid(str(item["chat_jid"]))
    ]


def get_last_interaction(jid: str) -> Any:
    normalized = normalize_whatsapp_jid(jid)
    if is_technical_whatsapp_jid(normalized):
        raise ValueError("Technical WhatsApp chats are excluded")
    return _serialize(wa.get_last_interaction(normalized))


def send_message(recipient: str, message: str) -> dict[str, Any]:
    if not settings.allow_whatsapp_send:
        return {
            "success": False,
            "blocked": True,
            "message": (
                "WhatsApp sending is disabled. Set OLLUM_ALLOW_WHATSAPP_SEND=true "
                "only after you have verified the recipient and message."
            ),
        }

    normalized = normalize_recipient(recipient)
    success, status = wa.send_message(normalized, message)
    return {"success": bool(success), "message": status, "recipient": normalized}
