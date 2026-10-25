"""Regression tests for #144167: no non-user row may precede the first user row.

A doubly-compacted session can keep assistant/tool rounds at the head of the wire payload while
the summary (a ``user`` row) lands last — the exact shape local Qwen chat templates reject with
"No user query found in messages." (HTTP 400, classified non-retryable, so the session is
poisoned for every following turn). The Anthropic converter
(``anthropic_message_convert._ensure_leading_user_turn``) and the Bedrock adapter
(``bedrock_adapter``'s leading/trailing guard) already prepend a placeholder user turn; the
OpenAI-compatible path every local server uses relied on the pre-API sanitizer, which only ever
repaired empty turns and tool pairing.

These tests drive the real pre-API chokepoint (``AIAgent._sanitize_api_messages``) with the role
sequence from the report, so they pin wire behaviour rather than a new helper's signature.
"""

from run_agent import AIAgent


def _msg(role, content, **extra):
    return {"role": role, "content": content, **extra}


_CALL_A = [{"id": "call_aaa", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}]
_CALL_BC = [
    {"id": "call_bbb", "type": "function", "function": {"name": "web_search", "arguments": "{}"}},
    {"id": "call_ccc", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
]


def _roles(messages):
    return [m["role"] for m in messages]


def _first_non_header_role(messages):
    return next(m["role"] for m in messages if m["role"] not in ("system", "developer"))


def _compacted_transcript():
    """The reported payload shape: system,assistant,tool,assistant,tool,tool,user."""
    return [
        _msg("system", "You are Hermes Agent."),
        _msg("assistant", "a", tool_calls=_CALL_A),
        _msg("tool", "out", tool_call_id="call_aaa"),
        _msg("assistant", "a2", tool_calls=_CALL_BC),
        _msg("tool", "out2", tool_call_id="call_bbb"),
        _msg("tool", "out3", tool_call_id="call_ccc"),
        _msg("user", "[CONTEXT COMPACTION — REFERENCE ONLY] summary. Now answer the question."),
    ]


def test_compacted_assistant_led_head_gains_a_leading_user_turn():
    """The reported shape must reach the provider with a user row before the first non-user row."""
    wire = AIAgent._sanitize_api_messages(_compacted_transcript())

    assert _first_non_header_role(wire) == "user"
    # The repair is a single insertion: the compacted rounds and the trailing summary survive
    # in order, so the model still sees the work that was carried over.
    assert _roles(wire) == ["system", "user", "assistant", "tool", "assistant", "tool", "tool", "user"]
    assert wire[-1]["role"] == "user"
    assert wire[0] == _compacted_transcript()[0]


def test_orphaned_tool_head_gains_a_leading_user_turn():
    """A tool row whose assistant call was compacted away is dropped — the head must still be user."""
    wire = AIAgent._sanitize_api_messages(
        [
            _msg("system", "sys"),
            _msg("tool", "out", tool_call_id="call_gone"),
            _msg("assistant", "a2"),
            _msg("user", "hi"),
        ]
    )

    assert _first_non_header_role(wire) == "user"
    assert "tool" not in _roles(wire)


def test_well_formed_transcript_is_returned_unchanged():
    """The ordinary case takes no insertion — this must not pad every request."""
    messages = [_msg("system", "sys"), _msg("user", "hi"), _msg("assistant", "yo")]

    wire = AIAgent._sanitize_api_messages(messages)

    assert wire == messages
    assert wire is not messages


def test_placeholder_row_carries_non_whitespace_text():
    """A blank filler trades the leading-role 400 for the empty-content one (see #69512 class)."""
    wire = AIAgent._sanitize_api_messages(_compacted_transcript())

    filler = wire[1]
    assert filler["role"] == "user"
    assert isinstance(filler["content"], str) and filler["content"].strip()
    # Tool-call scaffolding on a synthesized row would look like a real user tool request.
    assert "tool_calls" not in filler


def test_developer_header_is_treated_as_a_header_not_the_user_turn():
    """GPT-5/Codex routes rename the system row before sending, so 'developer' must count as header."""
    wire = AIAgent._sanitize_api_messages(
        [
            _msg("developer", "sys"),
            _msg("assistant", "a", tool_calls=_CALL_A),
            _msg("tool", "out", tool_call_id="call_aaa"),
            _msg("user", "question"),
        ]
    )

    assert _roles(wire) == ["developer", "user", "assistant", "tool", "user"]


def test_stored_transcript_is_not_mutated_by_the_repair():
    """The repair is per-call: persisted history must keep its original order."""
    stored = _compacted_transcript()
    before = _roles(stored)

    AIAgent._sanitize_api_messages(stored)

    assert _roles(stored) == before
