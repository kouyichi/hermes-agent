"""The activity→board liveness bridge must never park the agent loop on a busy board.

``_touch_activity`` mirrors runtime liveness onto the kanban board (#31752) from the
turn thread — including the streaming monitor's 30s heartbeat — so the dispatcher
watchdog sees an active worker. The bridge is best-effort by contract ("never raise
into the agent loop"), but a board held by any long writer used to park the caller for
the whole busy-timeout ladder (120s x 6 ≈ 720s at the stock settings), which is how a
worker turn ended up aborted by the turn-liveness watchdog while the stream stale
watchdog was never allowed to evaluate its threshold (#145886).

A missed heartbeat is harmless — the next 60s tick writes again — so the liveness
write caps its own wait instead of inheriting the interactive/claim ladder.

Verifies: a write-locked board answers the bridge in well under the configured
ladder, the liveness connection really carries the short timeout, and a free board
still lands the heartbeat through it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
import tools.kanban_tools as kt


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kt._auto_heartbeat_last_attempt = 0.0
    kt._auto_heartbeat_fence_warned = False
    return home


def _new_task(title: str) -> str:
    """A claimed (``running``) task: the board's heartbeat only writes for its owner."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=title)
        assert kb.claim_task(conn, tid, claimer="liveness-test") is not None
        return tid
    finally:
        conn.close()


def _unthrottle() -> None:
    kt._auto_heartbeat_last_attempt = 0.0


def _liveness_timeout_helper():
    helper = getattr(kt, "_set_liveness_busy_timeout", None)
    assert helper is not None, (
        "the auto-heartbeat connection needs its own bounded busy timeout; without one a "
        "write-locked board parks the call for the whole interactive ladder"
    )
    return helper


def test_heartbeat_gives_up_quickly_on_a_write_locked_board(worker_home, monkeypatch):
    tid = _new_task("busy board")
    # The scaffolded ladder (6 x 2s) is ~12s; the liveness bridge owns a much shorter one.
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "2000")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    _unthrottle()

    holder = kbc.connect()
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert kt.heartbeat_current_worker_from_env() is False
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert elapsed < 5.0, f"the liveness write waited {elapsed:.1f}s on a write-locked board"


def test_liveness_connection_carries_its_own_bounded_timeout(worker_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "120000")
    conn = kbc.connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 120000
        _liveness_timeout_helper()(conn)
        bounded = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert bounded == kt._LIVENESS_BUSY_TIMEOUT_MS
        assert bounded < 5000
    finally:
        conn.close()


def test_a_free_board_still_writes_through_the_short_wait(worker_home, monkeypatch):
    tid = _new_task("free board")
    conn = kbc.connect()
    try:
        _liveness_timeout_helper()(conn)
        assert kbd.heartbeat_worker(conn, tid, note=None) is True
        task = kb.get_task(conn, tid)
        assert task is not None and task.last_heartbeat_at is not None
    finally:
        conn.close()
