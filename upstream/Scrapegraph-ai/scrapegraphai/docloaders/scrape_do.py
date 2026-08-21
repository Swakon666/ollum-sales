"""
Scrape_do module
"""

import ipaddress
import os
import socket
from urllib.parse import quote, urlsplit, urlunsplit

import requests


def _public_http_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Target URL must use http or https and include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("Target URL must not contain credentials")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "metadata.google.internal"}:
        raise ValueError("Target URL hostname is not public")

    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {
            ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
            for _, _, _, _, sockaddr in socket.getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM
            )
        }
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Target URL resolves to a non-public address")
    return urlunsplit(parsed)


def _service_endpoint(env_name, default, default_scheme):
    value = os.getenv(env_name, default).strip()
    if "://" not in value:
        value = f"{default_scheme}://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{env_name} must be an HTTP(S) endpoint")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError(f"{env_name} must contain only a scheme, host, and port")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{env_name} must not contain a query or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def scrape_do_fetch(
    token, target_url, use_proxy=False, geoCode=None, super_proxy=False
):
    """
    Fetches the IP address of the machine associated with the given URL using Scrape.do.

    Args:
        token (str): The API token for Scrape.do service.
        target_url (str): A valid web page URL to fetch its associated IP address.
        use_proxy (bool): Whether to use Scrape.do proxy mode. Default is False.
        geoCode (str, optional): Specify the country code for
        geolocation-based proxies. Default is None.
        super_proxy (bool): If True, use Residential & Mobile Proxy Networks. Default is False.

    Returns:
        str: The raw response from the target URL.
    """
    target_url = _public_http_url(target_url)
    params = {"geoCode": geoCode, "super": str(super_proxy).lower()} if geoCode else {}
    if use_proxy:
        proxy_endpoint = _service_endpoint(
            "PROXY_SCRAPE_DO_URL", "proxy.scrape.do:8080", "http"
        )
        parsed_proxy = urlsplit(proxy_endpoint)
        proxy_mode_url = urlunsplit(
            (
                parsed_proxy.scheme,
                f"{quote(token, safe='')}:@{parsed_proxy.netloc}",
                "",
                "",
                "",
            )
        )
        proxies = {
            "http": proxy_mode_url,
            "https": proxy_mode_url,
        }
        response = requests.get(  # lgtm[py/full-ssrf]
            target_url,
            proxies=proxies,
            verify=True,
            params=params,
            timeout=60,
            allow_redirects=False,
        )
    else:
        api_endpoint = _service_endpoint("API_SCRAPE_DO_URL", "api.scrape.do", "https")
        params.update({"token": token, "url": target_url})
        response = requests.get(
            api_endpoint,
            params=params,
            timeout=60,
            allow_redirects=False,
        )

    return response.text
