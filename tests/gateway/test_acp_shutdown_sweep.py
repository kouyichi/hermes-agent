"""The shutdown ACP sweep reads the receipt of every local session, not just the ACP ones (#145396).

``settle_gateway_runtime`` calls ``end_idle_acp_sessions`` synchronously from ``_stop_impl``, inside
the shutdown watchdog's remaining leash. The sweep listed every ``gateway.local_policy.v1:*`` receipt
and then read each one with a blocking ``local_receipt()`` only to discover ``policy.source != 'acp'``
and skip it, so a gateway with N local sessions paid N+1 serial SQLite reads on the event-loop thread
to end zero sessions. The ACP filter now rides the statement that lists the keys.

The sweep stays on the stop's own thread: the filter removes the per-non-ACP read, leaving one
statement plus one read per ACP session.
"""
import json

from types import SimpleNamespace

from gateway import session_acp_lifecycle as lifecycle
from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import begin_runtime_epoch


def _receipt(source, session_id):
    return json.dumps({'policy': {'source': source, 'profile_id': 'p'},
                       'entry': {'session_id': session_id}})


def _seed(db, key_suffix, value):
    db._execute_write(lambda conn: conn.execute(
        'INSERT OR REPLACE INTO state_meta(key, value) VALUES(?,?)', (POLICY_PREFIX + key_suffix, value)))


def _epoch(db):
    return begin_runtime_epoch(db, instance_id='shutdown-sweep-test')


def _count_reads(monkeypatch):
    """Wrap the real receipt reader: the call count is the defect, the value must stay real."""
    reads, receipt = [], lifecycle.local_receipt

    def counting(db, session_id):
        reads.append(session_id)
        return receipt(db, session_id)

    monkeypatch.setattr(lifecycle, 'local_receipt', counting)
    return reads


def test_the_shutdown_sweep_reads_only_acp_receipts(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    for suffix in ('cli-one', 'desktop-two', 'cron-three'):
        _seed(db, suffix, _receipt('cli', suffix))
    _seed(db, 'acp-four', _receipt('acp', 'acp-four'))
    reads = _count_reads(monkeypatch)
    ended = []
    monkeypatch.setattr(lifecycle, 'end_idle_local_session',
                        lambda db, **kwargs: ended.append(kwargs['session_id']) or True)

    count = lifecycle.end_idle_acp_sessions(SimpleNamespace(db=db, sessions={}, epoch=_epoch(db)))

    # One read for the one ACP receipt; the three non-ACP receipts are never opened.
    assert reads == ['acp-four']
    assert ended == ['acp-four'] and count == 1


def test_the_shutdown_sweep_survives_receipts_it_cannot_parse(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    _seed(db, 'malformed', '{"policy": ')
    _seed(db, 'not-an-object', '"acp"')
    _seed(db, 'no-policy', json.dumps({'entry': {'session_id': 'no-policy'}}))
    _seed(db, 'policy-not-an-object', json.dumps({'policy': 'acp', 'entry': {'session_id': 'x'}}))
    _seed(db, 'live-acp', _receipt('acp', 'live-acp'))
    db.create_session('live-acp', source='acp', model='fake-model', cwd=str(tmp_path))
    viewed = SimpleNamespace(sessions={'live-acp': SimpleNamespace(subscribers={'s1'})})

    # A receipt the store cannot parse is not an ACP receipt: the sweep skips it instead of raising,
    # and the session a viewer still holds is left alone.
    assert lifecycle.end_idle_acp_sessions(
        SimpleNamespace(db=db, sessions=viewed.sessions, epoch=_epoch(db))) == 0
    assert db.get_session('live-acp')['ended_at'] is None


def test_the_shutdown_sweep_still_ends_an_idle_acp_session(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('acp-1', source='acp', model='fake-model', cwd=str(tmp_path))
    _seed(db, 'acp-1', _receipt('acp', 'acp-1'))
    _seed(db, 'cli-1', _receipt('cli', 'cli-1'))
    db.create_session('cli-1', source='cli', model='fake-model', cwd=str(tmp_path))

    authority = SimpleNamespace(db=db, sessions={}, epoch=_epoch(db))
    assert lifecycle.end_idle_acp_sessions(authority) == 1

    assert db.get_session('acp-1')['end_reason'] == lifecycle.ACP_END_REASON
    assert db.get_session('cli-1')['ended_at'] is None
