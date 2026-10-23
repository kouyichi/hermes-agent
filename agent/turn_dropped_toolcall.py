"""Dropped tool-call recovery: a provider reports ``finish_reason="tool_calls"`` while the parsed
``tool_calls`` array is empty (observed on GitHub Copilot's Claude routes, ~2026-07). Ending the
turn there leaves the task unstarted, so the loop re-prompts the model to emit the call.

The re-prompt is bounded twice. Three CONSECUTIVE stalls, refreshed by every landed tool round,
guard one stall; a per-TURN ceiling guards the whole turn, because that refresh is what let a model
fabricate one no-effect call per narration ("echo ok", "true", "pwd") and be re-prompted until the
API budget ran out — 109 calls in one turn, no text delivered (#143040). Past the ceiling the
model's own text becomes the answer, which is what it was trying to say.

Extracted from ``turn_final_response.finish_text_response`` so that function stays inside its
complexity/line cap; the nudge copy stays in ``conversation_loop`` and is imported lazily (cycle).
"""

from __future__ import annotations

import logging
from typing import Any

from agent.message_metadata import append_message

logger = logging.getLogger("agent.conversation_loop")

MAX_NUDGES_PER_TURN = 6
MAX_CONSECUTIVE_STALLS = 3


def dropped_toolcall_reenter(agent: Any, messages: list, final_msg: dict[str, Any]) -> bool:
    """Re-prompt for a dropped tool call; True when the loop must take another API call.

    ``final_msg`` is the assistant row for this response: it is appended as interim scaffolding
    (never durable — ``_dropped_toolcall_nudge`` is an ephemeral flag) together with the nudge, so
    the next request shows the model its own narration plus the instruction. False means the
    recovery is spent for this turn: the caller delivers the text as the answer.
    """
    consecutive = getattr(agent, "_dropped_toolcall_retries", 0)
    nudges = getattr(agent, "_dropped_toolcall_nudges", 0)

    if consecutive >= MAX_CONSECUTIVE_STALLS or nudges >= MAX_NUDGES_PER_TURN:
        return False

    from agent.conversation_loop import _DROPPED_TOOLCALL_NUDGE_CONTENT

    agent._dropped_toolcall_retries = consecutive + 1
    agent._dropped_toolcall_nudges = nudges + 1
    logger.warning(
        "finish_reason=tool_calls with empty tool_calls array "
        "(narration only) — re-prompting to emit the call "
        "(retry %d/%d, turn %d/%d, model=%s provider=%s)",
        agent._dropped_toolcall_retries, MAX_CONSECUTIVE_STALLS,
        agent._dropped_toolcall_nudges, MAX_NUDGES_PER_TURN, agent.model, agent.provider,
    )
    agent._emit_diagnostic_status(
        "↻ Model signaled a tool call but sent none — "
        f"re-prompting ({agent._dropped_toolcall_retries}/{MAX_CONSECUTIVE_STALLS})"
    )
    # Both halves of the re-prompt pair are ephemeral scaffolding: never persisted,
    # and the finalization pop strips an unanswered tail pair.
    final_msg["_dropped_toolcall_nudge"] = True
    append_message(messages, final_msg)
    append_message(messages, {
        "role": "user",
        "content": _DROPPED_TOOLCALL_NUDGE_CONTENT,
        "_dropped_toolcall_nudge": True,
    })
    agent._session_messages = messages
    return True
