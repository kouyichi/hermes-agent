"""A ``!cmd`` timeout must reap the command's whole process tree, not just the shell (#147103).

``subprocess.run`` kills only the direct child on timeout. A command whose shell backgrounds a
child (``cmd &``, ``nohup … &``, a tool that daemonizes) leaves that grandchild alive holding the
inherited stdout/stderr pipes: it survives the RPC, and where ``subprocess`` drains the killed
child through a pipe it also controls when that drain ends — which is how a 3 s ``!cmd`` came back
in about a minute on Windows.

These tests drive the real ``_captured_exec`` helper shared by the ``shell.exec`` / ``cli.exec`` /
exec quick-command RPCs against real children. They fail on the pre-fix tree: the timeout error is
returned on time but the grandchild is still running.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tui_gateway import server

# ``_captured_exec`` is published onto ``server`` by ``method_ctx.bind_module`` at install time,
# i.e. as an attribute rather than a definition in server.py.
_captured_exec = server._captured_exec


def _background_child_script(tmp_path: Path) -> tuple[str, Path]:
    """A shell command that backgrounds a long child, records its pid, and waits for it."""
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "bg.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"sleep 300 &\necho $! > '{pid_file}'\n"
        "wait\n",
        encoding="utf-8",
    )
    return f"sh '{script}'", pid_file


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_death(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.05)  # health: allow HX013 -- polling a real child's exit after a group kill
    return not _pid_is_alive(pid)


def test_captured_exec_timeout_reaps_the_grandchild(tmp_path: Path):
    """The timeout error comes back promptly AND the backgrounded child is gone."""
    command, pid_file = _background_child_script(tmp_path)

    started = time.monotonic()
    response = _captured_exec(
        "reap", command, 1, on_result=lambda r: r,
        timeout_err=(5002, "command timed out (1s)"), fail_code=5003, shell=True)
    elapsed = time.monotonic() - started

    assert response["error"]["code"] == 5002, response
    assert elapsed < 10, f"timeout returned after {elapsed:.1f}s instead of the 1 s budget"
    assert pid_file.exists(), "the child never started"
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _wait_for_death(child_pid), (
        f"the backgrounded grandchild (pid {child_pid}) outlived the timeout: only the shell was reaped")


def test_captured_exec_still_returns_output_and_exit_code():
    """The reaping spawn keeps the capture contract: text output, exit code, no timeout."""
    response = _captured_exec(
        "ok", "echo captured-ok", 10,
        on_result=lambda r: {"stdout": r.stdout, "code": r.returncode},
        timeout_err=(5002, "command timed out (10s)"), fail_code=5003, shell=True)

    assert response == {"stdout": "captured-ok\n", "code": 0}
