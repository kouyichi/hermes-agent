"""Regression for #141188: a non-string `content`/`old_text` used to reach the store.

``MemoryStore.add`` called ``content.strip()``, so a model that packed several facts into
one ``content`` list raised ``AttributeError`` inside an inline tool. Inline tools were
dispatched without an ``error_result``, so that exception escaped as "Error during API
call" for EVERY call in the assistant message — the valid sibling never ran. Two more
shapes of the same defect were confirmed against ``main`` while writing this: the batch
path raised ``TypeError: normalize() argument 2 must be str, not list`` from the content
scan, and ``patch`` with a list ``content`` was the worst of the three — it stringified
the Python repr into the entry (``entry becomes ['item B done']``) and reported success.

Every shape is now refused by name before anything is written.
"""

import json
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tools.memory_tool import memory_tool
from tools.memory_tool_store import MemoryStore

EXISTING = "keep me"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    assert s.add("memory", EXISTING)["success"] is True
    return s


def _call(store, **kwargs):
    return json.loads(memory_tool(target="memory", store=store, **kwargs))


def _entries(store):
    return store._entries_for("memory")


# (kwargs, the argument the error must name, the type it must report)
SINGLE_OP_CASES = {
    "add/list": (dict(action="add", content=["Prefers Python", "Lives in Berlin"]), "content", "list"),
    "add/int": (dict(action="add", content=42), "content", "int"),
    "add/dict": (dict(action="add", content={"a": 1}), "content", "dict"),
    "patch/list_content": (dict(action="patch", old_text=EXISTING, content=["keep me again"]), "content", "list"),
    "patch/int_old_text": (dict(action="patch", old_text=7, content="x"), "old_text", "int"),
    "remove/list_old_text": (dict(action="remove", old_text=["keep"]), "old_text", "list"),
}


@pytest.mark.parametrize("case", list(SINGLE_OP_CASES), ids=list(SINGLE_OP_CASES))
def test_single_op_refuses_a_non_string_argument_before_writing(store, case):
    kwargs, argument, type_name = SINGLE_OP_CASES[case]

    result = _call(store, **kwargs)

    assert result["success"] is False
    assert f"'{argument}' must be a string" in result["error"]
    assert f"got {type_name}" in result["error"]
    # Nothing was written and nothing was stringified into the entry.
    assert _entries(store) == [EXISTING]


BATCH_CASES = {
    "add/list_content": (dict(action="add", operations=[{"action": "add", "content": ["a", "b"]}]),
                         "Operation 1: 'content' must be a string", "list"),
    "patch/list_old_text": (dict(action="add", operations=[
        {"action": "patch", "old_text": ["keep"], "content": "y"}]), "Operation 1: 'old_text'", "list"),
    "remove/int_content": (dict(action="add", operations=[
        {"action": "remove", "old_text": EXISTING, "content": 1}]), "Operation 1: 'content'", "int"),
    "op_is_a_string": (dict(action="add", operations=["add"]),
                       "Operation 1 must be an object", "str"),
    "second_op_is_malformed": (dict(action="add", operations=[
        {"action": "add", "content": "fine"}, {"action": "add", "content": ["bad"]}]),
        "Operation 2: 'content' must be a string", "list"),
}


@pytest.mark.parametrize("case", list(BATCH_CASES), ids=list(BATCH_CASES))
def test_batch_refuses_a_non_string_argument_before_writing(store, case):
    kwargs, expected_error, type_name = BATCH_CASES[case]

    result = _call(store, **kwargs)

    assert result["success"] is False
    assert expected_error in result["error"]
    assert f"got {type_name}" in result["error"]
    assert _entries(store) == [EXISTING]


def test_the_refusal_names_the_batch_form_that_keeps_every_fact(store):
    result = _call(store, action="add", content=["Prefers Python", "Lives in Berlin"])

    assert "operations" in result["error"]
    assert "one fact" in result["error"]


def test_a_valid_sibling_call_still_saves_after_the_refusal(store):
    assert _call(store, action="add", content=["Prefers Python"])["success"] is False

    assert _call(store, action="add", content="Project uses uv")["success"] is True
    assert _entries(store) == [EXISTING, "Project uses uv"]


def test_inline_executor_returns_the_error_instead_of_raising(store):
    """The dispatch contract: an inline tool answers with a tool error, never an exception."""
    agent = SimpleNamespace(_memory_store=store, _memory_manager=None)

    result = json.loads(INLINE_TOOL_EXECUTORS["memory"](
        agent, {"action": "add", "target": "memory", "content": ["Prefers Python"]},
        InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")))

    assert result["success"] is False
    assert "'content' must be a string" in result["error"]
