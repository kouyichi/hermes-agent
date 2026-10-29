"""A cron turn's machine-injected user row carries ``display_kind="cron_prompt"`` (#146134).

Cron turns are persisted as plain ``role="user"`` rows whose content starts with
``[IMPORTANT: You are running as a scheduled cron job. ...]``. ``messages.display_kind`` already exists
for exactly this ("machine provenance, display only, stripped before every provider request"), but the
cron lanes never filled it, so every consumer had to match that prefix by string — which changes without
notice and silently un-classifies historical rows.

Two lanes write a cron user row, and both are covered here:
* the cron turn itself (``cron.scheduler._run_agent_with_watchdog`` -> ``agent.run_conversation``);
* the cron delivery mirror (``cron.scheduler_delivery._maybe_mirror_cron_delivery`` -> ``gateway.mirror``).

The expected value is spelled as a literal on purpose: the source-side constant is imported inside its own
test, so reverting the implementation fails these tests on behaviour instead of erroring at collection.
"""

from unittest.mock import patch

from cron import scheduler
from gateway.mirror import mirror_to_session

CRON_PROMPT_KIND = "cron_prompt"


class _RecordingAgent:
    """Stands in for ``AIAgent`` at the one seam cron drives: ``run_conversation``."""

    def __init__(self):
        self.calls: list[dict] = []

    def run_conversation(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return {"final_response": "nightly report"}

    def get_activity_summary(self):
        return {"seconds_since_active": 0.0}


def test_cron_turn_types_its_prompt_row(monkeypatch):
    """The persisted user row is the cron prompt, so the turn must say so."""
    # 0 = unlimited: keeps the real call on the future's own completion instead of the watchdog poll.
    monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 0)
    agent = _RecordingAgent()

    result = scheduler._run_agent_with_watchdog(
        agent, "[IMPORTANT: You are running as a scheduled cron job.] check the build",
        {"id": "job-1", "name": "nightly"}, "job-1", "nightly", "task-1", None,
    )

    assert result == {"final_response": "nightly report"}
    assert len(agent.calls) == 1
    assert agent.calls[0]["persist_user_display_kind"] == CRON_PROMPT_KIND


def test_the_delivery_mirror_passes_the_kind():
    """The mirror row is machine-generated too, so the cron lane must type it."""
    from cron.scheduler_delivery import _maybe_mirror_cron_delivery

    with patch("gateway.mirror.mirror_to_session", return_value=True) as mirror:
        _maybe_mirror_cron_delivery(
            {"id": "job-1", "name": "nightly"}, "telegram", "12345", "Mirrored brief",
            enabled=True,
        )

    assert mirror.call_count == 1
    kwargs = mirror.call_args.kwargs
    assert kwargs["display_kind"] == CRON_PROMPT_KIND
    assert kwargs["role"] == "user"


def test_the_mirror_row_reaches_the_store_typed(tmp_path):
    """End to end: the kind survives ``gateway.mirror`` into the messages table."""
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("cron-session", source="telegram")

    with patch("hermes_state_registry.acquire", return_value=db), \
         patch("hermes_state_registry.release_or_close", lambda _db: None):
        mirrored = mirror_to_session(
            "telegram", "12345", "[Cron delivery: nightly]\nMirrored brief",
            source_label="cron", role="user", session_id="cron-session",
            display_kind=CRON_PROMPT_KIND,
        )

    assert mirrored is True
    assert [row["display_kind"] for row in db.get_messages("cron-session")] == [CRON_PROMPT_KIND]


def test_an_untyped_mirror_row_keeps_the_column_null(tmp_path):
    """Every other caller (``send_message``) must stay untyped — the kind is opt-in."""
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat-session", source="telegram")

    with patch("hermes_state_registry.acquire", return_value=db), \
         patch("hermes_state_registry.release_or_close", lambda _db: None):
        mirror_to_session("telegram", "12345", "agent reply", session_id="chat-session")

    assert [row["display_kind"] for row in db.get_messages("chat-session")] == [None]


def test_the_shared_constant_matches_the_persisted_value():
    """``cron.scheduler_prompt`` owns the vocabulary; the persisted literal must be that value."""
    from cron import scheduler_prompt

    assert getattr(scheduler_prompt, "CRON_PROMPT_DISPLAY_KIND", None) == CRON_PROMPT_KIND


def test_the_kind_is_not_a_provider_facing_field():
    """``display_kind`` is stripped before the request, so typing the row cannot change the wire."""
    from agent.message_metadata import PERSISTENCE_ONLY_MESSAGE_FIELDS

    assert "display_kind" in PERSISTENCE_ONLY_MESSAGE_FIELDS
