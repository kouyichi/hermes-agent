"""Reaction feedback the user left, as a note on the next user message.

``SessionDB.take_unseen_reactions`` stamps each reaction ``seen`` as it reads it, so a reaction is
announced exactly once. Both turn paths render that read through :func:`reaction_notes` — the
legacy TUI turn (``tui_gateway.methods_prompt``) and the canonical gateway turn
(``gateway.run_turn_prepare``) — so one conversation reads the same note whichever surface typed
the next message, and there is one reaction ledger, not two.

The note is model input only, on the user message's api_content sidecar: the reacted message is
never rewritten (that would break the prompt cache) and the persisted user row stays the words the
user typed.
"""

from typing import Any, Iterable, Optional

# The quote is context, not a re-send: a long answer is truncated to its opening.
_SNIPPET_LIMIT = 120


def reaction_note(entry: dict[str, Any]) -> str:
    """One note line for a ``take_unseen_reactions`` entry.

    An attachment-only / tool-call-only row carries no text, and an empty quote reads worse than no
    quote at all, so that case names the earlier message instead.
    """
    snippet = (entry.get("text") or "").strip().replace("\n", " ")
    if len(snippet) > _SNIPPET_LIMIT:
        snippet = snippet[:_SNIPPET_LIMIT] + "…"
    emoji = entry.get("emoji") or ""
    whose = "their own" if entry.get("role") == "user" else "your"
    if snippet:
        return f'[The user reacted {emoji} to {whose} message: "{snippet}"]'
    return f"[The user reacted {emoji} to {whose} earlier message]"


def reaction_notes(entries: Optional[Iterable[dict[str, Any]]]) -> str:
    """The note block for *entries* from one session (``""`` when nothing is pending)."""
    return "\n".join(reaction_note(entry) for entry in entries or ())
