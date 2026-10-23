"""A pre-tool checkpoint never covers an OS-managed tree, whichever host judges it.

``C:\\Windows\\System32`` reaches the checkpoint store as a process cwd on a desktop launch, so
the verdict has to be reachable from a Linux CI run too (#143481).
"""

import os
from types import SimpleNamespace

import pytest

from agent.tool_executor import _ToolCallRef, _begin_tool_execution
from tools.checkpoint_manager import CheckpointManager

try:  # the module the fix adds; a sabotage run reverts the source and keeps the tests
    from tools import checkpoint_paths as _paths
except ImportError:  # pragma: no cover - only the reverted-source run takes this branch
    _paths = None


def _os_root_reason():
    """The predicate under test, or a skip when the reverted source lacks it."""
    module = _paths
    if module is None:
        pytest.skip("tools.checkpoint_paths is missing")
    return module.os_root_reason


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", tmp_path / "checkpoints")
    return CheckpointManager(enabled=True)


@pytest.mark.parametrize(
    "path",
    [
        "C:\\Windows\\System32",
        "C:\\Windows",
        "c:/windows/System32/drivers",
        "C:\\",
    ],
)
def test_windows_system_trees_are_refused(path):
    assert _os_root_reason()(path)


@pytest.mark.parametrize("path", ["/", "/etc", "/etc/hermes", "/proc/1/cwd", "/sys/class", "/bin", "/lib64"])
def test_posix_system_trees_are_refused(path):
    assert _os_root_reason()(path)


@pytest.mark.parametrize(
    "path",
    [
        "/usr/local/src/app",
        "/var/www/site",
        "/home/me/project",
        "/srv/app",
        "/tmp/scratch",
        "C:\\Users\\me\\project",
        "D:\\work\\app",
        "C:\\Program Files\\App",  # an install target, not the OS tree itself
        "",
    ],
)
def test_workspace_directories_are_allowed(path):
    assert _os_root_reason()(path) is None


def test_windows_root_follows_the_systemroot_environment(monkeypatch):
    """A Windows directory on another volume is still the system directory."""
    monkeypatch.setenv("SystemRoot", "D:\\WINNT")
    reason = _os_root_reason()
    assert reason("D:\\WINNT\\System32")
    assert reason("D:\\work\\app") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX tree; the Windows spelling is in the table above")
def test_ensure_checkpoint_refuses_an_os_tree(tmp_path, manager):
    """The refusal happens before the store is touched: no snapshot, no project registration."""
    assert manager.ensure_checkpoint("/etc", "before terminal: rm -rf build") is False
    assert not (tmp_path / "checkpoints" / "store").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX tree; the Windows spelling is in the table above")
def test_destructive_terminal_checkpoint_skips_an_os_launch_dir(tmp_path, monkeypatch, manager):
    """The reported sequence: no ``workdir``, no ``$TERMINAL_CWD`` and no session record, so the
    process cwd is the only anchor left and it names an OS tree. Nothing is snapshotted and no
    project is registered for it (#143481)."""
    agent = SimpleNamespace(
        _checkpoint_mgr=manager, quiet_mode=True, tool_progress_callback=None,
        tool_start_callback=None, verbose_logging=False, log_prefix_chars=100,
        _touch_activity=lambda *_: None,
    )
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.chdir("/etc")

    _begin_tool_execution(
        agent,
        _ToolCallRef("terminal", {"command": "rm -rf build"}, "os-launch-dir", "call-1", []),
        None,
    )

    assert not (tmp_path / "checkpoints" / "store").exists()


def test_ensure_checkpoint_still_accepts_a_workspace(tmp_path, manager):
    """Control: an ordinary workspace keeps its checkpoint, so the guard is not a blanket refusal."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('hi')\n", encoding="utf-8")

    assert manager.ensure_checkpoint(str(workspace), "before terminal: rm -rf build") is True
    assert manager.list_checkpoints(str(workspace))
