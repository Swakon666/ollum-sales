from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

LEGAL_FORM_PATTERN = re.compile(
    r"\b(?:ооо|оао|ао|пао|зао|ип|нко|llc|ltd|limited|inc|corp|corporation)\b",
    re.IGNORECASE,
)
NON_WORD_PATTERN = re.compile(r"[^\w]+", re.UNICODE)
TECHNICAL_JIDS = {
    "0@s.whatsapp.net",
    "status@broadcast",
    "status@s.whatsapp.net",
}


def company_domain_key(value: str) -> str:
    """Return a scheme-independent host key suitable for company deduplication."""
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("website_url must be an absolute HTTP(S) URL")
    host = parsed.hostname.rstrip(".").lower().removeprefix("www.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("website_url contains an invalid hostname") from exc
    port = parsed.port
    if port and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        return f"{host}:{port}"
    return host


def company_name_key(value: str) -> str:
    """Normalize exact company names while removing common legal-form noise."""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = LEGAL_FORM_PATTERN.sub(" ", normalized)
    normalized = NON_WORD_PATTERN.sub(" ", normalized)
    return " ".join(normalized.split())


def location_key(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(NON_WORD_PATTERN.sub(" ", normalized).split())


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    digits = digits.removeprefix("00")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) < 7 or len(digits) > 15 or set(digits) == {"0"}:
        return None
    return digits


def phone_keys(values: Iterable[Any]) -> set[str]:
    return {
        normalized
        for value in values
        if (normalized := normalize_phone(str(value or ""))) is not None
    }


def candidate_phones(candidate: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    direct = candidate.get("phones")
    if isinstance(direct, list):
        values.extend(direct)
    elif direct:
        values.append(direct)
    if candidate.get("phone"):
        values.append(candidate["phone"])
    contacts = candidate.get("contacts")
    if isinstance(contacts, dict):
        nested = contacts.get("phones")
        if isinstance(nested, list):
            values.extend(nested)
        elif nested:
            values.append(nested)
    return sorted(phone_keys(values))


def normalize_whatsapp_jid(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raise ValueError("WhatsApp JID must not be empty")
    if "@" not in raw:
        phone = normalize_phone(raw)
        if not phone:
            raise ValueError("WhatsApp recipient must contain a valid phone or JID")
        return f"{phone}@s.whatsapp.net"
    local, server = raw.rsplit("@", 1)
    if server == "s.whatsapp.net":
        local = local.split(":", 1)[0]
        phone = normalize_phone(local)
        if not phone:
            raise ValueError("WhatsApp user JID must contain a valid phone")
        return f"{phone}@s.whatsapp.net"
    if not local or not server:
        raise ValueError("WhatsApp JID is invalid")
    return f"{local}@{server}"


def is_technical_whatsapp_jid(value: str) -> bool:
    try:
        jid = normalize_whatsapp_jid(value)
    except ValueError:
        return True
    return (
        jid in TECHNICAL_JIDS
        or jid.endswith(("@newsletter", "@broadcast"))
        or jid.startswith("0@")
    )


def normalize_whatsapp_records(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        raw_jid = str(item.get("jid") or item.get("chat_jid") or "").strip()
        if not raw_jid or is_technical_whatsapp_jid(raw_jid):
            continue
        clean = dict(item)
        jid = normalize_whatsapp_jid(raw_jid)
        if "jid" in clean:
            clean["jid"] = jid
        if "chat_jid" in clean:
            clean["chat_jid"] = jid
        if jid.endswith("@s.whatsapp.net"):
            clean["phone_number"] = jid.split("@", 1)[0]
        normalized.append(clean)
    return normalized


def normalize_contacts(value: Any) -> dict[str, list[str]]:
    contacts = value if isinstance(value, dict) else {}
    phones = sorted(phone_keys(contacts.get("phones") or []))
    emails = sorted(
        {
            str(item).strip().casefold()
            for item in contacts.get("emails") or []
            if "@" in str(item) and str(item).strip()
        }
    )
    messengers: set[str] = set()
    for item in contacts.get("messengers") or []:
        raw = str(item or "").strip()
        if not raw:
            continue
        if "://" in raw:
            messengers.add(raw)
            continue
        try:
            jid = normalize_whatsapp_jid(raw)
        except ValueError:
            continue
        if not is_technical_whatsapp_jid(jid):
            messengers.add(jid)
    social_links = sorted(
        {
            str(item).strip()
            for item in contacts.get("social_links") or []
            if str(item).strip()
        }
    )
    return {
        "phones": phones,
        "emails": emails,
        "messengers": sorted(messengers),
        "social_links": social_links,
    }


def retry_call[T](
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.25,
    max_delay_seconds: float = 2.0,
    retry_if: Callable[[Exception], bool] | None = None,
    on_retry: Callable[[int, Exception], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry a bounded idempotent operation with exponential backoff."""
    total_attempts = max(1, min(int(attempts), 5))
    delay = max(0.0, float(base_delay_seconds))
    for attempt in range(1, total_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            retryable = (
                retry_if(exc)
                if retry_if is not None
                else not isinstance(exc, (KeyError, TypeError, ValueError))
            )
            if not retryable or attempt >= total_attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            sleep(min(delay, max(0.0, float(max_delay_seconds))))
            delay = delay * 2 if delay else 0
    raise RuntimeError("retry loop exhausted")
