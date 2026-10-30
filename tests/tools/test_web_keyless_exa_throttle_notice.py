"""Exa's free-tier throttle arrives as an ordinary HTTP 200 text item (issue #146853).

The envelope carries neither a JSON-RPC ``error`` nor ``result.isError``, so ``_parse_mcp_body``
returns the notice as regular tool content. Left as content it reads as an *empty* result page
(``web_search`` answers ``success: true`` with zero hits, so the keyless ring never fails over) or
as the requested URL's body (``web_extract`` serves the notice as page content). These tests drive
the real searcher/extractor and ring functions against that envelope.
"""

import json

import pytest

from plugins.web import keyless_mcp

# Verbatim payload text from the report: HTTP 200, ``text/event-stream``, no error, no isError.
EXA_THROTTLE_NOTICE = (
    "You've hit Exa's free MCP rate limit. To continue using without limits, create your own "
    "Exa API key.\n\nFix: Create API key at https://dashboard.exa.ai/api-keys , and then update "
    "Exa MCP URL to this https://mcp.exa.ai/mcp?exaApiKey=YOUR_EXA_API_KEY"
)

PARALLEL_HIT = {
    "url": "https://parallel.example/hit", "title": "Real hit", "description": "a snippet", "position": 1,
}
PARALLEL_PAGE = {
    "url": "https://example.com/page",
    "title": "Real page",
    "content": "the real page body",
    "raw_content": "the real page body",
    "metadata": {"sourceURL": "https://example.com/page", "title": "Real page"},
}


def _sse_response(text):
    """A 200 ``text/event-stream`` envelope carrying *text* as its only content item."""
    import requests

    payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "text/event-stream"
    response.encoding = "utf-8"
    response._content = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
    return response


@pytest.fixture()
def exa_answers(monkeypatch):
    """Return a setter that makes every Exa MCP POST answer *text* with the masked envelope."""
    import requests

    def _install(text):
        monkeypatch.setattr(requests, "post", lambda *a, **k: _sse_response(text))

    return _install


@pytest.fixture()
def exa_pinned(monkeypatch):
    """Deterministic ring: Exa first, no vendor pinned to a paid tier."""
    monkeypatch.setattr(keyless_mcp, "_vendor_pinned", lambda name: name == "exa")
    monkeypatch.setattr(keyless_mcp, "provider_tier", lambda name: "auto")


class TestMaskedThrottleIsAnError:
    def test_exa_search_surfaces_the_notice_instead_of_zero_results(self, exa_answers):
        exa_answers(EXA_THROTTLE_NOTICE)
        out = keyless_mcp.exa_search_keyless("q")
        assert out["success"] is False, out
        assert "free MCP rate limit" in out["error"], out
        assert "EXA_API_KEY" in out["error"], out

    def test_exa_extract_never_serves_the_notice_as_page_content(self, exa_answers):
        exa_answers(EXA_THROTTLE_NOTICE)
        out = keyless_mcp.exa_extract_keyless(["https://example.com/page"])
        assert out[0]["error"], out
        assert not out[0]["content"], out
        assert "free MCP rate limit" in out[0]["error"], out


class TestRingFailsOverPastTheNotice:
    def test_search_ring_advances_to_the_next_vendor(self, exa_pinned, exa_answers, monkeypatch):
        exa_answers(EXA_THROTTLE_NOTICE)
        healthy = {**keyless_mcp._KEYLESS_SEARCHERS, "parallel": lambda q, l: {"success": True, "data": {"web": [dict(PARALLEL_HIT)]}}}
        monkeypatch.setattr(keyless_mcp, "_KEYLESS_SEARCHERS", healthy)
        out = keyless_mcp.search_with_failover("exa", "q")
        assert out["success"] is True, out
        assert out["data"]["web"] == [PARALLEL_HIT], out
        assert out["data"]["served_by"] == "parallel", out

    def test_extract_ring_advances_to_the_next_vendor(self, exa_pinned, exa_answers, monkeypatch):
        exa_answers(EXA_THROTTLE_NOTICE)
        healthy = {**keyless_mcp._KEYLESS_EXTRACTORS, "parallel": lambda urls: [dict(PARALLEL_PAGE) for _ in urls]}
        monkeypatch.setattr(keyless_mcp, "_KEYLESS_EXTRACTORS", healthy)
        out = keyless_mcp.extract_with_failover("exa", ["https://example.com/page"])
        assert out[0]["content"] == "the real page body", out
        assert "error" not in out[0], out


class TestNoFalsePositives:
    def test_a_real_search_block_is_untouched(self, monkeypatch):
        text = "Title: First\nURL: https://a.example\nHighlights:\nbody text"
        monkeypatch.setattr(keyless_mcp, "mcp_call", lambda *a, **k: text)
        out = keyless_mcp.exa_search_keyless("q")
        assert out["success"] is True, out
        assert out["data"]["web"][0]["url"] == "https://a.example", out

    def test_a_real_page_that_mentions_the_limit_is_still_content(self, monkeypatch):
        page = "# Exa free tier\n\n" + ("This page documents the free MCP rate limit. " * 40)
        monkeypatch.setattr(keyless_mcp, "mcp_call", lambda *a, **k: page)
        out = keyless_mcp.exa_extract_keyless(["https://example.com/doc"])
        assert out[0]["content"] == page, out

    def test_another_vendor_wording_is_not_an_exa_notice(self):
        predicate = getattr(keyless_mcp, "_exa_throttle_notice", None)
        if predicate is None:
            pytest.skip("masked-throttle detection not present")
            return
        assert predicate(EXA_THROTTLE_NOTICE) is True
        assert predicate("You've hit Parallel's free MCP rate limit.") is False
        assert predicate("Title: A\nURL: https://a.example") is False
