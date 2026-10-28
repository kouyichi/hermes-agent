"""Delivery state the terminal tool arms on a spawned process must survive a crash.

``ProcessRegistry.spawn_*`` writes the crash-recovery checkpoint while the session still
carries its spawn-time defaults. The terminal tool then arms ``notify_on_complete`` — and,
in gateway mode, the completion watcher's routing — on the session it just got back. A
checkpoint that is never re-written after that arming recovers the process with no
completion contract, so a job that outlives an unclean gateway exit finishes without
telling anyone: exactly the case the mechanism exists for (#145825).
"""

import json

import pytest

import tools.process_registry as pr
from tools.process_registry import ProcessRegistry
from tools.terminal_tool_background import spawn_background_process


class _LocalEnv:
    """Minimal local backend: ``spawn_local`` reads only ``.env`` off it."""

    env = {}


def _entry(checkpoint, session_id):
    entries = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
    return next(item for item in entries if item["session_id"] == session_id)


@pytest.fixture()
def armed_spawn(tmp_path, monkeypatch):
    """One background process spawned through the real terminal-tool seam, notify armed."""
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    # "This session can receive a later completion": the arming path is skipped otherwise.
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr("agent.delegation_context.is_delegated_child_context", lambda: False)
    registry = ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)

    result = json.loads(spawn_background_process(
        command="sleep 20", env=_LocalEnv(), env_type="local",
        effective_task_id="bg-armed", task_id="bg-armed", session_key="", workdir=None,
        cwd=str(tmp_path), effective_pty=False, notify_on_complete=True,
        watch_patterns=None, approval_note=None, pty_disabled_reason=None,
    ))
    assert result["notify_on_complete"] is True, result
    yield registry, checkpoint, result["session_id"]
    registry.kill_process(result["session_id"])


def test_armed_notify_reaches_the_checkpoint(armed_spawn):
    """The durable record must describe the session as the caller left it, not as spawned."""
    _registry, checkpoint, session_id = armed_spawn

    assert _entry(checkpoint, session_id)["notify_on_complete"] is True


def test_recovered_session_keeps_the_armed_notify(armed_spawn, monkeypatch):
    """A gateway that dies after arming must recover the process *with* its contract."""
    _registry, _checkpoint, session_id = armed_spawn
    monkeypatch.setattr(ProcessRegistry, "_detached_host_fate", lambda self, pid, start: "running")
    recovery = ProcessRegistry()

    assert recovery.recover_from_checkpoint() == 1
    recovered = recovery.get(session_id)
    assert recovered is not None
    assert recovered.notify_on_complete is True
