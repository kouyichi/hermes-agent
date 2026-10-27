"""A live ``memory.*`` edit must reach a running gateway session (#145284).

The gateway reuses a cached ``AIAgent`` for as long as its config signature stays
equal (``gateway/session_execution._resolve_turn_agent``). The built-in memory
store is constructed once at agent init from ``memory.memory_char_limit`` /
``user_char_limit``, and the enabled flags plus ``nudge_interval`` are frozen the
same way, so those values are construction inputs: leaving them out of the
signature makes a memory-limit edit the one config change that neither applies to
the live agent nor invalidates the cache.

These tests drive the real reuse decision (signature -> cache lookup -> rebuild)
against the real ``_CACHE_BUSTING_CONFIG_KEYS`` table; only the agent builder is
stubbed, since the subject is the cache decision, not ``AIAgent`` construction.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session_execution import GatewaySessionAgentMixin
from gateway.turn_context import TurnContext
from hermes_cli.config import DEFAULT_CONFIG

RUNTIME = {"api_key": "k", "base_url": "https://api.example/v1", "provider": "openai"}
ROUTE = {"model": "m", "runtime": RUNTIME, "request_overrides": None}
SESSION_KEY = "telegram:145284"


class _Agent:
    """The attributes the cache touches on a reused agent."""

    _last_activity_ts = 0.0
    _last_activity_desc = ""
    _last_activity_provenance = None
    _last_flushed_db_idx = 0
    _api_call_count = 0
    max_iterations = 0


class _Harness(GatewaySessionAgentMixin):
    """Real mixin, real runner, stubbed builder — records what was built."""

    def __init__(self, runner, ctx):
        self._runner = runner
        self._ctx = ctx
        self.built: list[_Agent] = []

    def _build_fresh_agent(self, *_args, **_kwargs):
        agent = _Agent()
        self.built.append(agent)
        return agent


def _runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = None
    runner._prefill_messages = None
    runner._service_tier = None
    runner._enforce_agent_cache_cap = lambda: None
    return runner


def _ctx(user_config: dict) -> TurnContext:
    source = type(
        "Source",
        (),
        {
            "platform": Platform.TELEGRAM,
            "chat_id": "1",
            "user_id": "u1",
            "user_id_alt": None,
            "user_name": "u",
            "chat_name": "c",
            "chat_type": "dm",
            "thread_id": None,
        },
    )()
    return TurnContext(
        source=source,
        session_key=SESSION_KEY,
        session_id=None,
        user_config=user_config,
        enabled_toolsets=[],
    )


def _turn(harness: _Harness):
    """One inbound turn: ``(agent, reused_cached_agent)``."""
    return harness._resolve_turn_agent(ROUTE, "telegram", "", 30, None, {})


def _memory_config(**overrides) -> dict:
    return {"memory": {"provider": "", **overrides}}


def test_an_unchanged_memory_section_keeps_the_cached_agent():
    """Baseline: nothing changed -> the cached agent is reused, not rebuilt."""
    harness = _Harness(_runner(), _ctx(_memory_config()))

    first, first_reused = _turn(harness)
    second, second_reused = _turn(harness)

    assert first_reused is False
    assert second_reused is True
    assert second is first


@pytest.mark.parametrize(
    "override",
    [
        {"memory_char_limit": 15000},  # the report's own edit (10,000 -> 15,000)
        {"user_char_limit": 5000},
        {"nudge_interval": 3},
        {"memory_enabled": False},
        {"user_profile_enabled": False},
    ],
    ids=[
        "memory_char_limit",
        "user_char_limit",
        "nudge_interval",
        "memory_enabled",
        "user_profile_enabled",
    ],
)
def test_a_live_memory_config_edit_rebuilds_the_cached_agent(override):
    """Every memory value the store freezes at init must invalidate the cache."""
    ctx = _ctx(_memory_config())
    harness = _Harness(_runner(), ctx)

    before, _ = _turn(harness)
    ctx.user_config = _memory_config(**override)
    after, reused = _turn(harness)

    assert reused is False, (
        f"a live edit of memory.{next(iter(override))} reused the cached agent, so the "
        "store built at init keeps the old cap for the life of the session"
    )
    assert after is not before
    assert len(harness.built) == 2


def test_an_explicit_default_matches_an_absent_memory_key():
    """Writing the shipped value into config.yaml must not churn the cached agent."""
    defaults = DEFAULT_CONFIG["memory"]
    explicit = _memory_config(
        memory_char_limit=defaults["memory_char_limit"],
        user_char_limit=defaults["user_char_limit"],
        nudge_interval=defaults["nudge_interval"],
        memory_enabled=True,
        user_profile_enabled=True,
    )
    harness = _Harness(_runner(), _ctx({"memory": {"provider": ""}}))

    first, _ = _turn(harness)
    harness._ctx.user_config = explicit
    second, reused = _turn(harness)

    assert reused is True
    assert second is first


def test_a_rebuilt_agent_reads_the_edited_limits_from_disk():
    """The other half: agent init re-reads config, so the rebuild lands the edited cap.

    Together with the cache-invalidation tests above this closes the loop the report
    describes — the store that replaces the stale one is built from the file.
    """
    from hermes_cli.config import get_config_path
    from run_agent import AIAgent

    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    def _limits(session_id: str) -> tuple[int, int]:
        agent = AIAgent(
            model="anthropic/claude-sonnet-4",
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            session_id=session_id,
        )
        store = agent._memory_store
        return store.memory_char_limit, store.user_char_limit

    config_path.write_text(
        "memory:\n  memory_char_limit: 10000\n  user_char_limit: 3000\n", encoding="utf-8"
    )
    assert _limits("s145284-a") == (10000, 3000)

    # A different LENGTH on purpose: the config layer keys its cache on the file's stat signature.
    config_path.write_text(
        "memory:\n  memory_char_limit: 17500\n  user_char_limit: 3000\n", encoding="utf-8"
    )
    assert _limits("s145284-b") == (17500, 3000)


def test_the_memory_limits_reach_the_signature_through_the_documented_table():
    """The values travel as cache-busting keys, so the table must name them."""
    from gateway.run import GatewayRunner as Runner

    keys = {(section, key) for section, key in Runner._CACHE_BUSTING_CONFIG_KEYS}
    assert {
        ("memory", "memory_char_limit"),
        ("memory", "user_char_limit"),
        ("memory", "nudge_interval"),
        ("memory", "memory_enabled"),
        ("memory", "user_profile_enabled"),
    } <= keys
