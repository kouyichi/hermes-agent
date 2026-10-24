"""The Matrix exec-approval prompt's reaction window must be the same ``approvals.timeout``
the waiting side uses; ``MATRIX_APPROVAL_TIMEOUT_SECONDS`` stays an explicit override.

The card and the parked turn used to run on independent clocks: the adapter stamped a
hardcoded 300s onto ``expires_at`` while ``tools.approval_context._get_approval_timeout()``
kept waiting for the configured value, so a deployment with ``approvals.timeout: 86400``
saw its reaction controls die (and its late answer get swallowed) after five minutes.
"""

from __future__ import annotations

import time
import types
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult


def _adapter(monkeypatch, *, env_timeout=None):
    monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@user:example.org")
    if env_timeout is None:
        monkeypatch.delenv("MATRIX_APPROVAL_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MATRIX_APPROVAL_TIMEOUT_SECONDS", str(env_timeout))
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(
        PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
    adapter._user_id = "@bot:example.org"
    return adapter


async def _prompt_window(monkeypatch, adapter, approvals: dict) -> float:
    """Drive the real prompt-send path and return the lifetime stamped on the prompt."""
    captured: dict[str, float] = {}
    monkeypatch.setattr("tools.approval_context._get_approval_config", lambda: approvals)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="$prompt"))
    adapter._send_reaction = AsyncMock(return_value="$reaction")

    def _make(message_id, requester, expires_at):
        captured["window"] = expires_at - time.monotonic()
        return types.SimpleNamespace(bot_reaction_events={})

    await adapter._send_reaction_prompt(
        "!room:example.org", "approval?", {"requester_user_id": "@user:example.org"},
        _make, {}, ("✅", "❌"), "approval")
    return captured["window"]


class TestMatrixApprovalPromptWindow:

    @pytest.mark.asyncio
    async def test_window_follows_configured_approvals_timeout(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        window = await _prompt_window(monkeypatch, adapter, {"timeout": 86400})
        assert abs(window - 86400) < 5, window

    @pytest.mark.asyncio
    async def test_env_var_stays_an_explicit_override(self, monkeypatch):
        adapter = _adapter(monkeypatch, env_timeout=120)
        window = await _prompt_window(monkeypatch, adapter, {"timeout": 86400})
        assert abs(window - 120) < 5, window

    @pytest.mark.asyncio
    async def test_default_window_unchanged_without_config(self, monkeypatch):
        adapter = _adapter(monkeypatch)
        window = await _prompt_window(monkeypatch, adapter, {})
        assert abs(window - 300) < 5, window
