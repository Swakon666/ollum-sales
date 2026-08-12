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


def _is_blocked_domain(hostname: str) -> bool:
    hostname = hostname.lower().removeprefix("www.")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in BLOCKED_RESULT_DOMAINS
    )


def build_company_query(
    industry: str, location: str, extra_query: str | None = None
) -> str:
    parts = [industry.strip(), location.strip(), "official company website"]
    if extra_query:
        parts.append(extra_query.strip())
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
        seen.add(website_url)
        snippet_node = item.select_one(".b_caption p")
        title = link.get_text(" ", strip=True)
        if not title:
            title = parsed.hostname.removeprefix("www.")
        results.append(
            {
                "company_name": title[:300],
                "website_url": website_url,
                "source_url": href,
                "snippet": snippet_node.get_text(" ", strip=True)[:1000]
                if snippet_node
                else None,
            }
        )
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
            "company_name": str(item.get("title") or parsed.hostname).strip()[:300],
            "website_url": website_url,
            "source_url": href,
            "snippet": str(item.get("snippet") or "").strip()[:1000] or None,
        }
        if website_url in seen or not _is_relevant_result(result, tokens):
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
