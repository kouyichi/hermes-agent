"""A repaired tool name must reach the ordered provider sidecars (#142590).

``validate_tool_calls`` repairs a hallucinated name on ``tc.function.name`` alone, while both replay
paths prefer the ordered blocks captured at normalization time (``bedrock_content_blocks`` on
Converse, ``anthropic_content_blocks`` on Anthropic) and take the tool name from the block. The next
request then records a call to a name that is not declared in ``toolConfig`` / ``tools``, paired with
the result of the tool that actually ran.
"""

from __future__ import annotations

import pytest

from agent.bedrock_adapter import _assistant_blocks, normalize_converse_response
from agent.transports.types import NormalizedResponse, ToolCall
from agent.turn_tool_validation import validate_tool_calls
from run_agent import AIAgent

VALID = {"skills_list"}


def _converse(content, stop="tool_use"):
    """The normalized assistant message the Converse adapter hands the conversation loop."""
    return normalize_converse_response({
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": {"inputTokens": 1, "outputTokens": 1},
        "modelId": "m",
    }).choices[0].message


def _agent():
    # Real AIAgent methods (name repair, assistant serialization), no init side effects.
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id, agent.tools = "bedrock", "us.amazon.nova-pro-v1:0", "s1", []
    agent.valid_tool_names = set(VALID)
    agent.log_prefix = ""
    agent._invalid_tool_retries = agent._invalid_json_retries = 0
    agent.verbose_logging = False
    agent.suppress_status_output = True
    agent._base_url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    return agent


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import hermes_cli.observability.shared_metrics_model as metrics

    monkeypatch.setattr(metrics, "record_tool_call_quality", lambda *a, **k: None)
    monkeypatch.setattr(AIAgent, "_vprint", lambda self, *a, **k: None, raising=False)


def _validate(message, agent=None):
    agent = agent or _agent()
    verdict = validate_tool_calls(agent, message, "tool_calls", messages=[], conversation_history=[],
                                  api_call_count=1, effective_task_id="t")
    assert verdict.action == "ok"
    return agent


def _replay(message, agent=None, claude=False):
    """The Converse blocks the follow-up request would carry for this assistant turn."""
    from agent.chat_completion_helpers import build_assistant_message

    msg = build_assistant_message(agent or _agent(), message, "tool_calls")
    return _assistant_blocks(msg, msg.get("content") or "", claude=claude)


def test_repaired_name_reaches_the_replayed_converse_blocks():
    message = _converse([
        {"text": "Checking."},
        {"toolUse": {"toolUseId": "tooluse_A", "name": "Skills-List", "input": {"category": "none"}}},
    ])
    _validate(message)
    assert [tc.function.name for tc in message.tool_calls] == ["skills_list"]

    blocks = _replay(message)

    assert [list(block) for block in blocks] == [["text"], ["toolUse"]]  # captured order kept
    assert blocks[1]["toolUse"] == {
        "toolUseId": "tooluse_A", "name": "skills_list", "input": {"category": "none"},
    }


def test_repaired_name_reaches_the_replayed_anthropic_block():
    message = NormalizedResponse(
        content="", finish_reason="tool_calls",
        tool_calls=[ToolCall(id="toolu_1", name="Skills-List", arguments='{"category": "none"}')],
        provider_data={"anthropic_content_blocks": [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "Skills-List", "input": {"category": "none"}},
        ]},
    )
    _validate(message)
    assert [tc.function.name for tc in message.tool_calls] == ["skills_list"]

    blocks = _replay(message, claude=True)

    assert [list(block) for block in blocks] == [["reasoningContent"], ["toolUse"]]
    assert blocks[0]["reasoningContent"]["reasoningText"] == {"text": "hmm", "signature": "sig"}
    assert blocks[1]["toolUse"]["name"] == "skills_list"
    assert blocks[1]["toolUse"]["toolUseId"] == "toolu_1"


def test_a_name_that_needs_no_repair_leaves_the_blocks_untouched():
    message = _converse([
        {"toolUse": {"toolUseId": "tooluse_A", "name": "skills_list", "input": {}}},
        {"toolUse": {"toolUseId": "tooluse_B", "name": "Skills-List", "input": {"category": "x"}}},
    ])
    captured = [dict(block["toolUse"]) for block in message.bedrock_content_blocks]
    _validate(message)

    blocks = _replay(message)
    tool_uses = [block["toolUse"] for block in blocks]

    # Only the repaired call changes; its neighbour keeps the captured bytes.
    assert tool_uses[0]["name"] == "skills_list"
    assert tool_uses[1]["name"] == "skills_list"
    assert message.bedrock_content_blocks[0]["toolUse"] == captured[0]
    assert tool_uses[1]["input"] == {"category": "x"}  # the repair never rewrites the arguments


def test_a_uniquified_call_id_still_matches_its_captured_block():
    """Ids are uniquified before the repair, so the block's id can differ from ``tc.id``."""
    message = _converse([
        {"toolUse": {"toolUseId": "tooluse_A", "name": "Skills-List", "input": {"category": "none"}}},
    ])
    message.tool_calls[0].id = "call_0"
    _validate(message)

    blocks = _replay(message)

    assert blocks[0]["toolUse"]["name"] == "skills_list"
    assert blocks[0]["toolUse"]["toolUseId"] == "tooluse_A"
