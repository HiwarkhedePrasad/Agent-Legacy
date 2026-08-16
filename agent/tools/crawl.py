"""Deep-crawl tools: fetch pages, extract links, and crawl websites.

These give the agent the capability to actually crawl and read websites
(the 'deep' part of the deep agent).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from markdownify import markdownify as html_to_markdown

from agent.config import settings
from agent.tools.web_search import USER_AGENT

MAX_PAGE_CHARS = 8000

# SSRF hardening: never buffer more than ~2 MB of page data, and never follow
# more than this many redirect hops (each hop is re-validated).
MAX_FETCH_BYTES = 2_000_000
MAX_REDIRECTS = 5


def _is_safe_url(url: str) -> bool:
    """Block SSRF: reject non-http(s) schemes, localhost and private/reserved
    IP ranges after DNS resolution."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        host = parsed.hostname or ""
        if host in ("localhost", "metadata.google.internal"):
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _fetch_one(url: str) -> tuple[str, str | None]:
    """GET one URL WITHOUT following redirects, retrying once on transient
    errors. Returns:
        ("ok", body)          - page fetched
        ("redirect", target)  - a 3xx with a Location to follow
        ("error", None)       - gave up
    Bodies are capped at MAX_FETCH_BYTES so a huge page cannot exhaust memory.
    """
    for attempt in range(2):
        try:
            with httpx.Client(timeout=25.0, follow_redirects=False) as client:
                with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return ("error", None)
                        return ("redirect", urljoin(str(resp.url), location))
                    resp.raise_for_status()
                    length = resp.headers.get("content-length")
                    if length and int(length) > MAX_FETCH_BYTES:
                        return ("error", None)
                    chunks: list[str] = []
                    received = 0
                    for chunk in resp.iter_text():
                        chunks.append(chunk)
                        received += len(chunk)
                        if received > MAX_FETCH_BYTES:
                            return ("error", None)
                    return ("ok", "".join(chunks))
        except Exception:  # noqa: BLE001
            if attempt == 0:
                time.sleep(0.5)
    return ("error", None)


def _fetch_html(url: str) -> str | None:
    """Fetch a page, following redirects MANUALLY so every hop is validated
    against the SSRF blocklist - a redirect to an internal/private address is
    refused instead of silently followed."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        if not _is_safe_url(current):
            return None
        kind, value = _fetch_one(current)
        if kind == "ok":
            return value
        if kind == "redirect":
            current = value
            continue
        return None
    return None


def _html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = html_to_markdown(str(soup), heading_style="ATX")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:MAX_PAGE_CHARS]


def _extract_links(html: str, base_url: str, same_domain_only: bool = True) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    links = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if same_domain_only and parsed.netloc and parsed.netloc != base_domain:
            continue
        links.add(full)
    return sorted(links)


@tool
def fetch_url(url: str) -> str:
    """Fetch a single web page and return its readable content as markdown.
    Use this to read the full text of a page you found via web_search."""
    html = _fetch_html(url)
    if html is None:
        return f"Failed to fetch {url}"
    return _html_to_markdown(html)


@tool
def extract_links(url: str, same_domain_only: bool = True) -> str:
    """Fetch a page and return the links found on it. Use same_domain_only=True
    to stay within one website while crawling."""
    html = _fetch_html(url)
    if html is None:
        return f"Failed to fetch {url}"
    links = _extract_links(html, url, same_domain_only)
    if not links:
        return "No links found."
    return "\n".join(f"- {link}" for link in links[:50])


@dataclass
class CrawlResult:
    pages: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def _crawl(
    start_url: str,
    max_pages: int,
    max_depth: int,
    visited: set[str] | None = None,
    depth: int = 0,
) -> CrawlResult:
    visited = visited or set()
    result = CrawlResult()

    if depth > max_depth or len(visited) >= max_pages:
        return result

    html = _fetch_html(start_url)
    if html is None:
        return result

    visited.add(start_url)
    result.pages[start_url] = _html_to_markdown(html)
    result.order.append(start_url)

    if len(visited) >= max_pages or depth == max_depth:
        return result

    for link in _extract_links(html, start_url, same_domain_only=True):
        if link in visited or len(visited) >= max_pages:
            continue
        sub = _crawl(link, max_pages, max_depth, visited, depth + 1)
        result.pages.update(sub.pages)
        result.order.extend(sub.order)
    return result


def _make_crawl_website_tool(session_id: str):
    """Build crawl_website bound to a session so crawl indexes don't clobber
    each other across concurrent sessions."""
    safe_session = re.sub(r"[^\w\-.]", "_", session_id or "default")

    @tool
    def crawl_website(start_url: str, max_pages: int = 5, max_depth: int = 2) -> str:
        """Crawl a website starting from start_url, following same-domain links up to
        max_depth levels, downloading at most max_pages pages. Returns the combined
        readable content of every crawled page."""
        result = _crawl(start_url, max_pages=max_pages, max_depth=max_depth)
        if not result.pages:
            return f"Crawl of {start_url} produced no pages."
        blocks = []
        for url in result.order:
            content = result.pages[url]
            blocks.append(f"=== PAGE: {url} ===\n{content[:MAX_PAGE_CHARS]}")

        index_path = settings.WORKSPACE_DIR / f"crawled_pages_{safe_session}.json"
        import json

        index_path.write_text(
            json.dumps({"session_urls": result.order}, indent=2), encoding="utf-8"
        )
        return "\n\n".join(blocks)

    crawl_website.__name__ = "crawl_website"
    return crawl_website


def build_crawl_tools(session_id: str = "default") -> list:
    return [fetch_url, extract_links, _make_crawl_website_tool(session_id)]