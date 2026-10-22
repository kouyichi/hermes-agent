"""#142969 — an enabled platform with no usable credential is skipped, not a fatal startup conflict.

Since the gateway became the runtime every local CLI session depends on, a platform whose connect
fails *non-retryably* while nothing else connected exits ``GATEWAY_FATAL_CONFIG_EXIT_CODE`` (78) and
takes those sessions down with it (``Gateway draining: not_ready``).

The reported shape is a leftover ``WHATSAPP_ENABLED=true`` that was never paired: no retry can heal
it, and the credential predicate the startup already owns (``_platform_has_bot_credential``) knows it
is unusable — but only the multiplex branch consulted it. These tests pin the skip, the status row
that keeps the dead channel visible, and the fact that a platform which *does* have a credential is
still created and connected.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig


def _runner(config: GatewayConfig):
    """A GatewayRunner wired only for the startup prefilter (no real adapters, no real status file)."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {}
    runner._failed_platforms = {}
    runner._profile_adapters = {}
    runner._busy_text_mode = "off"
    runner.session_store = MagicMock()
    runner._shutdown_event = MagicMock()
    runner._running = True
    created: list = []
    statuses: list = []

    def _create(platform, config):
        created.append(platform)
        return MagicMock()

    async def _no_abort(*_args, **_kwargs):
        return False

    runner._create_adapter = _create
    runner._abort_startup_if_shutdown_requested = _no_abort
    runner._update_platform_runtime_status = lambda *a, **k: statuses.append((a, k))
    runner._adapter_may_heal = lambda *_a, **_k: False
    runner._wire_adapter_handlers = lambda *_a, **_k: None
    return runner, created, statuses


def _whatsapp_config(tmp_path, *, paired: bool) -> GatewayConfig:
    """Only WhatsApp enabled, its session folder either paired (creds.json) or empty."""
    session = tmp_path / "whatsapp-session"
    session.mkdir(parents=True, exist_ok=True)
    if paired:
        (session / "creds.json").write_text("{}", encoding="utf-8")
    config = GatewayConfig()
    platform_config = PlatformConfig(enabled=True)
    platform_config.extra = {"session_path": str(session)}
    config.platforms[Platform.WHATSAPP] = platform_config
    return config


@pytest.mark.asyncio
async def test_unpaired_whatsapp_is_skipped_and_stamped(tmp_path):
    """No adapter is created for an unpaired session, and its status row says why."""
    runner, created, statuses = _runner(_whatsapp_config(tmp_path, paired=False))

    _aborted, enabled_count, skipped, pending = await runner._start_prefilter_platforms()

    assert created == [] and pending == []
    assert enabled_count == 0
    assert skipped == []  # the multiplex-only list: nothing else will serve this platform
    (platform,), kwargs = statuses[0]
    assert platform == "whatsapp"
    assert kwargs["platform_state"] == "disabled"
    assert kwargs["error_code"] == "whatsapp_unpaired"
    assert "whatsapp" in kwargs["error_message"]


@pytest.mark.asyncio
async def test_paired_whatsapp_is_still_connected(tmp_path):
    """The skip is about the missing credential, not about WhatsApp."""
    runner, created, statuses = _runner(_whatsapp_config(tmp_path, paired=True))

    _aborted, enabled_count, _skipped, pending = await runner._start_prefilter_platforms()

    assert created == [Platform.WHATSAPP]
    assert [platform for platform, _cfg, _adapter in pending] == [Platform.WHATSAPP]
    assert enabled_count == 1
    assert statuses == []


@pytest.mark.asyncio
async def test_token_platform_without_credential_is_skipped(tmp_path):
    """The same rule for a token platform: an empty token cannot connect and cannot heal itself."""
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, token="")
    runner, created, statuses = _runner(config)

    _aborted, enabled_count, _skipped, pending = await runner._start_prefilter_platforms()

    assert created == [] and pending == []
    assert enabled_count == 0
    (platform,), kwargs = statuses[0]
    assert platform == "telegram"
    assert kwargs["platform_state"] == "disabled"
    assert kwargs["error_code"] == "credential_missing"


@pytest.mark.asyncio
async def test_token_platform_with_credential_is_connected(tmp_path):
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, token="123:abc")
    runner, created, statuses = _runner(config)

    _aborted, enabled_count, _skipped, _pending = await runner._start_prefilter_platforms()

    assert created == [Platform.TELEGRAM]
    assert enabled_count == 1
    assert statuses == []


@pytest.mark.asyncio
async def test_multiplex_keeps_its_quiet_skip(tmp_path):
    """Under multiplex the platform may be served by a secondary: no ``disabled`` stamp, same list."""
    config = _whatsapp_config(tmp_path, paired=False)
    config.multiplex_profiles = True
    runner, created, statuses = _runner(config)

    _aborted, enabled_count, skipped, pending = await runner._start_prefilter_platforms()

    assert created == [] and pending == []
    assert enabled_count == 0
    assert skipped == [Platform.WHATSAPP]
    assert statuses == []


@pytest.mark.asyncio
async def test_gateway_serves_sessions_when_the_only_platform_is_unpaired(tmp_path):
    """End to end of the decision: nothing connected, nothing failed — the runtime stays up."""
    runner, _created, _statuses = _runner(_whatsapp_config(tmp_path, paired=False))
    _aborted, enabled_count, _skipped, _pending = await runner._start_prefilter_platforms()

    assert enabled_count == 0  # nothing is attempted, so nothing can fail non-retryably
    exit_now = runner._start_handle_no_connections(0, enabled_count, [], [])

    assert exit_now is False
    assert getattr(runner, "_exit_code", None) is None
    # The shape this issue was filed against: a non-retryable failure with nothing connected.
    assert runner._start_handle_no_connections(0, 1, [], ["whatsapp: unpaired"]) is True
