"""``kanban_create`` can type the park it was asked for (#141271).

``initial_status="blocked"`` used to land ``block_kind`` NULL, so needs_input queues, notifier
wording and the block-recurrence accounting read a deliberately parked card as untyped — and the
only repair, ``block_task(kind=...)``, is refused to a dispatcher-owned worker on a card that is
not its own. These tests pin the creating-transaction contract on the storage, CLI and tool
surfaces, and that the untyped park stays the default.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty board, and no ambient worker identity."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return tmp_path


def _rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _blocked_payload(conn, task_id) -> dict:
    events = [e for e in kb.list_events(conn, task_id) if e.kind == "blocked"]
    assert len(events) == 1, events
    assert events[0].payload is not None
    return events[0].payload


def test_initial_block_kind_types_the_park(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="decision needed", initial_status="blocked",
            initial_block_kind="needs_input")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.block_recurrences == 1
        assert _blocked_payload(conn, tid)["kind"] == "needs_input"


def test_initial_block_kind_is_optional_and_defaults_to_untyped(board):
    """Today's park: no kind written, so no consumer can read one."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="opaque park", initial_status="blocked")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.block_kind is None
        assert task.block_recurrences == 0
        payload = _blocked_payload(conn, tid)
        assert "kind" not in payload
        assert payload["reason"] == "initial_status"


@pytest.mark.parametrize("status, kind, match", [
    ("running", "needs_input", "requires initial_status='blocked'"),
    ("blocked", "dependency", "initial_block_kind must be one of"),
    ("blocked", "bogus", "initial_block_kind must be one of"),
])
def test_initial_block_kind_rejects_an_impossible_park(board, status, kind, match):
    """A refusal must leave no card behind: the validation runs inside the creating txn."""
    with kbc.connect_closing() as conn:
        before = _rows(conn)
        with pytest.raises(ValueError, match=match):
            kb.create_task(conn, title="never lands", initial_status=status,
                           initial_block_kind=kind)
        assert _rows(conn) == before


def test_initial_block_kind_is_refused_on_a_triage_card(board):
    """``triage=True`` also lands outside ``blocked``: the kind must not be dropped silently."""
    with kbc.connect_closing() as conn:
        before = _rows(conn)
        with pytest.raises(ValueError, match="requires initial_status='blocked'"):
            kb.create_task(conn, title="never lands", triage=True,
                           initial_block_kind="needs_input")
        assert _rows(conn) == before


def test_cli_flag_reaches_storage(board, capsys):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args([
        "kanban", "create", "decision card", "--assignee", "default",
        "--initial-status", "blocked", "--initial-block-kind", "capability", "--json",
    ])
    assert args.initial_block_kind == "capability"
    assert kanban_command(args) == 0
    created = json.loads(capsys.readouterr().out)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, created["id"])
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "capability"


def test_tool_create_forwards_initial_block_kind(board):
    from tools import kanban_tools as kt

    assert "initial_block_kind" in kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    out = json.loads(kt._handle_create({
        "title": "ask the human", "assignee": "default",
        "initial_status": "blocked", "initial_block_kind": "needs_input",
    }))
    assert out.get("ok"), out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, out["task_id"])
    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
