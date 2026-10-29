"""A session row with no model renders as an unknown model, not as JSON ``null``.

``sessions.model`` stays NULL for a row that never ran a model turn (a route reservation, or a
``/status`` slash command in a channel), and the gateway still mounts such a row into the live
registry. The TUI switcher's row renderer called ``.replace`` on the value it read, so one NULL
model took the whole Ink tree down when the picker opened (#146537).

Both canonical feeds now answer ``''`` — the contract ``session_active_list.py`` already kept — so
no consumer receives a ``null`` that a picker column cannot print.
"""
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def listing(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import LiveSession, initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    db.create_session('live-null', source='discord')  # never ran a model turn
    db.create_session('stored-null', source='cli')
    db.append_message('stored-null', 'user', 'question')
    db.end_session('stored-null', 'cli_exit')
    assert db.get_session('live-null')['model'] is None, 'fixture must keep the model column NULL'
    assert db.get_session('stored-null')['model'] is None, 'fixture must keep the model column NULL'
    runner = SimpleNamespace(_session_db=db, session_store=store, _draining=False,
                             _handle_message=None, _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id='null-model', instance_id='owner')
    authority.sessions['live-null'] = LiveSession(SessionSource(platform=Platform.DISCORD, chat_id='live-null'), 'live-null')
    connection = AuthorityConnection(authority, object(), {'provider': 'local', 'user_id': 'native',
                                                           'profile_id': 'null-model', 'instance_id': 'owner'})

    async def rows(scope):
        reply = await connection.dispatch({'id': 1, 'method': 'session.list', 'params': {'scope': scope}})
        assert 'result' in reply, reply
        return {row['id']: row for row in reply['result']['sessions']}

    return rows


@pytest.mark.asyncio
async def test_a_null_model_live_row_lists_an_empty_model(listing):
    """The live feed is what `Ctrl+X` renders: its rows must carry a printable model."""
    assert (await listing('live'))['live-null']['model'] == ''


@pytest.mark.asyncio
async def test_a_null_model_stored_row_lists_an_empty_model(listing):
    """The stored feed is the resumable half of the same picker, and it disagreed with the live one."""
    assert (await listing('stored'))['stored-null']['model'] == ''
