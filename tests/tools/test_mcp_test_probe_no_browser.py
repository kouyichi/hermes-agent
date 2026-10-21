"""The MCP ``test`` probe is a health check, not a login: neither the dashboard endpoint nor the
gateway RPC may start a browser OAuth flow.

The Desktop health sweep (``apps/desktop/src/store/mcp-health.ts``, every 30 min and on gateway
connect) calls these two surfaces to decide whether to nudge the user. When the backend has a
real console on stdin — ``hermes dashboard`` started from a terminal — ``_is_interactive()``
answers from the PROCESS, so a rejected refresh token fell through to the authorization-code
flow and opened a consent tab on every sweep (#142406). Interactive authorization already has
its own explicit entry points (``/api/mcp/servers/{name}/auth``, ``mcp.servers.oauth.start``,
``hermes mcp login``, which passes ``force_interactive_oauth()``).
"""

from __future__ import annotations

import contextlib

import pytest

SERVER_CFG = {"url": "https://mcp.cloudflare.com/mcp", "auth": "oauth"}


def _recording_probe(seen: list[bool]):
    """A ``_probe_single_server`` stand-in that records the caller's interactivity verdict."""

    def _probe(name, config, connect_timeout=None, *, details=None):
        from tools.mcp_oauth import _is_interactive

        seen.append(_is_interactive())
        return [("echo", "Echo")]

    return _probe


@pytest.fixture
def console_backend(monkeypatch, tmp_path):
    """A backend with a console on stdin, one ``auth: oauth`` server configured, and a probe that
    records whether OAuth would have been allowed to open a browser."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import mcp_config
    from hermes_cli.web_routers import mcp as mcp_routes

    seen: list[bool] = []
    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda: {"cloudflare": dict(SERVER_CFG)})
    monkeypatch.setattr(mcp_config, "_oauth_tokens_present", lambda name: True)
    monkeypatch.setattr(mcp_config, "_probe_single_server", _recording_probe(seen))
    # The router resolves its profile scope through web_server at call time; the scope is not what
    # this test is about.
    monkeypatch.setattr(mcp_routes, "_config_profile_scope", lambda profile: contextlib.nullcontext())
    return seen


@pytest.mark.asyncio
async def test_dashboard_test_endpoint_probes_with_oauth_suppressed(console_backend):
    from hermes_cli.web_routers import mcp as mcp_routes
    from tools.mcp_oauth import _is_interactive, force_interactive_oauth

    seen = console_backend
    # A console-capable backend: the process-level TTY heuristic says "a human is here".
    with force_interactive_oauth():
        result = await mcp_routes.test_mcp_server("cloudflare")
        # ... and the suppression is scoped to the probe, not leaked to the caller.
        assert _is_interactive() is True

    assert result["ok"] is True, result
    assert seen == [False], (
        "POST /api/mcp/servers/{name}/test ran the probe with interactive OAuth enabled "
        "-> the Desktop health sweep opens a consent tab every 30 min")


def test_gateway_test_rpc_probes_with_oauth_suppressed(console_backend):
    from tools.mcp_oauth import _is_interactive, force_interactive_oauth
    from tui_gateway import server

    seen = console_backend
    with force_interactive_oauth():
        resp = server._methods["mcp.servers.test"](1, {"name": "cloudflare"})
        assert _is_interactive() is True

    assert "error" not in resp, resp.get("error")
    assert resp["result"]["ok"] is True, resp["result"]
    assert seen == [False], (
        "the mcp.servers.test RPC ran the probe with interactive OAuth enabled "
        "-> the Desktop health sweep opens a consent tab every 30 min")
