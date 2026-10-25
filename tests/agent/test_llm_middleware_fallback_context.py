"""The LLM middleware contexts carry ``has_pending_fallback`` (#144527).

A plugin that can turn a reply into a failure — the ``refusal`` catalog plugin marks a detected refusal
``content_filter`` — cannot tell whether Hermes has a fallback provider to move to. With none left,
``handle_content_policy_refusal`` discards the reply for its "declined" notice, so a false positive
throws away a valid answer. Both LLM middleware lanes are driven through their production call sites
here, so the field cannot drift from the guard ``AIAgent._has_pending_fallback`` uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import plugins


class _RecordingManager:
    """Plugin-manager stand-in: one recording middleware callback per LLM kind."""

    def __init__(self) -> None:
        self.seen: dict[str, list[dict]] = {}
        self._middleware: dict[str, list] = {}
        self._discovered = True

    def register(self, kind: str) -> None:
        def record(**kwargs):
            self.seen.setdefault(kind, []).append(kwargs)

        self._middleware.setdefault(kind, []).append(record)

    def has_middleware(self, kind: str) -> bool:
        return bool(self._middleware.get(kind))

    def invoke_middleware(self, kind: str, **kwargs):
        return [callback(**kwargs) for callback in self._middleware.get(kind, [])]

    def has_hook(self, _hook_name: str) -> bool:
        return False

    def iter_hook_callbacks(self, _hook_name: str):
        return ()

    def discover_and_load(self, force: bool = False) -> None:
        return None

    def _report_hook_failure(self, kind, _callback, _call_kwargs, exc, **_kwargs):
        raise AssertionError(f"{kind} middleware failed: {exc!r}")


@pytest.fixture
def manager(monkeypatch):
    manager = _RecordingManager()
    for kind in ("llm_request", "llm_execution"):
        manager.register(kind)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    return manager


@pytest.fixture
def agent():
    from agent.turn_context import _PER_TURN_RESET_STATE
    from run_agent import AIAgent

    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("model_tools.check_toolset_requirements", return_value={}), \
         patch("agent.process_bootstrap.OpenAI"):
        built = AIAgent(
            provider="nous", model="anthropic/claude-opus-5.5", api_key="sk-test-0000",
            base_url="https://inference-api.nousresearch.com/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=[],
            save_trajectories=False, session_id="middleware-context",
        )
    # The turn loop resets these per attempt; a fresh agent must match the state of an in-turn one.
    for name, value in _PER_TURN_RESET_STATE:
        setattr(built, name, value)
    yield built
    built.close()


def _set_chain(agent, chain: list, index: int) -> None:
    agent._fallback_chain = chain
    agent._fallback_index = index


CASES = [
    pytest.param([], 0, False, id="no-fallback-configured"),
    pytest.param(["openai-api/gpt-5.5"], 0, True, id="fallback-available"),
    pytest.param(["openai-api/gpt-5.5"], 1, False, id="fallback-already-used"),
]


@pytest.mark.parametrize("chain,index,expected", CASES)
def test_llm_request_middleware_sees_pending_fallback(manager, agent, chain, index, expected):
    from agent.turn_api_request import build_api_request

    _set_chain(agent, chain, index)
    messages = [{"role": "user", "content": "ping"}]
    build_api_request(
        agent, api_messages=messages, _moa_prepared_request=None, tools_for_api=[],
        system_message="", messages=messages, original_user_message="ping", approx_tokens=0,
        total_chars=0, retry_count=0, api_call_count=1, api_request_id="req-1",
        api_start_time=0.0, effective_task_id="task-1", turn_id="turn-1",
    )

    assert manager.seen["llm_request"][-1]["has_pending_fallback"] is expected


@pytest.mark.parametrize("chain,index,expected", CASES)
def test_llm_execution_middleware_sees_pending_fallback(manager, agent, chain, index, expected):
    from agent.turn_api_call import perform_api_call

    _set_chain(agent, chain, index)
    agent._disable_streaming = True
    agent._interruptible_api_call = lambda _kwargs: SimpleNamespace(
        model=agent.model, choices=[], usage=None)
    kwargs = {"model": agent.model, "messages": [{"role": "user", "content": "ping"}]}
    verdict = perform_api_call(
        agent, api_kwargs=kwargs, _original_api_kwargs=kwargs, _llm_middleware_trace=[],
        _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
        retry_count=0, api_call_count=1, api_request_id="req-1", effective_task_id="task-1",
        turn_id="turn-1", interrupted=False,
    )

    assert verdict.action == "fallthrough"
    assert manager.seen["llm_execution"][-1]["has_pending_fallback"] is expected


def test_partial_agent_doubles_report_no_fallback():
    """Middleware context is built for every agent the loop drives, test doubles included."""
    from agent.api_request_hooks import pending_fallback_available

    assert pending_fallback_available(SimpleNamespace()) is False
    assert pending_fallback_available(SimpleNamespace(_has_pending_fallback=lambda: True)) is True
