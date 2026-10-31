"""A CLI that only *viewed* a messaging session must not end the channel's conversation.

Regression for #147071. ``hermes chat -r <session>`` on a channel-owned row followed by a clean
exit stamped ``cli_close`` on that row; the reason is outside the gateway's reopen whitelist
(``hermes_state_common._RECOVERABLE_END_REASONS``), so the channel's stale-route self-heal could
not recover it and the next inbound message minted a brand-new empty session, orphaning the
conversation. The CLI still ends the sessions it owns.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_state import SessionDB

CHANNEL_KEY = "feishu:p2p:u1"


@pytest.fixture
def cli_mod():
    import cli as cli_mod
    cli_mod._cleanup_done = False
    cli_mod._handed_off_session_ids.clear()
    cli_mod._single_query_finalize_attempted_session_ids.clear()
    yield cli_mod
    cli_mod._active_agent_ref = None
    cli_mod._cleanup_done = False


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def _channel_session(db, session_id="feishu-session"):
    db.create_session(session_id, "feishu", session_key=CHANNEL_KEY, chat_id="chat-1",
                      chat_type="p2p", user_id="u1")
    db.append_message(session_id, "user", content="hello from feishu")
    db.append_message(session_id, "assistant", content="hi")
    return session_id


def _resume_then_quit(cli_mod, store, session_id):
    """The classic CLI attached with ``chat -r``: a local agent, no gateway ownership."""
    cli = cli_mod.HermesCLI()
    cli.agent = None
    cli.session_id = session_id
    cli._gateway_session_open = True
    cli._session_db = store
    with patch.object(cli_mod, "_run_cleanup"), patch.object(cli, "_print_exit_summary"), \
            patch.object(cli, "_release_active_session"):
        cli._tui_shutdown()


def test_cli_exit_leaves_a_channel_session_recoverable(cli_mod, db):
    session_id = _channel_session(db)

    _resume_then_quit(cli_mod, db, session_id)

    assert db.get_session(session_id)["ended_at"] is None
    recovered = db.find_latest_gateway_session_for_peer(source="feishu", session_key=CHANNEL_KEY)
    assert recovered is not None and recovered["id"] == session_id


def test_cli_exit_still_closes_a_cli_born_session(cli_mod, db):
    db.create_session("cli-session", "cli")
    db.append_message("cli-session", "user", content="hello")

    _resume_then_quit(cli_mod, db, "cli-session")

    assert db.get_session("cli-session")["end_reason"] == "cli_close"


def test_oneshot_run_on_a_channel_session_leaves_it_recoverable(cli_mod, db):
    """The one-shot exit (``chat -r … -q``) is a second stamp site for the same reason."""
    from hermes_cli.cli_shutdown import _flush_one_shot_session_store

    session_id = _channel_session(db, "feishu-oneshot")
    agent = SimpleNamespace(session_id=session_id, _session_db=db, _session_messages=[])
    _flush_one_shot_session_store(SimpleNamespace(
        agent=agent, session_id=session_id, conversation_history=[], _session_db=db))

    assert db.get_session(session_id)["ended_at"] is None
