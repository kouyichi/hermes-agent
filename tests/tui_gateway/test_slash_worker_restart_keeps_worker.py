"""A failed slash-worker restart must keep the session's live worker (#147104).

``_restart_slash_worker`` rebuilds the per-session slash-command worker after a model switch, a
compression rotation and ``/new``. It closed the healthy worker first and built the replacement
afterwards, so a constructor failure (thread/file-descriptor pressure, an unscoped secret, a
provider/model lookup) left the session with ``slash_worker = None`` and nothing logged: every
later ``/`` command in that session silently found nothing to route to, and only a session restart
brought the capability back.

The replacement is built first and swapped in only on success; a failure keeps the live worker and
is logged with its traceback.
"""

import logging
import types

import pytest

from tui_gateway import server


class _RecordingWorker:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.closed = 0

    def close(self):
        self.closed += 1


class _BoomyWorker(_RecordingWorker):
    """Stands in for a constructor that raises (fd/thread pressure, secret scope, provider lookup)."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("can't start new thread")


@pytest.fixture(autouse=True)
def _isolated_restart_session(monkeypatch):
    """Keep the session registry clean: ``_restart_slash_worker`` stores through ``_attach_worker``,
    which consults ``server._sessions`` to decide whether the fresh worker is still owned."""
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    server._sessions.pop("restart-fail", None)
    server._sessions.pop("restart-ok", None)
    yield
    server._sessions.pop("restart-fail", None)
    server._sessions.pop("restart-ok", None)


def _session_with(worker):
    return {
        "session_key": "k",
        "agent": types.SimpleNamespace(model="test-model", provider="test-provider"),
        "slash_worker": worker,
    }


def test_failed_replacement_keeps_the_live_worker(monkeypatch):
    monkeypatch.setattr(server, "_SlashWorker", _BoomyWorker)
    live_worker = _RecordingWorker()
    session = _session_with(live_worker)
    server._sessions["restart-fail"] = session

    server._restart_slash_worker("restart-fail", session)

    assert session["slash_worker"] is live_worker
    assert live_worker.closed == 0


def test_failed_replacement_is_logged_with_its_traceback(monkeypatch, caplog):
    monkeypatch.setattr(server, "_SlashWorker", _BoomyWorker)
    session = _session_with(_RecordingWorker())
    server._sessions["restart-fail"] = session

    with caplog.at_level(logging.WARNING, logger="tui_gateway.server"):
        server._restart_slash_worker("restart-fail", session)

    records = [r for r in caplog.records if "slash" in r.getMessage().lower()]
    assert records, f"a failed slash-worker restart must say so, got: {caplog.text!r}"
    assert records[-1].levelno == logging.WARNING
    assert records[-1].exc_info is not None, "the failure must carry its traceback"


def test_successful_replacement_closes_the_old_worker_once(monkeypatch):
    monkeypatch.setattr(server, "_SlashWorker", _RecordingWorker)
    old_worker = _RecordingWorker()
    session = _session_with(old_worker)
    server._sessions["restart-ok"] = session

    server._restart_slash_worker("restart-ok", session)

    assert isinstance(session["slash_worker"], _RecordingWorker)
    assert session["slash_worker"] is not old_worker
    assert old_worker.closed == 1


def test_replacement_is_constructed_before_the_live_worker_is_closed(monkeypatch):
    """The ordering IS the fix: a raiser must not be reached with the old worker already gone."""
    closed_at_construction = []

    class _OrderProbe(_RecordingWorker):
        def __init__(self, *args, **kwargs):
            closed_at_construction.append(old_worker.closed)
            super().__init__(*args, **kwargs)

    old_worker = _RecordingWorker()
    monkeypatch.setattr(server, "_SlashWorker", _OrderProbe)
    session = _session_with(old_worker)
    server._sessions["restart-ok"] = session

    server._restart_slash_worker("restart-ok", session)

    assert closed_at_construction == [0]
    assert old_worker.closed == 1
