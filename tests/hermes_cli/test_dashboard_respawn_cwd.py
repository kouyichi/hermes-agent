"""The update respawn must relaunch a manual dashboard backend in its ORIGINAL directory.

A respawned backend's cwd decides which context files (repo ``AGENTS.md`` and friends) load into
every session's system prompt. ``hermes update`` captured ``(pid, argv, hermes_home)`` and passed
no ``cwd=`` to ``Popen``, so the revived backend inherited the update process's own directory and
every dashboard session silently got a different prompt (#146565).

The regression test drives the real capture → kill → respawn path with a stand-in backend process
whose cwd is a real directory, so the assertion is on the ``Popen`` keyword the OS would receive.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import dashboard_procs
from hermes_cli import main_dashboard


class _RecordingPopen:
    """Records every spawn instead of starting a second backend."""

    def __init__(self, calls: list[dict]):
        self._calls = calls

    def __call__(self, cmd, **kwargs):
        self._calls.append({"cmd": list(cmd), "cwd": kwargs.get("cwd")})

        class _Alive:
            returncode = None

            def poll(self):
                return None

        return _Alive()


def _stand_in_backend(cwd: Path) -> subprocess.Popen:
    """A real process sitting in *cwd* so ``/proc/<pid>/cwd`` (or psutil) is truthful."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return proc


@pytest.fixture()
def _sandbox_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DESKTOP_CHILD_PID", raising=False)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda *a, **k: home)
    monkeypatch.setattr(dashboard_procs, "_lock_owned_serve_pids", lambda: set())
    monkeypatch.setattr(main_dashboard, "_restart_managed_dashboard_service", lambda *a, **k: False)
    monkeypatch.setattr(main_dashboard, "_loaded_launchd_backend_jobs", lambda *a, **k: [])
    return home


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill path; /proc cwd fixture")
def test_respawn_keeps_the_manual_backends_original_cwd(tmp_path, monkeypatch, _sandbox_home):
    backend_cwd = tmp_path / "repo"
    backend_cwd.mkdir()
    proc = _stand_in_backend(backend_cwd)
    try:
        monkeypatch.setattr(main_dashboard, "_find_stale_dashboard_pids", lambda **k: [proc.pid])
        monkeypatch.setattr(main_dashboard, "_get_pid_cgroup_path", lambda pid: None)
        monkeypatch.setattr(main_dashboard, "_get_systemd_service_for_pid", lambda pid: None)
        monkeypatch.setattr(
            main_dashboard, "_dashboard_cmdline_for_pid",
            lambda pid: ["hermes", "serve", "--port", "8300"],
        )
        monkeypatch.setattr(
            dashboard_procs, "_hermes_home_for_pid", lambda pid, **k: str(_sandbox_home)
        )
        calls: list[dict] = []
        monkeypatch.setattr(main_dashboard.subprocess, "Popen", _RecordingPopen(calls))
        monkeypatch.setattr(main_dashboard.time, "sleep", lambda _s: None)

        def _record_kill(pids, killed, failed):
            killed.extend(pids)

        monkeypatch.setattr(dashboard_procs, "_kill_pids_posix", _record_kill)
        dashboard_procs._kill_stale_dashboard_processes(restart_managed=True)
    finally:
        proc.kill()
        proc.wait(timeout=30)

    assert [c["cmd"] for c in calls] == [["hermes", "serve", "--port", "8300"]]
    assert calls[0]["cwd"] == os.path.realpath(backend_cwd)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX /proc cwd read")
def test_pid_cwd_reads_a_live_process_directory(tmp_path):
    helper = getattr(dashboard_procs, "_pid_cwd", None)
    if helper is None:  # source reverted: the behaviour under test does not exist yet
        pytest.skip("_pid_cwd not present")
        return
    backend_cwd = tmp_path / "workdir"
    backend_cwd.mkdir()
    proc = _stand_in_backend(backend_cwd)
    try:
        assert os.path.realpath(helper(proc.pid)) == os.path.realpath(backend_cwd)
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_pid_cwd_is_none_for_a_dead_process():
    helper = getattr(dashboard_procs, "_pid_cwd", None)
    if helper is None:
        pytest.skip("_pid_cwd not present")
        return
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    assert helper(proc.pid) is None


def test_respawn_falls_back_when_the_recorded_cwd_is_gone(tmp_path, monkeypatch):
    """A backend cwd deleted during the update must not turn the respawn into a spawn failure."""
    from hermes_cli.main_dashboard import _respawn_dashboard_processes

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    calls: list[dict] = []
    monkeypatch.setattr(main_dashboard.subprocess, "Popen", _RecordingPopen(calls))
    monkeypatch.setattr(main_dashboard.time, "sleep", lambda _s: None)
    command = ["hermes", "serve", "--port", "8300"]
    gone = tmp_path / "deleted-workdir"

    failed = _respawn_dashboard_processes(
        [command], cwd_by_command={tuple(command): str(gone)}
    )

    assert failed == []
    assert calls[0]["cwd"] is None
