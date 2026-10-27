"""A stale reap ticks the unified breaker only when its run never heartbeated (issue #145676).

``detect_stale_running`` reclaims a ``running`` card whose worker stopped reporting. For a worker
that heartbeated and then wedged that reclaim is benign and must stay counter-neutral — otherwise a
legitimately long task trips the breaker on its own patience. The other shape is a worker that dies
BEFORE its first heartbeat: nothing about the card ran, the failure repeats deterministically (an
endpoint down, a dead credential, a poisoned prompt), and with no accounting the dispatcher loops
spawn -> stale reap -> requeue forever, unbounded by ``kanban.failure_limit`` and by a task's
``max_retries``.

These drive the real reaper over a real store: the incident's two laps park the card at the
configured limit, the streak survives a dispatcher restart (it is a DB column, not process state),
and a heartbeating long-runner keeps today's reclaim without being counted.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch import _STALE_HEARTBEAT_GAP_SECONDS

STALE_TIMEOUT = 1
FAILURE_LIMIT = 2

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "kanban.db"


@pytest.fixture
def conn(db_path: Path):
    db = kbc.connect(db_path)
    try:
        yield db
    finally:
        db.close()


def _claim_stale(conn, task_id: str, *, heartbeat_age: int | None = None) -> int:
    """Claim *task_id* and age the new run past the stale timeout.

    ``heartbeat_age=None`` is the reported shape: the claim exists, the run started hours ago, and
    ``last_heartbeat_at`` is still NULL because the worker never reached its first progress event.
    """
    task = kb.claim_task(conn, task_id, claimer="builder:test")
    assert task is not None, "the card must be claimable for another lap"
    run_id = task.current_run_id
    assert run_id is not None
    started = int(time.time()) - 1_000
    heartbeat = None if heartbeat_age is None else int(time.time()) - heartbeat_age
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (started, heartbeat, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (started, heartbeat, int(run_id)),
        )
    return int(run_id)


def _reap(conn) -> list[str]:
    """One dispatcher reap pass. No ``failure_limit``: the default (2) is the documented breaker
    threshold, so the sabotage run fails on the counter, not on an unknown keyword."""
    return kbd.detect_stale_running(conn, stale_timeout_seconds=STALE_TIMEOUT)


def _row(conn, task_id: str):
    return conn.execute(
        "SELECT status, consecutive_failures, last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def test_no_heartbeat_laps_park_the_card_at_the_configured_limit(conn):
    """The reported loop: two spawn->stale->requeue laps, then the card is held for an operator."""
    task_id = kb.create_task(conn, title="endpoint-dependent card", assignee="builder")

    _claim_stale(conn, task_id)
    assert _reap(conn) == [task_id]
    first = _row(conn, task_id)
    assert first["status"] == "ready"
    assert first["consecutive_failures"] == 1
    assert first["last_failure_error"].startswith("stale-loop:")

    _claim_stale(conn, task_id)
    assert _reap(conn) == [task_id]
    parked = _row(conn, task_id)
    assert parked["status"] == "blocked"
    assert parked["consecutive_failures"] == 2
    assert "no heartbeat ever" in parked["last_failure_error"]

    # The same tick's promotion pass must not hand the card back to the dispatcher.
    assert kb.recompute_ready(conn, failure_limit=FAILURE_LIMIT) == 0
    assert _row(conn, task_id)["status"] == "blocked"

    gave_up = [e for e in kb.list_events(conn, task_id=task_id) if e.kind == "gave_up"][-1]
    assert gave_up.payload is not None
    assert gave_up.payload["trigger_outcome"] == "stale"


def test_a_heartbeating_long_runner_keeps_its_counter_neutral_reclaim(conn):
    """A worker that reported progress and then wedged is requeued, never blamed."""
    task_id = kb.create_task(conn, title="six-hour training run", assignee="builder")

    _claim_stale(conn, task_id, heartbeat_age=_STALE_HEARTBEAT_GAP_SECONDS + 600)
    assert _reap(conn) == [task_id]
    row = _row(conn, task_id)
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == 0
    assert row["last_failure_error"] is None


def test_the_stale_streak_survives_a_dispatcher_restart(db_path: Path):
    """``consecutive_failures`` is a DB column: a mid-loop restart reaches the same block."""
    conn = kbc.connect(db_path)
    try:
        task_id = kb.create_task(conn, title="endpoint-dependent card", assignee="builder")
        _claim_stale(conn, task_id)
        assert _reap(conn) == [task_id]
        assert _row(conn, task_id)["consecutive_failures"] == 1
    finally:
        conn.close()

    restarted = kbc.connect(db_path)
    try:
        assert _row(restarted, task_id)["consecutive_failures"] == 1
        _claim_stale(restarted, task_id)
        assert _reap(restarted) == [task_id]
        assert _row(restarted, task_id)["status"] == "blocked"
    finally:
        restarted.close()
