from __future__ import annotations

from typing import Any

from .company_search import candidate_rejection_reason

COMMERCIAL_MARKERS = (
    "заказать",
    "каталог",
    "контакты",
    "наша компания",
    "наши услуги",
    "о компании",
    "оставить заявку",
    "получить консультацию",
    "прайс",
    "продукция",
    "рассчитать стоимость",
    "стоимость услуг",
    "цены",
)
EDITORIAL_MARKERS = (
    "автор статьи",
    "блог",
    "виды и",
    "новости",
    "определение",
    "редакция",
    "статья",
    "что такое",
)


def _marker_count(text: str, markers: tuple[str, ...]) -> int:
    return sum(marker in text for marker in markers)


def assess_company_candidate(
    result: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Validate that a fetched result looks like a commercial company website."""
    inspected_result = {
        **result,
        "source_url": snapshot.get("final_url") or result.get("source_url"),
        "company_name": snapshot.get("title") or result.get("company_name"),
        "snippet": snapshot.get("meta_description") or result.get("snippet"),
    }
    search_reason = candidate_rejection_reason(inspected_result)
    if search_reason:
        return {"accepted": False, "reason": search_reason, "signals": {}}

    headings = " ".join(
        str(item.get("text") or "")
        for item in (snapshot.get("headings") or [])
        if isinstance(item, dict)
    )
    visible_text = str(snapshot.get("visible_text") or "")
    text = " ".join(
        (
            str(snapshot.get("title") or ""),
            str(snapshot.get("meta_description") or ""),
            headings,
            visible_text,
        )
    ).lower()
    contacts = snapshot.get("contacts") or {}
    contact_count = (
        sum(
            len(value)
            for key, value in contacts.items()
            if key in {"emails", "phones"} and isinstance(value, list)
        )
        if isinstance(contacts, dict)
        else 0
    )
    forms = snapshot.get("forms") or {}
    form_count = int(forms.get("count") or 0) if isinstance(forms, dict) else 0
    commercial_count = _marker_count(text, COMMERCIAL_MARKERS)
    editorial_count = _marker_count(text, EDITORIAL_MARKERS)
    signals = {
        "commercial_markers": commercial_count,
        "editorial_markers": editorial_count,
        "contacts": contact_count,
        "forms": form_count,
        "visible_text_chars": len(visible_text),
    }

    if editorial_count >= 2 and commercial_count < 2 and not form_count:
        return {"accepted": False, "reason": "editorial_site", "signals": signals}
    if len(visible_text) < 120 and not contact_count and not form_count:
        return {"accepted": False, "reason": "thin_site", "signals": signals}
    if not contact_count and not form_count and commercial_count < 2:
        return {
            "accepted": False,
            "reason": "no_commercial_contact_or_action",
            "signals": signals,
        }
    return {"accepted": True, "reason": None, "signals": signals}
