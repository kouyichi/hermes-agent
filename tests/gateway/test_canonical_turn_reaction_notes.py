"""A reaction the user left reaches the next CANONICAL gateway turn's model input.

The storage half (``SessionDB.set_message_reaction``) and the note renderer existed, but only the
legacy TUI turn consumed them: on the shared gateway a reaction was persisted and never announced
(#145472). These drive the canonical prepare step against a real session store, so they assert
behaviour — the note reaches the turn's model-bound sidecar notes exactly once — rather than the
shape of the wiring.
"""
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import SessionDB

try:  # the fix's own module; absent on the unpatched tree, where these tests must fail on behaviour
    from gateway.session_reaction_notes import reaction_notes as _reaction_notes
except ImportError:  # pragma: no cover - the sabotage tree
    _reaction_notes = None

REACTION = "\U0001f44d"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda: {"display": {"message_reactions": True}})
    db = SessionDB(db_path=tmp_path / "state.db")
    session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig(platforms={}))
    session_store._db = db  # production: the store and the authority share the profile's state.db
    try:
        yield session_store
    finally:
        db.close()


def _reacted_answer(store, emoji=REACTION, author="user", text="Probe answer"):
    """*sid* with one persisted assistant answer carrying a reaction from *author*."""
    db = store._db
    sid = db.create_session("key", "local")
    db.append_message(sid, "assistant", text)
    row = db.latest_message_row_id(sid, role="assistant")
    db.set_message_reaction(sid, row, emoji, author=author)
    return sid, row


def _runner(store):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={})
    runner.adapters = {}
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)

    async def _none(*_args, **_kwargs):
        return None

    async def _open(*_args):
        return False, False

    async def _history(*_args, **_kwargs):
        return []

    runner._hmwa_open_session = _open
    runner._set_session_env = lambda context: []
    runner._hmwa_acquire_turn_lease = runner._mark_durable_active_turn = _none
    runner._hmwa_first_contact_notes = _none
    runner._voice_channel_sidecar_note = lambda event, source, session_key: None
    runner._hmwa_run_session_hygiene = _history
    runner._pinned_session_context_prompt = lambda *_args, **_kwargs: ""
    return runner


async def _prepare(runner, sid, *, internal=False):
    event = MessageEvent(
        text="hello", source=SessionSource(platform=Platform.LOCAL, chat_id="local-chat"),
        message_id="adm-1", internal=internal)
    entry = SimpleNamespace(yolo=None, session_id=sid, session_key="key")
    return await runner._hmwa_prepare_turn(event, event.source, entry, "key", "key", 1)


def _staged(runner):
    return runner._consume_pending_turn_sidecar_notes("key")


@pytest.mark.asyncio
async def test_a_user_reaction_reaches_the_next_canonical_turn_once(store, monkeypatch):
    monkeypatch.setattr("gateway.run_turn_prepare.build_session_context", lambda *_args: None)
    sid, _row = _reacted_answer(store)
    runner = _runner(store)

    turn, _tokens = await _prepare(runner, sid)

    notes = _staged(runner)
    assert any("reacted" in note and "Probe answer" in note for note in notes), notes
    # The note rides the sidecar; the persisted user row is still the words the user typed.
    assert turn.message_text == "hello"
    # Announced once: the read stamped the reaction seen, so the next turn carries no note.
    await _prepare(runner, sid)
    assert not any("reacted" in note for note in _staged(runner))


@pytest.mark.asyncio
async def test_the_reaction_is_not_consumed_while_the_setting_is_off(store, monkeypatch):
    monkeypatch.setattr("gateway.run_turn_prepare.build_session_context", lambda *_args: None)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"display": {"message_reactions": False}})
    sid, row = _reacted_answer(store)
    runner = _runner(store)

    await _prepare(runner, sid)

    assert not any("reacted" in note for note in _staged(runner))
    assert [entry.get("emoji") for entry in store._db.get_message_reactions(sid, row)] == [REACTION]


@pytest.mark.asyncio
async def test_an_agent_authored_reaction_is_never_announced(store, monkeypatch):
    monkeypatch.setattr("gateway.run_turn_prepare.build_session_context", lambda *_args: None)
    sid, _row = _reacted_answer(store, author="agent")
    runner = _runner(store)

    await _prepare(runner, sid)

    assert not any("reacted" in note for note in _staged(runner))


@pytest.mark.asyncio
async def test_an_injected_turn_does_not_consume_the_reaction(store, monkeypatch):
    """A heartbeat/goal turn is not the user's next message: the note must wait for a typed one."""
    monkeypatch.setattr("gateway.run_turn_prepare.build_session_context", lambda *_args: None)
    sid, _row = _reacted_answer(store)
    runner = _runner(store)

    await _prepare(runner, sid, internal=True)
    assert not any("reacted" in note for note in _staged(runner))

    await _prepare(runner, sid)
    assert any("reacted" in note for note in _staged(runner))


@pytest.mark.asyncio
async def test_a_messaging_turn_neither_announces_nor_consumes(store, monkeypatch):
    """A messaging adapter delivers a reaction as its own platform event: no second note there."""
    monkeypatch.setattr("gateway.run_turn_prepare.build_session_context", lambda *_args: None)
    sid, row = _reacted_answer(store)
    runner = _runner(store)
    event = MessageEvent(
        text="hello", source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1"),
        message_id="adm-1")
    entry = SimpleNamespace(yolo=None, session_id=sid, session_key="key")

    await runner._hmwa_prepare_turn(event, event.source, entry, "key", "key", 1)

    assert not any("reacted" in note for note in _staged(runner))
    assert [entry.get("emoji") for entry in store._db.get_message_reactions(sid, row)] == [REACTION]


def test_reaction_notes_render_quotes_and_survive_quote_hostile_rows():
    render = _reaction_notes
    if render is None:
        pytest.skip("gateway.session_reaction_notes is not on this tree")

    quoted = render([{"emoji": REACTION, "role": "assistant", "text": "Probe\nanswer"}])
    assert quoted == f'[The user reacted {REACTION} to your message: "Probe answer"]'
    # A long answer is quoted short: the note is context, not a re-send of the message.
    long_note = render([{"emoji": REACTION, "role": "assistant", "text": "x" * 200}])
    assert len(long_note) < 200 and long_note.endswith('…"]')
    # Attachment-only / tool-call-only rows have no text: no quote beats an empty one.
    assert render([{"emoji": REACTION, "role": "assistant", "text": ""}]) == \
        f"[The user reacted {REACTION} to your earlier message]"
    # The user reacting to their own message is not feedback about the answer.
    assert "their own" in render([{"emoji": REACTION, "role": "user", "text": "mine"}])
    assert render([]) == ""
