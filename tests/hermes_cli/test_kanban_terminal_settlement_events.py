"""Regression tests for #146973 — a settlement that lands on a finished card must write nothing.

``agent/turn_finalizer.py::_record_kanban_budget_exhausted`` settles an exhausted iteration budget
through ``_record_task_failure(outcome="timed_out", release_claim=True, end_run=True)``. Both of that
helper's status writes are CAS-guarded on a live status, so a card that already reached ``done``
keeps its state — but the settlement still appended a ``timed_out`` event carrying ``failures: 1``
and ``retry_status: "ready"`` for work that had already merged, because ``complete_task`` had closed
the run and the event write was not behind any guard. Operators read that event as "the finished
task failed" and re-open the work.

The dispatcher's own wall-clock timeout is structurally immune: it only appends its ``timed_out``
event when its ``UPDATE ... WHERE status = 'running'`` matched a row. These tests pin the same
property on the settlement leg, and keep the live-card behaviour (retry restored, failure recorded)
unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

BUDGET_ERROR = "Iteration budget exhausted (150/150) — task could not complete within the allowed iterations"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _settle_budget(conn, task_id: str, *, failure_limit: int | None = None) -> bool:
    """The settlement shape ``_record_kanban_budget_exhausted`` uses."""
    return kbd._record_task_failure(
        conn,
        task_id,
        error=BUDGET_ERROR,
        outcome="timed_out",
        failure_limit=failure_limit,
        release_claim=True,
        end_run=True,
        event_payload_extra={"budget_used": 150, "budget_max": 150},
    )


def _failures(conn, task_id: str) -> int:
    return int(conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0])


def _event_kinds(conn, task_id: str) -> list[str]:
    return [event.kind for event in kb.list_events(conn, task_id)]


def test_budget_settlement_after_completion_leaves_the_card_untouched(kanban_home: Path) -> None:
    """The reported shape: the budget runs out while the worker is reporting a merged card."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="already merged", assignee="dev")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, result="merged in PR #1", force=True)
        completed = kb.get_task(conn, task_id)
        assert completed is not None and completed.status == "done"
        events_before = _event_kinds(conn, task_id)

        assert _settle_budget(conn, task_id) is False

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "done"
        assert _event_kinds(conn, task_id) == events_before, (
            "a settlement that lands after the card completed must not append a failure event"
        )
        assert _failures(conn, task_id) == 0, (
            "a completed card's failure counter must stay cleared"
        )


def test_budget_settlement_on_a_tripped_done_card_writes_no_gave_up(kanban_home: Path) -> None:
    """The trip half: an already-terminal card must not get a ``gave_up`` either."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="merged and archived next", assignee="dev")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, result="done", force=True)
        events_before = _event_kinds(conn, task_id)

        assert _settle_budget(conn, task_id, failure_limit=1) is False

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "done"
        assert _event_kinds(conn, task_id) == events_before
        assert "gave_up" not in _event_kinds(conn, task_id)


def test_budget_settlement_on_an_archived_card_leaves_the_card_untouched(kanban_home: Path) -> None:
    """``archived`` is terminal for dependency gating, so it is terminal here too."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="archived after merge", assignee="dev")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, result="done", force=True)
        assert kb.archive_task(conn, task_id) is True
        archived = kb.get_task(conn, task_id)
        assert archived is not None and archived.status == "archived"
        events_before = _event_kinds(conn, task_id)

        assert _settle_budget(conn, task_id) is False

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "archived"
        assert _event_kinds(conn, task_id) == events_before


def test_budget_settlement_on_a_live_card_still_records_the_timeout(kanban_home: Path) -> None:
    """Control: the same settlement on a genuinely running card keeps today's behaviour.

    Without this, "write nothing" would pass by disabling the settlement outright.
    """
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="still working", assignee="dev")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        running = kb.get_task(conn, task_id)
        assert running is not None and running.status == "running"

        assert _settle_budget(conn, task_id) is False

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready", (
            "the retry status is restored so the card can run again"
        )
        timed_out = [event for event in kb.list_events(conn, task_id) if event.kind == "timed_out"]
        assert len(timed_out) == 1
        assert timed_out[0].payload is not None
        assert timed_out[0].payload["error"] == BUDGET_ERROR
        assert timed_out[0].payload["failures"] == 1
        assert timed_out[0].payload["retry_status"] == "ready"
        assert timed_out[0].run_id == claimed.current_run_id, (
            "the event belongs to the run it closed"
        )
        assert _failures(conn, task_id) == 1
