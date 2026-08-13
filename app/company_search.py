from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup

from .crm import canonical_company_url

SEARCH_URL = "https://www.bing.com/search"
SERPER_URL = "https://google.serper.dev/search"
BLOCKED_RESULT_DOMAINS = {
    "2gis.ru",
    "avito.ru",
    "bing.com",
    "dzen.ru",
    "facebook.com",
    "hh.ru",
    "instagram.com",
    "linkedin.com",
    "maps.google.com",
    "ok.ru",
    "t.me",
    "twitter.com",
    "vk.com",
    "wikipedia.org",
    "x.com",
    "yandex.ru",
    "youtube.com",
}

# Ollum's target check is sized for a 50–300k RUB project. These domains belong
# to federal platforms or very large chains where that offer is not a credible fit.
NON_TARGET_ENTERPRISE_DOMAINS = {
    "beeline.ru",
    "foxford.ru",
    "geekbrains.ru",
    "invitro.ru",
    "lenta.com",
    "magnit.com",
    "mail.ru",
    "medsi.ru",
    "megafon.ru",
    "mts.ru",
    "netology.ru",
    "openedu.ru",
    "ozon.ru",
    "rostelecom.ru",
    "sber.ru",
    "skillbox.ru",
    "skyeng.ru",
    "skysmart.ru",
    "tbank.ru",
    "tele2.ru",
    "tinkoff.ru",
    "wildberries.ru",
    "x5.ru",
}
CONTENT_SUBDOMAINS = ("blog.", "journal.", "media.", "news.")
CONTENT_PATH_MARKERS = (
    "/article",
    "/blog",
    "/journal",
    "/knowledge",
    "/media",
    "/news",
    "/publication",
    "/reviews",
    "/wiki",
)
EDITORIAL_RESULT_MARKERS = (
    "вакансии",
    "виды и",
    "каталог компаний",
    "лучшие компании",
    "определение",
    "работа в",
    "рейтинг",
    "список компаний",
    "топ компаний",
    "что такое",
)


def _is_blocked_domain(hostname: str) -> bool:
    hostname = hostname.lower().removeprefix("www.")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in BLOCKED_RESULT_DOMAINS | NON_TARGET_ENTERPRISE_DOMAINS
    )


def candidate_rejection_reason(result: dict[str, Any]) -> str | None:
    """Return a stable reason when a search hit is clearly not a target company."""
    source_url = str(result.get("source_url") or result.get("website_url") or "")
    parsed = urlsplit(source_url)
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    if not hostname:
        return "invalid_url"
    if _is_blocked_domain(hostname):
        return "blocked_or_enterprise_domain"
    if hostname.startswith(CONTENT_SUBDOMAINS):
        return "content_subdomain"
    path = parsed.path.lower().rstrip("/")
    if any(marker in path for marker in CONTENT_PATH_MARKERS):
        return "editorial_path"
    haystack = " ".join(
        str(result.get(key) or "") for key in ("company_name", "snippet")
    ).lower()
    if any(marker in haystack for marker in EDITORIAL_RESULT_MARKERS):
        return "editorial_result"
    return None


def _clean_company_name(title: str, hostname: str) -> str:
    name = title.strip() or hostname.removeprefix("www.")
    name = re.sub(
        r"\s*(?:[|—–-]\s*)?(?:официальный сайт|главная|home)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip(" |—–-")
    return (name or hostname.removeprefix("www."))[:300]


def build_company_query(
    industry: str, location: str, extra_query: str | None = None
) -> str:
    parts = [
        industry.strip(),
        location.strip(),
        '"официальный сайт"',
        "компания",
        "услуги",
    ]
    # Short vertical hints such as "B2B" help. Long strategy descriptions pollute
    # the query and tend to produce SEO articles that quote the same wording.
    if extra_query and len(extra_query.split()) <= 6:
        parts.append(extra_query.strip())
    parts.extend(("-рейтинг", "-отзывы", "-вакансии", "-статья", "-новости"))
    query = " ".join(part for part in parts if part)
    if not query:
        raise ValueError("industry or a search query is required")
    return query


def _query_tokens(industry: str, location: str) -> set[str]:
    raw = re.findall(r"[\w-]{4,}", f"{industry} {location}".lower())
    ignored = {"company", "companies", "official", "website"}
    return {token for token in raw if token not in ignored}


def _is_relevant_result(result: dict[str, Any], tokens: set[str]) -> bool:
    if not tokens:
        return True
    haystack = f"{result.get('company_name', '')} {result.get('snippet', '')}".lower()
    return any(token in haystack for token in tokens)


def _decode_bing_target(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.hostname or "").lower().removeprefix("www.") != "bing.com":
        return value
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return value
    payload = encoded[2:]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return value
    return decoded if decoded.startswith(("http://", "https://")) else value


def parse_bing_results(html: str, *, limit: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in soup.select("li.b_algo"):
        link = item.select_one("h2 a[href]")
        if link is None:
            continue
        href = _decode_bing_target(str(link.get("href") or "").strip())
        parsed = urlsplit(href)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if _is_blocked_domain(parsed.hostname):
            continue
        try:
            website_url = canonical_company_url(href)
        except ValueError:
            continue
        if website_url in seen:
            continue
        snippet_node = item.select_one(".b_caption p")
        title = link.get_text(" ", strip=True)
        result = {
            "company_name": _clean_company_name(title, parsed.hostname),
            "website_url": website_url,
            "source_url": href,
            "snippet": snippet_node.get_text(" ", strip=True)[:1000]
            if snippet_node
            else None,
        }
        if candidate_rejection_reason(result):
            continue
        seen.add(website_url)
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _serper_results(
    query: str,
    *,
    tokens: set[str],
    limit: int,
    api_key: str,
    timeout: int,
) -> list[dict[str, Any]]:
    response = requests.post(
        SERPER_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={
            "q": query,
            "gl": "ru",
            "hl": "ru",
            "num": min(50, max(10, limit * 2)),
        },
        timeout=max(5, min(int(timeout), 60)),
    )
    response.raise_for_status()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in response.json().get("organic", []):
        href = str(item.get("link") or "").strip()
        parsed = urlsplit(href)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or _is_blocked_domain(parsed.hostname)
        ):
            continue
        try:
            website_url = canonical_company_url(href)
        except ValueError:
            continue
        result = {
            "company_name": _clean_company_name(
                str(item.get("title") or ""), parsed.hostname
            ),
            "website_url": website_url,
            "source_url": href,
            "snippet": str(item.get("snippet") or "").strip()[:1000] or None,
        }
        if (
            website_url in seen
            or candidate_rejection_reason(result)
            or not _is_relevant_result(result, tokens)
        ):
            continue
        seen.add(website_url)
        results.append(result)
        if len(results) >= limit:
            break
    return results


def search_company_websites(
    industry: str,
    location: str,
    *,
    limit: int = 20,
    extra_query: str | None = None,
    serper_api_key: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """Discover likely official company websites from a public search provider."""
    limit = max(1, min(int(limit), 50))
    query = build_company_query(industry, location, extra_query)
    tokens = _query_tokens(industry, location)

    if serper_api_key:
        results = _serper_results(
            query,
            tokens=tokens,
            limit=limit,
            api_key=serper_api_key,
            timeout=timeout,
        )
        return {
            "provider": "serper",
            "query": query,
            "requested": limit,
            "found": len(results),
            "results": results,
            "warning": None
            if results
            else "Serper returned no relevant official websites.",
        }

    params = {"q": query, "count": min(50, max(10, limit * 2)), "setlang": "ru-RU"}
    response = requests.get(
        f"{SEARCH_URL}?{urlencode(params)}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        },
        timeout=max(5, min(int(timeout), 60)),
    )
    response.raise_for_status()
    parsed_results = parse_bing_results(response.text, limit=max(limit * 2, 10))
    results = [
        result for result in parsed_results if _is_relevant_result(result, tokens)
    ][:limit]
    return {
        "provider": "bing_html",
        "query": query,
        "requested": limit,
        "found": len(results),
        "results": results,
        "warning": (
            None
            if results
            else (
                "Bing returned no relevant official websites. Configure SERPER_API_KEY or "
                "import agent-discovered candidates with sales_import_leads."
            )
        ),
    }
