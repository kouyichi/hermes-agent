"""The cached agent's signature must describe the state its own build leaves behind.

Building an ``AIAgent`` resolves the configured memory provider, and a provider plugin whose
``register(ctx)`` calls ``ctx.register_tool(...)`` re-registers its tools on every load:
``ToolRegistry.register`` bumps ``_generation`` unconditionally (#144442). ``_resolve_turn_agent``
computes the cache signature from that generation *before* the build, so the entry is stored with a
signature the build itself has already invalidated — every inbound message then misses the cache,
rebuilds the agent and re-initializes the provider, with nothing logged for the miss.

Driven through the production ``_resolve_turn_agent`` with a build stand-in that bumps the registry
generation exactly like a tool-registering provider does, so the assertion is behavioural: the
second turn of an unchanged session must reuse the agent the first turn built.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _restore_registry_generation(monkeypatch):
    """``registry._generation`` is process-global; leave it exactly as this test found it."""
    from tools.registry import registry

    monkeypatch.setattr(registry, "_generation", registry._generation)


def _turn_runner(monkeypatch, *, build_bumps_generation=True):
    """A real ``TurnRunner`` over a real ``GatewayRunner``, with only the build replaced."""
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from tools.registry import registry

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    ctx = TurnContext(
        session_key="telegram:cache-signature-probe",
        session_id="cache-signature-probe-1",
        user_config={},
        enabled_toolsets=["hermes-telegram"],
        source=SimpleNamespace(user_id="probe-user", user_id_alt=None),
    )
    turn_runner = TurnRunner.__new__(TurnRunner)
    turn_runner._runner = runner
    turn_runner._ctx = ctx
    built: list = []

    def _build(*_args, **_kwargs):
        # What a memory provider's ``register(ctx)`` → ``ctx.register_tool(...)`` does to the
        # registry while the agent is being built.
        if build_bumps_generation:
            registry._generation += 1
        agent = SimpleNamespace(max_iterations=0)
        built.append(agent)
        return agent

    monkeypatch.setattr(turn_runner, "_build_fresh_agent", _build)
    monkeypatch.setattr(turn_runner, "_skip_context_files", lambda _key: False)
    monkeypatch.setattr(turn_runner, "_current_message_count", lambda: 1)
    monkeypatch.setattr(turn_runner, "_cached_sid_is_dead", lambda *_args: (None, False))
    monkeypatch.setattr(turn_runner, "_release_evicted_agent", lambda *_args: None)
    monkeypatch.setattr(runner, "_init_cached_agent_for_turn", lambda *_args: None)
    monkeypatch.setattr(runner, "_apply_fallback_chain_to_agent", lambda *_args: None)
    monkeypatch.setattr(runner, "_refresh_fallback_model", lambda: None)
    monkeypatch.setattr(runner, "_enforce_agent_cache_cap", lambda: None)
    return turn_runner, built


def _turn(turn_runner):
    return turn_runner._resolve_turn_agent(
        {"model": "probe-model", "runtime": {"api_key": "k", "provider": "probe"}},
        "telegram", "", 10, None, None,
    )


def test_build_side_tool_registration_does_not_invalidate_the_entry_it_stores(monkeypatch):
    """A tool-registering memory provider must not turn the cache into a rebuild-per-message loop."""
    turn_runner, built = _turn_runner(monkeypatch)

    first, reused_first = _turn(turn_runner)
    assert reused_first is False
    assert len(built) == 1

    second, reused_second = _turn(turn_runner)
    assert reused_second is True, (
        "the build's own tool registration changed the signature it was cached under, so every "
        "turn rebuilt the agent (and re-loaded the memory provider)"
    )
    assert second is first
    assert len(built) == 1


def test_registry_change_between_turns_still_rebuilds_the_cached_agent(monkeypatch):
    """The generation must keep busting the cache when something OTHER than the build bumps it.

    Guards the cheap non-fix of dropping the generation from the cache signature: an MCP reload or
    plugin refresh between turns has to reach the next turn as a fresh agent.
    """
    from tools.registry import registry

    turn_runner, built = _turn_runner(monkeypatch)
    first, _ = _turn(turn_runner)

    registry._generation += 1  # e.g. an MCP server refresh between two inbound messages
    rebuilt, reused = _turn(turn_runner)

    assert reused is False
    assert rebuilt is not first
    assert len(built) == 2
