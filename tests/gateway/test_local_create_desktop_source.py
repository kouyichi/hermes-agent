"""``--source desktop`` names the gui surface, so a create that passes it is admitted like ``--source gui``.

The classic CLI sends ``--source`` verbatim as ``session.create``'s ``source``: ``main`` exports it as
``HERMES_SESSION_SOURCE`` and ``GatewaySessionStore.create`` reads it back. Before the single local
gateway owned local sessions that label was free-form row metadata; on main it reaches
``build_policy``, whose closed surface set knows ``gui`` but not the Desktop's own name, so every
script opening a Desktop-sidebar chat (``hermes chat --source desktop -Q``) failed with a bare
``invalid_params`` while the Desktop app itself keeps stamping ``created_source='desktop'`` on its own
rows (#142991). ``SURFACE_GROUPS`` already pairs the two names — ``sessions list --source desktop``
lists a ``gui`` row — so the create side is what has to resolve the pair.

The harness below is ``tests/gateway/test_local_source_labels.py``'s (copied, not imported: the health
gate reports an imported fixture re-bound as a parameter, and these cases need the canonical authority
call path rather than a stubbed policy).
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection


async def _authority(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway import run

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {'default': 'fixture'}, 'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(run, '_resolve_gateway_model', lambda config: 'fixture')
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = run.GatewayRunner.__new__(run.GatewayRunner)
    runner.adapters, runner.session_store, runner._session_db, runner._draining = {}, store, store._db, False
    authority = await initialize_session_authority(runner, profile_id=str(Path(store._db.db_path).parent), instance_id='test')
    authority._schedule = lambda ref: None
    return authority, store


async def _dispatch(conn, method, params, request_id='probe'):
    return await conn.dispatch({'id': request_id, 'method': method, 'params': params})


async def _create_frame(conn, authority, source, request_id):
    conn_cwd = Path(authority.db.db_path).parent
    return await _dispatch(conn, 'session.create',
                           {'request_id': request_id, 'source': source, 'cwd': str(conn_cwd)}, request_id)


@pytest.mark.asyncio
async def test_source_desktop_creates_the_desktops_own_row(tmp_path, monkeypatch):
    """``created_source='desktop'`` on a local row is what the Desktop sidebar lists: the label the
    create named resolves to the gui surface, whose stored surface IS the Desktop's."""
    authority, store = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    created = await _create_frame(conn, authority, 'desktop', 'desk')
    assert 'error' not in created, created
    sid = created['result']['stored_session_id']
    live = authority.sessions[sid]
    store._record_gateway_session_peer(sid, live.route, live.source)
    row = authority.db.get_session(sid)
    assert row['created_source'] == 'desktop'
    assert row['source'] in {'gui', 'local'}
    assert authority.sessions[sid].source.platform.value == 'local'
    # ... and the Desktop's own rows are the human-facing ones: the picker lists it.
    listed = await _dispatch(conn, 'session.list', {'limit': 5}, 'list')
    assert sid in [item['id'] for item in listed['result']['sessions']]
    await conn.close()
    authority.db.close()


def test_desktop_builds_the_policy_gui_builds():
    """Same surface, same default toolsets: the pair is one conversation, not a new surface."""
    import tempfile

    from gateway.session_policy import build_policy

    cwd = tempfile.mkdtemp()
    desktop = build_policy({'source': 'desktop', 'cwd': cwd}, {})
    gui = build_policy({'source': 'gui', 'cwd': cwd}, {})
    assert (desktop.source, desktop.platform, desktop.toolsets) == (gui.source, gui.platform, gui.toolsets)
    assert desktop.platform == 'desktop'
    assert 'desktop_ui' in desktop.toolsets


@pytest.mark.asyncio
async def test_describe_advertises_the_paired_name(tmp_path, monkeypatch):
    """The thin client refuses a source ``runtime.describe`` does not list, so the pair has to be
    advertised as well as accepted."""
    authority, _store = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    described = await _dispatch(conn, 'runtime.describe', {})
    sources = described['result']['session_create']['sources']
    assert {'desktop', 'gui'} <= set(sources)
    await conn.close()
    authority.db.close()


@pytest.mark.asyncio
async def test_an_unrelated_tag_is_still_refused_by_name(tmp_path, monkeypatch):
    """Resolving the pair must not turn the surface set open: a tag that names no surface keeps the
    named refusal, pointing at the source field (#142895 is the free-form-tag decision, not this)."""
    authority, _store = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    refused = await _create_frame(conn, authority, 'mytag', 'tag')
    assert refused['error']['message'] == 'invalid_params'
    assert refused['error']['data']['reason'] == 'invalid_params'
    assert refused['error']['data']['fields'] == ['source']
    await conn.close()
    authority.db.close()
