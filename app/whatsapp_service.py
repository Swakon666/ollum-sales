from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .config import settings


REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_WHATSAPP_SERVER = REPO_ROOT / "upstream" / "whatsapp-mcp" / "whatsapp-mcp-server"
UPSTREAM_WHATSAPP_MODULE = UPSTREAM_WHATSAPP_SERVER / "whatsapp.py"


def _load_upstream_whatsapp():
    """Load the original upstream whatsapp.py without modifying its source tree."""
    if not UPSTREAM_WHATSAPP_MODULE.exists():
        raise RuntimeError(f"Upstream WhatsApp module not found: {UPSTREAM_WHATSAPP_MODULE}")

    # Upstream whatsapp.py imports audio.py as a top-level module.
    server_path = str(UPSTREAM_WHATSAPP_SERVER)
    if server_path not in sys.path:
        sys.path.insert(0, server_path)

    spec = importlib.util.spec_from_file_location("ollum_upstream_whatsapp", UPSTREAM_WHATSAPP_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not create import spec for upstream WhatsApp module")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Keep upstream files pristine; adapt runtime paths here instead.
    module.MESSAGES_DB_PATH = settings.whatsapp_db_path
    module.WHATSAPP_API_BASE_URL = settings.whatsapp_api_base_url
    return module


wa = _load_upstream_whatsapp()


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
    recipient = recipient.strip()
    if "@" in recipient:
        return recipient
    digits = re.sub(r"\D", "", recipient)
    if not digits:
        raise ValueError("Recipient must contain a phone number or a WhatsApp JID")
    return digits


def search_contacts(query: str) -> list[dict[str, Any]]:
    return _serialize(wa.search_contacts(query))


def list_chats(query: str | None = None, limit: int = 20) -> Any:
    return _serialize(wa.list_chats(query=query, limit=limit, page=0, include_last_message=True))


def list_messages(
    phone: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> Any:
    return _serialize(
        wa.list_messages(
            sender_phone_number=phone,
            chat_jid=chat_jid,
            query=query,
            limit=limit,
            page=0,
            include_context=True,
            context_before=2,
            context_after=2,
        )
    )


def get_last_interaction(jid: str) -> Any:
    return _serialize(wa.get_last_interaction(jid))


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
