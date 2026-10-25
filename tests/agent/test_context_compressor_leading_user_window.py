"""Regression test for #144167: a compacted window must not open with a non-user row.

``protect_first_n`` decays to 0 after the first compaction, so the second compaction runs with
``compress_start == 0``: the assembled window is summary row + retained tail, and the retained tail
can begin with assistant/tool rounds that strict chat templates exempt from alternation. When the
only template-visible tail row was the trailing user ask, ``_summary_placement`` merged the summary
into that row — appending it after the whole bridge, which left the stored window opening with
``assistant`` (``tool`` included). LM Studio/Qwen answer that payload with "No user query found in
messages." and Hermes classifies the 400 non-retryable, so the session is poisoned for good.

The invariant is the one ``test_context_compressor.py`` already asserts for the system-only-head
shape: the first non-system row of a compacted window is ``user``.
"""

import copy
from unittest.mock import patch

import pytest

from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, ContextCompressor


def _rounds(start, count):
    """Assistant(tool_calls) + tool result pairs: template-exempt rows for strict alternation."""
    out = []
    for i in range(start, start + count):
        out.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "run_command", "arguments": "{}"}}],
        })
        out.append({"role": "tool", "content": "x" * 300, "tool_call_id": f"c{i}"})
    return out


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/model", threshold_percent=0.50, protect_first_n=3, protect_last_n=20,
            quiet_mode=True,
        )
    compressor.tail_token_budget = 40
    return compressor


@pytest.fixture
def summary_stub(monkeypatch):
    monkeypatch.setattr(ContextCompressor, "_call_summary_llm", lambda self, prompt, ts: "SUMMARY BODY")


def _first_compaction(summary_stub):
    messages = [{"role": "user", "content": "FIRST REAL REQUEST" + "r" * 400}] + _rounds(0, 10) + [
        {"role": "user", "content": "continue"}
    ]
    return _compressor().compress(copy.deepcopy(messages), force=True)


def _second_compaction(first_pass, summary_stub):
    compressor = _compressor()
    compressor.compression_count = 1
    compressor._previous_summary = "PREVIOUS SUMMARY"
    return compressor.compress(
        copy.deepcopy(first_pass) + _rounds(100, 10) + [{"role": "user", "content": "continue"}],
        force=True,
    )


def test_second_compaction_window_is_user_led(summary_stub):
    """The reported shape: compress_start == 0 and the only visible tail row is the user ask."""
    second = _second_compaction(_first_compaction(summary_stub), summary_stub)

    non_system = [m for m in second if m.get("role") not in ("system", "developer")]
    assert non_system, "expected a non-empty compacted window"
    assert non_system[0]["role"] == "user", (
        "first non-system row must be role=user, got "
        f"{non_system[0]['role']!r} — payload would 400 with 'No user query found in messages.'"
    )


def test_second_compaction_summary_is_its_own_leading_row(summary_stub):
    """The summary stays readable as a leading row instead of being folded into the trailing ask."""
    second = _second_compaction(_first_compaction(summary_stub), summary_stub)

    summary_indices = [i for i, m in enumerate(second) if m.get(COMPRESSED_SUMMARY_METADATA_KEY)]
    assert summary_indices == [0]
    assert "SUMMARY BODY" in second[0]["content"]
    # The callback's own ask must survive verbatim as a separate turn.
    assert second[-1]["role"] == "user"
    assert second[-1]["content"] == "continue"
    assert not second[-1].get(COMPRESSED_SUMMARY_METADATA_KEY)


def test_second_compaction_keeps_the_retained_bridge_in_order(summary_stub):
    """The exempt assistant/tool rounds are neither dropped nor reordered by the repair."""
    second = _second_compaction(_first_compaction(summary_stub), summary_stub)

    assert [m["role"] for m in second] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant", "tool",
        "assistant", "tool", "user",
    ]
    ids = [m.get("tool_call_id") for m in second if m.get("role") == "tool"]
    assert ids == ["c106", "c107", "c108", "c109"]


def test_first_compaction_keeps_its_protected_head(summary_stub):
    """Control: with head protection still active the summary is still placed after the head."""
    first = _first_compaction(summary_stub)

    assert [m["role"] for m in first][:3] == ["user", "assistant", "tool"]
    assert not first[0].get(COMPRESSED_SUMMARY_METADATA_KEY)
    assert [i for i, m in enumerate(first) if m.get(COMPRESSED_SUMMARY_METADATA_KEY)] == [3]
