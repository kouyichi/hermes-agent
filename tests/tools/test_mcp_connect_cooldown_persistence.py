"""Regression test for #141030: an MCP connect cooldown must outlive the process that armed it.

A stdio MCP server that exits on start is never recorded in ``_servers``, so the per-server
cooldown in ``tools/mcp_tool_discovery`` is what stops every worker session from re-spawning it.
That cooldown lived only in two module dicts, so every NEW process started from failure count 0
and launched the broken server again: cron workers (one per execution), the CLI, the one-shot path
and the Desktop backend each did it, and one install logged ~98,000 identical start banners. The
cooldown is now mirrored into ``<HERMES_HOME>/state/mcp_connect_cooldown.json``, so a fresh process
honours the deadline a failing one published.

These tests drive the real helpers and the real candidate filter; nothing here spawns a server.
"""

from __future__ import annotations

import json

import pytest

import tools.mcp_tool as mcp_mod
from tools import mcp_tool_discovery as discovery


BROKEN = {"command": "/bin/false"}
FIXED = {"command": "/bin/true"}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Sandbox the published cooldown file and the in-memory tables for each test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    snapshot = (
        dict(mcp_mod._servers),
        set(mcp_mod._server_connecting),
        dict(mcp_mod._server_connect_errors),
        dict(mcp_mod._server_connect_retry_after),
        dict(mcp_mod._server_connect_failures),
    )
    mcp_mod._servers.clear()
    mcp_mod._server_connecting.clear()
    mcp_mod._server_connect_errors.clear()
    mcp_mod._server_connect_retry_after.clear()
    mcp_mod._server_connect_failures.clear()
    try:
        yield tmp_path
    finally:
        (servers, connecting, errs, retry_after, failures) = snapshot
        mcp_mod._servers.clear(); mcp_mod._servers.update(servers)
        mcp_mod._server_connecting.clear(); mcp_mod._server_connecting.update(connecting)
        mcp_mod._server_connect_errors.clear(); mcp_mod._server_connect_errors.update(errs)
        mcp_mod._server_connect_retry_after.clear(); mcp_mod._server_connect_retry_after.update(retry_after)
        mcp_mod._server_connect_failures.clear(); mcp_mod._server_connect_failures.update(failures)


def _new_process() -> None:
    """Drop the in-process cooldown tables, as a freshly spawned Hermes process would have them."""
    mcp_mod._server_connect_retry_after.clear()
    mcp_mod._server_connect_failures.clear()


def _cooldown_file(home):
    return home / "state" / "mcp_connect_cooldown.json"


def test_cooldown_survives_a_new_process(_isolated_state):
    discovery._record_connect_failure("broken")
    path = _cooldown_file(_isolated_state)
    assert path.exists(), "the failure was not published for other processes"

    _new_process()

    assert discovery._connect_cooldown_active("broken") is True


def test_a_successful_connect_clears_the_published_cooldown(_isolated_state):
    discovery._record_connect_failure("broken")
    _new_process()

    discovery._clear_connect_failure("broken")
    _new_process()

    assert discovery._connect_cooldown_active("broken") is False


def test_a_config_change_bypasses_the_published_cooldown(_isolated_state):
    discovery._record_connect_failure("broken", config=BROKEN)
    _new_process()

    assert discovery._connect_cooldown_active("broken", config=BROKEN) is True
    assert discovery._connect_cooldown_active("broken", config=FIXED) is False


def test_the_published_cooldown_expires(_isolated_state, monkeypatch):
    monkeypatch.setattr("tools.mcp_connect_cooldown.time.time", lambda: 1_000.0)
    discovery._record_connect_failure("broken")
    _new_process()

    monkeypatch.setattr("tools.mcp_connect_cooldown.time.time",
                        lambda: 1_000.0 + mcp_mod._CONNECT_RETRY_BASE_BACKOFF_SEC + 1.0)

    assert discovery._connect_cooldown_active("broken") is False


def test_failures_accumulate_across_processes(_isolated_state, monkeypatch):
    """Each new process used to restart the backoff ladder at 30 s; the published count continues it."""
    monkeypatch.setattr("tools.mcp_connect_cooldown.time.time", lambda: 1_000.0)
    discovery._record_connect_failure("broken")
    _new_process()
    discovery._record_connect_failure("broken")

    assert mcp_mod._server_connect_failures["broken"] == 2, "a new process restarted the ladder"
    payload = json.loads(_cooldown_file(_isolated_state).read_text())
    assert payload["servers"]["broken"]["failures"] == 2
    assert payload["servers"]["broken"]["retry_after"] == 1_000.0 + mcp_mod._CONNECT_RETRY_BASE_BACKOFF_SEC * 2


def test_a_corrupt_state_file_is_ignored(_isolated_state):
    path = _cooldown_file(_isolated_state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    assert discovery._connect_cooldown_active("broken") is False
    discovery._record_connect_failure("broken")
    _new_process()
    assert discovery._connect_cooldown_active("broken") is True


def test_discovery_candidates_skip_a_published_cooldown(_isolated_state):
    discovery._record_connect_failure("broken")
    _new_process()

    candidates = discovery._select_new_servers({"broken": dict(BROKEN), "healthy": {"command": "mcp-ok"}})

    assert "broken" not in candidates
    assert "healthy" in candidates
