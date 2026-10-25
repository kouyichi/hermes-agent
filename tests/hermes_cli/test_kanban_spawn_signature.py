"""Regression tests for the spawn-callback signature fallback in the kanban dispatcher (#144200).

``_call_spawn_fn`` exists for back-compat: a ``spawn_fn`` written against the older
``(task, workspace)`` shape must keep working while one that accepts ``board`` gets it.
Its single ``try`` used to wrap the *callback call* as well as the signature lookup, so a
callback that recorded a side effect and then raised ``TypeError``/``ValueError`` was
invoked a SECOND time without ``board`` — one dispatch, two spawns. Only
``RuntimeError`` escaped, which is why the defect looks selective.

These tests pin the properties that make the fallback safe:

* one invocation per dispatch, whatever the callback raises from its own body;
* the callback's error reaches the dispatcher (it books the failure) instead of being
  swallowed and turned into a second attempt;
* every documented shape that must keep working still works — ``(task, workspace)``,
  ``board`` as positional-or-keyword / keyword-only / a ``**board`` var-keyword, a
  positional-only ``board`` (which keeps the two-argument fallback), and callables whose
  signature cannot be inspected at all.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch import _call_spawn_fn

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home: Path):
    with kbc.connect() as connection:
        yield connection


def _task() -> Task:
    return cast("Task", SimpleNamespace(id="t1"))


# ---------------------------------------------------------------------------
# One invocation per dispatch, even when the body raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_type", [TypeError, ValueError])
def test_callback_body_error_is_not_retried(error_type: type[Exception]) -> None:
    """A ``board``-accepting callback that fails in its own body runs ONCE.

    The failure carries no information about the signature, and the fallback cannot
    distinguish it from a bad keyword call — so it must never re-invoke the callback.
    """
    calls: list[tuple] = []

    def spawn(task, workspace, board=None):
        calls.append((task.id, workspace, board))
        raise error_type("after-effect")

    with pytest.raises(error_type):
        _call_spawn_fn(spawn, _task(), "/ws", "default")

    assert len(calls) == 1, "the callback must not be invoked a second time to retry its own error"


def test_callback_body_error_other_than_signature_error_still_single_call() -> None:
    """Control: a non-signature error already behaved; keep it pinned."""
    calls: list[tuple] = []

    def spawn(task, workspace, board=None):
        calls.append((task.id, board))
        raise RuntimeError("after-effect")

    with pytest.raises(RuntimeError):
        _call_spawn_fn(spawn, _task(), "/ws", "default")

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Shapes that must keep working
# ---------------------------------------------------------------------------


def test_board_is_passed_when_positional_or_keyword() -> None:
    seen: list[str | None] = []

    def spawn(task, workspace, board=None):
        seen.append(board)
        return 4242

    assert _call_spawn_fn(spawn, _task(), "/ws", "board-a") == 4242
    assert seen == ["board-a"]


def test_board_is_passed_when_keyword_only() -> None:
    seen: list[str | None] = []

    def spawn(task, workspace, *, board=None):
        seen.append(board)
        return 7

    assert _call_spawn_fn(spawn, _task(), "/ws", "board-b") == 7
    assert seen == ["board-b"]


def test_board_is_passed_through_a_var_keyword_named_board() -> None:
    seen: list[str | None] = []

    def spawn(task, workspace, **board):
        seen.append(board.get("board"))
        return 8

    assert _call_spawn_fn(spawn, _task(), "/ws", "board-c") == 8
    assert seen == ["board-c"]


def test_board_is_passed_to_a_generic_var_keyword_callback() -> None:
    """``bind`` accepts the keyword for a ``**kwargs`` callable, so it gets the board."""
    seen: list[dict] = []

    def spawn(task, workspace, **kwargs):
        seen.append(kwargs)
        return 9

    assert _call_spawn_fn(spawn, _task(), "/ws", "board-c2") == 9
    assert seen == [{"board": "board-c2"}]


def test_two_argument_callback_keeps_working() -> None:
    seen: list[tuple] = []

    def spawn(task, workspace):
        seen.append((task.id, workspace))
        return 11

    assert _call_spawn_fn(spawn, _task(), "/ws", "board-d") == 11
    assert seen == [("t1", "/ws")]


def test_positional_only_board_keeps_the_two_argument_fallback() -> None:
    """A positional-only ``board`` cannot take a keyword, so the older shape is used.

    The slot is never guessed: ``board`` is the third parameter by contract, but a
    positional-only declaration does not promise that order, so the callable is handed
    the two-argument form it was written to tolerate rather than a silently misbound
    third argument.
    """
    seen: list[tuple] = []

    def spawn(task, workspace, board=None, /):
        seen.append((task.id, workspace, board))
        return 12

    assert _call_spawn_fn(spawn, _task(), "/ws", "board-e") == 12
    assert seen == [("t1", "/ws", None)], "a positional-only board keeps the back-compat call"


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_uninspectable_callable_uses_two_arguments(error_type: type[Exception]) -> None:
    """Callables without an inspectable signature keep the two-argument call."""
    seen: list[tuple] = []

    class Opaque:
        def __call__(self, task, workspace):
            seen.append((task.id, workspace))
            return 13

        @property
        def __signature__(self):
            raise error_type("no signature available")

    assert _call_spawn_fn(Opaque(), _task(), "/ws", "board-f") == 13
    assert seen == [("t1", "/ws")]


# ---------------------------------------------------------------------------
# Through the real dispatcher: one spawn, one booked failure
# ---------------------------------------------------------------------------


def test_dispatch_books_one_failure_for_a_failing_spawn(conn, all_assignees_spawnable) -> None:
    """A spawn that fails after starting must spend one retry, not two spawns."""
    task_id = kb.create_task(conn, title="t", assignee="alice")
    spawns: list[str] = []

    def spawn(task, workspace, board=None):
        spawns.append(task.id)
        raise TypeError("after-effect")

    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert spawns == [task_id], "one dispatch must invoke the spawn callback exactly once"
    assert task_id not in [row[0] for row in result.spawned]
    row = conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["consecutive_failures"] == 1
    assert row["status"] == "ready", "a single failure keeps the card retryable"
