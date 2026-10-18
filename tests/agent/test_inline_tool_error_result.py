"""Regression for #141188: a raising inline tool must cost only its own call.

The sequential dispatcher built inline tools with ``error_result=None``, so an exception
inside one escaped to the turn loop and was reported as "Error during API call" for
EVERY call in the same assistant message — valid siblings never ran, and the repeated
failure counted toward the outer-error cap. The concurrent path and the registry path
already convert a raising tool into that call's error result; the inline branch now
does the same.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from run_agent import AIAgent


def _call(call_id, name, args):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(
        name=name, arguments=json.dumps(args)))


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        built = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    built._flush_messages_to_session_db = lambda messages, conversation_history=None: True
    return built


def test_a_raising_inline_tool_costs_only_its_own_call(agent, monkeypatch):
    import agent.tool_executor as executor
    from agent.tool_executor import _execute_tool_calls_sequential

    ran = []

    def boom(_agent, _args, _ctx):
        ran.append("boom")
        raise AttributeError("'list' object has no attribute 'strip'")

    def sibling(_agent, _args, _ctx):
        ran.append("sibling")
        return json.dumps({"marker": "sibling ran"})

    monkeypatch.setitem(executor.INLINE_TOOL_EXECUTORS, "todo_list", boom)
    monkeypatch.setitem(executor.INLINE_TOOL_EXECUTORS, "session_search", sibling)
    messages = []

    _execute_tool_calls_sequential(agent, SimpleNamespace(tool_calls=[
        _call("call_1", "todo_list", {"todos": []}),
        _call("call_2", "session_search", {"query": "anything"}),
    ]), messages, "task-1")

    # Both calls ran, in order: the exception stopped neither the batch nor the sibling.
    assert ran == ["boom", "sibling"]
    results = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    assert results["call_1"] == "Error executing tool 'todo_list': 'list' object has no attribute 'strip'"
    assert json.loads(results["call_2"])["marker"] == "sibling ran"
