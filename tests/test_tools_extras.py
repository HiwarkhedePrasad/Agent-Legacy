"""Tests for the extra tools (calculate, get_datetime, call_api, fetch_pdf)
and the SSRF-safe fetch plumbing. No API calls, no real network."""

import datetime

import pytest

from agent.tools import crawl
from agent.tools.extras import call_api, calculate, fetch_pdf, get_datetime
from agent.tools.registry import build_all_tools


# --- calculate ----------------------------------------------------------------
def test_calculate_basics():
    assert calculate.invoke({"expression": "(128+256)/3"}) == "128.0"
    assert calculate.invoke({"expression": "2**10"}) == "1024"
    assert calculate.invoke({"expression": "7 // 2"}) == "3"
    assert calculate.invoke({"expression": "-5 + 2"}) == "-3"


def test_calculate_functions():
    assert calculate.invoke({"expression": "sqrt(16)"}) == "4.0"
    assert calculate.invoke({"expression": "max(3, 9, 4)"}) == "9"
    assert float(calculate.invoke({"expression": "pi"})) == pytest.approx(3.14159, rel=1e-4)


def test_calculate_rejects_dangerous_input():
    for evil in ("__import__('os').system('dir')", "open('x')", "os.name",
                 "[x for x in (1,)]", "1/0", "(9**9)**9"):
        result = calculate.invoke({"expression": evil})
        assert result.startswith("Cannot evaluate"), (evil, result)


# --- get_datetime ---------------------------------------------------------------
def test_get_datetime_contains_current_year():
    out = get_datetime.invoke({})
    assert str(datetime.datetime.now().year) in out
    assert "ISO:" in out


# --- call_api / fetch_pdf (network mocked) ---------------------------------------
def test_call_api_pretty_prints_json(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.extras._fetch_bytes",
        lambda url: b'{"b": 1, "a": [1, 2]}',
    )
    out = call_api.invoke({"url": "https://example.com/api"})
    import json as _json

    assert _json.loads(out) == {"b": 1, "a": [1, 2]}
    assert "\n  " in out  # pretty-printed with indentation


def test_call_api_returns_raw_text_for_non_json(monkeypatch):
    monkeypatch.setattr("agent.tools.extras._fetch_bytes", lambda url: b"plain text body")
    assert call_api.invoke({"url": "https://example.com/txt"}) == "plain text body"


def test_call_api_fetch_failure(monkeypatch):
    monkeypatch.setattr("agent.tools.extras._fetch_bytes", lambda url: None)
    assert "Failed to call" in call_api.invoke({"url": "https://example.com/x"})


def test_fetch_pdf_rejects_non_pdf(monkeypatch):
    monkeypatch.setattr("agent.tools.extras._fetch_bytes", lambda url: b"<html>hi</html>")
    assert "not a PDF" in fetch_pdf.invoke({"url": "https://example.com/page"})


def test_fetch_pdf_fetch_failure(monkeypatch):
    monkeypatch.setattr("agent.tools.extras._fetch_bytes", lambda url: None)
    assert "Failed to fetch" in fetch_pdf.invoke({"url": "https://example.com/x.pdf"})


# --- SSRF-safe fetch plumbing (new byte-based signature) -------------------------
def test_ssrf_redirect_to_internal_refused(monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        if url == "https://example.com/start":
            return ("redirect", "http://127.0.0.1/secret")
        return ("ok", b"<html>body</html>")

    monkeypatch.setattr(crawl, "_fetch_one", fake)
    assert crawl._fetch_bytes("https://example.com/start") is None
    assert calls == ["https://example.com/start"]


def test_ssrf_public_redirect_chain_works(monkeypatch):
    def fake(url):
        if url == "https://example.com/start":
            return ("redirect", "https://example.com/page2")
        return ("ok", b"<html><p>hello</p></html>")

    monkeypatch.setattr(crawl, "_fetch_one", fake)
    html = crawl._fetch_html("https://example.com/start")
    assert html is not None and "hello" in html


def test_ssrf_redirect_loop_terminates(monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        return ("redirect", "https://example.com/next")

    monkeypatch.setattr(crawl, "_fetch_one", fake)
    assert crawl._fetch_bytes("https://example.com/next") is None
    assert len(calls) == crawl.MAX_REDIRECTS + 1


# --- wiring ----------------------------------------------------------------------
def test_registry_has_all_eleven_tools():
    tools = build_all_tools("tool-test")
    names = {t.name for t in tools}
    assert names == {
        "fetch_url", "extract_links", "crawl_website", "web_search",
        "fetch_pdf", "call_api", "calculate", "get_datetime",
        "recall_memory", "remember", "route_to_strong_llm",
    }


def test_research_subagent_gets_new_tools():
    from agent.core.agent_factory import _WEB_TOOLS

    for name in ("fetch_pdf", "call_api", "calculate", "get_datetime"):
        assert name in _WEB_TOOLS
