from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .security import validate_public_http_url

MAX_HTML_BYTES = 2_000_000
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-])")
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"
)
TECH_MARKERS = {
    "1C-Bitrix": ("bitrix", "bx-"),
    "WordPress": ("wp-content", "wp-includes"),
    "Tilda": ("tilda", "t-records"),
    "Wix": ("wixstatic.com", "wix.com"),
    "Next.js": ("__next_data__", "/_next/"),
    "React": ("data-reactroot", "react-dom"),
    "Vue": ("data-v-", "__vue__"),
    "Google Analytics": ("googletagmanager.com", "google-analytics.com", "gtag("),
    "Yandex Metrica": ("mc.yandex.ru", "ym("),
    "JivoSite": ("jivosite", "jivochat"),
    "amoCRM": ("amocrm",),
}


def _download_html(url: str, *, timeout: int) -> tuple[str, str, str]:
    current = validate_public_http_url(url)
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }
    for _ in range(6):
        response = session.get(
            current,
            headers=headers,
            timeout=max(5, min(int(timeout), 60)),
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("website returned a redirect without a location")
            current = validate_public_http_url(urljoin(current, location))
            continue
        response.raise_for_status()
        content_type = (
            response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type and content_type not in {
            "text/html",
            "application/xhtml+xml",
            "text/plain",
        }:
            response.close()
            raise ValueError(
                f"website content type is not inspectable HTML/text: {content_type}"
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_HTML_BYTES:
                response.close()
                raise ValueError("website HTML exceeds the 2 MB inspection limit")
            chunks.append(chunk)
        encoding = response.encoding or response.apparent_encoding or "utf-8"
        response.close()
        return (
            current,
            b"".join(chunks).decode(encoding, errors="replace"),
            content_type,
        )
    raise ValueError("website redirected too many times")


def inspect_website(
    url: str, *, max_text_chars: int = 20_000, timeout: int = 20
) -> dict[str, Any]:
    """Fetch bounded public HTML and return factual evidence for Codex-side analysis."""
    final_url, html, content_type = _download_html(url, timeout=timeout)
    soup = BeautifulSoup(html, "html.parser")

    for node in soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else None
    description_node = soup.select_one('meta[name="description" i]')
    description = (
        str(description_node.get("content") or "").strip() if description_node else None
    )
    viewport = soup.select_one('meta[name="viewport" i]') is not None
    language = str(soup.html.get("lang") or "").strip() if soup.html else None

    headings = [
        {"level": node.name, "text": node.get_text(" ", strip=True)[:500]}
        for node in soup.select("h1, h2, h3")
        if node.get_text(" ", strip=True)
    ][:100]
    text = "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )
    text = text[: max(1000, min(int(max_text_chars), 50_000))]

    emails = sorted(set(EMAIL_PATTERN.findall(text)))[:30]
    phones = sorted(set(PHONE_PATTERN.findall(text)))[:30]
    external_links: list[str] = []
    social_links: list[str] = []
    final_host = (urlsplit(final_url).hostname or "").removeprefix("www.")
    for link in soup.select("a[href]"):
        href = urljoin(final_url, str(link.get("href") or "").strip())
        parsed = urlsplit(href)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        host = parsed.hostname.removeprefix("www.")
        if host != final_host and not host.endswith(f".{final_host}"):
            external_links.append(href)
            if any(
                domain in host
                for domain in (
                    "vk.com",
                    "t.me",
                    "instagram.com",
                    "youtube.com",
                    "rutube.ru",
                    "dzen.ru",
                )
            ):
                social_links.append(href)

    html_lower = html.lower()
    technologies = sorted(
        name
        for name, markers in TECH_MARKERS.items()
        if any(marker in html_lower for marker in markers)
    )
    forms = soup.select("form")
    form_inputs = sorted(
        {
            str(node.get("type") or node.name).lower()
            for form in forms
            for node in form.select("input, textarea, select, button")
        }
    )

    return {
        "requested_url": url,
        "final_url": final_url,
        "content_type": content_type,
        "title": title,
        "meta_description": description,
        "language": language,
        "mobile_viewport": viewport,
        "headings": headings,
        "visible_text": text,
        "contacts": {
            "emails": emails,
            "phones": phones,
            "social_links": sorted(set(social_links))[:30],
        },
        "forms": {"count": len(forms), "input_types": form_inputs},
        "technologies": technologies,
        "external_links": sorted(set(external_links))[:100],
        "evidence_limits": {
            "html_bytes": len(html.encode("utf-8", errors="ignore")),
            "visible_text_chars": len(text),
        },
    }
