"""A LOCAL (Desktop) turn announces the user's reactions on the canonical gateway path.

The note block is the same announcement the TUI gateway prepends
(``tui_gateway.methods_prompt._pending_reaction_notes``): a Desktop conversation served by
``gateway.run`` never runs that lane, so a stored reaction was never surfaced to the model (#145472).
Gated on ``display.message_reactions``, and announced exactly once — reading marks the rows seen.
"""
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _FakeDB:
    """Stand-in for the AsyncSessionDB: ``take_unseen_reactions`` is async and consumes rows."""

    def __init__(self, pending):
        self.pending = list(pending)
        self.calls = []

    async def take_unseen_reactions(self, session_id, *, author="user"):
        self.calls.append((session_id, author))
        pending, self.pending = self.pending, []
        return pending


def _runner(monkeypatch, db, session_id='sid-1'):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={})
    runner.adapters = {}
    runner._session_db = db
    runner.session_store = SimpleNamespace(peek_session_id=lambda _key: session_id)

    async def _context_length(_source, _session_key):
        return 128_000

    monkeypatch.setattr(runner, '_inbound_model_context_length', _context_length, raising=False)
    return runner


def _event(text, platform=Platform.LOCAL):
    return MessageEvent(text=text, source=SessionSource(platform=platform, chat_id='local-chat'),
                        message_id='adm-1')


async def _prepare(runner, text, platform=Platform.LOCAL):
    event = _event(text, platform)
    return await runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[], session_key='key')


@pytest.fixture
def reactions_on(monkeypatch):
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, 'load_config_readonly',
                        lambda: {'display': {'message_reactions': True}})


@pytest.fixture
def reactions_off(monkeypatch):
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, 'load_config_readonly', lambda: {'display': {}})


@pytest.mark.asyncio
async def test_local_turn_announces_an_unseen_reaction_once(monkeypatch, reactions_on):
    db = _FakeDB([{'row_id': 7, 'role': 'assistant', 'emoji': '👍', 'text': 'the build passed'}])
    runner = _runner(monkeypatch, db)

    prepared = await _prepare(runner, 'and then?')

    assert prepared.startswith('[The user reacted 👍 to your message: "the build passed"]')
    assert prepared.endswith('and then?')
    assert db.calls == [('sid-1', 'user')]
    # Announced exactly once: the next turn has nothing left to say.
    assert await _prepare(runner, 'thanks') == 'thanks'


@pytest.mark.asyncio
async def test_reaction_note_is_gated_before_the_read(monkeypatch, reactions_off):
    db = _FakeDB([{'row_id': 7, 'role': 'assistant', 'emoji': '👍', 'text': 'the build passed'}])
    runner = _runner(monkeypatch, db)

    assert await _prepare(runner, 'and then?') == 'and then?'
    # A disabled announcement must not consume what it does not show.
    assert db.calls == []


@pytest.mark.asyncio
async def test_messaging_turns_leave_reactions_to_their_adapter(monkeypatch, reactions_on):
    db = _FakeDB([{'row_id': 7, 'role': 'assistant', 'emoji': '👍', 'text': 'the build passed'}])
    runner = _runner(monkeypatch, db)

    prepared = await _prepare(runner, 'and then?', platform=Platform.TELEGRAM)

    assert prepared == 'and then?'
    assert db.calls == []


@pytest.mark.asyncio
async def test_an_unreadable_store_never_breaks_the_turn(monkeypatch, reactions_on):
    class _Broken(_FakeDB):
        async def take_unseen_reactions(self, session_id, *, author="user"):
            raise RuntimeError('no such table: messages')

    runner = _runner(monkeypatch, _Broken([]))

    assert await _prepare(runner, 'and then?') == 'and then?'


@pytest.mark.asyncio
async def test_a_real_store_reaction_reaches_the_turn_once(monkeypatch, reactions_on, tmp_path):
    """The note is read from the SESSION ID the runner resolves — the same lineage lookup the TUI
    lane relies on — not from the routing key (which owns no message rows)."""
    from hermes_state import AsyncSessionDB, SessionDB

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('sess-react', source='local')
    row_id = db.append_message('sess-react', 'assistant', 'the build passed')
    assert db.set_message_reaction('sess-react', row_id, '👍', author='user') is not None

    runner = _runner(monkeypatch, AsyncSessionDB(db), session_id='sess-react')
    prepared = await _prepare(runner, 'and then?')
    assert prepared.startswith('[The user reacted 👍 to your message: "the build passed"]')
    # The row is stamped seen on read, so the next turn announces nothing.
    assert await _prepare(runner, 'and then?') == 'and then?'
    db.close()
