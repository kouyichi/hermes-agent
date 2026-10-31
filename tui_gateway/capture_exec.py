"""Captured-text child execution for the TUI gateway exec RPCs (``shell.exec``, ``cli.exec`` and
the ``type: exec`` quick command).

``subprocess.run`` kills only the direct child when its timeout expires. A command whose shell
backgrounds a child (``cmd &``, ``nohup … &``, anything the command daemonizes) leaves that
grandchild alive holding the inherited stdout/stderr pipes: it outlives the RPC, and where the
drain after the kill waits for EOF on those pipes the grandchild also decides when the call
returns — the reported case took about a minute for a 3 s budget (#147103). So the child is
spawned as its own process group, the timeout reaps the whole tree, and the post-kill drain is
bounded so a descendant that escaped the group cannot pin the RPC worker.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

# How long the post-kill drain may spend collecting output a reaped child already wrote. The tree
# is dead by then, so this only bounds a descendant that escaped the group (setsid / reparented).
_DRAIN_SECONDS = 1.0


def _hide_flags() -> int:
    """``CREATE_NO_WINDOW`` on Windows (no console behind the desktop parent), ``0`` elsewhere.
    Imported lazily: startup budget, and the tests pin that helper's value."""
    from hermes_cli._subprocess_compat import windows_hide_flags

    return windows_hide_flags()


def _spawn_kwargs() -> dict:
    """Popen kwargs for a captured, non-interactive child in its own process group."""
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=_hide_flags())
    if sys.platform != "win32":
        # The group exists only so the hard timeout below can reach grandchildren; Windows
        # rejects start_new_session, and the tree kill there goes through taskkill /T.
        # detached-spawn: ok — awaited here; the timeout kills the group it creates
        kwargs["start_new_session"] = True
    return kwargs


def _reap_tree(proc: subprocess.Popen) -> None:
    """Kill the child AND its descendants: ``proc.kill()`` alone leaves a grandchild that
    inherited the pipes alive, which is the defect this module exists for (#147103)."""
    try:
        from agent.deadline import kill_process_tree
    except ImportError:
        kill_process_tree = None
    if kill_process_tree is not None and kill_process_tree(proc.pid):
        return
    try:
        proc.kill()
    except OSError:
        logger.debug("capture_exec: child %s was already gone", proc.pid, exc_info=True)


def _drain_after_kill(proc: subprocess.Popen, expired: subprocess.TimeoutExpired):
    """Collect what the reaped child wrote, never waiting on a surviving descendant's pipes."""
    try:
        return proc.communicate(timeout=_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        # A descendant outside the group still holds the pipes: stop reading instead of pinning
        # the caller, and report the output captured so far.
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                logger.debug("capture_exec: closing a lingering pipe failed", exc_info=True)
        return expired.output, expired.stderr


def run_captured(
    cmd, timeout, *, cwd=None, shell: bool = False, env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` with captured UTF-8 text output and reap its whole tree on ``timeout``.

    Same contract as ``subprocess.run(..., capture_output=True, text=True, timeout=timeout)``:
    a ``TimeoutExpired`` carries whatever output was collected. The difference is what happens on
    timeout — the process group dies with the command instead of leaving orphans behind.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, shell=shell, env=env, **_spawn_kwargs())
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        _reap_tree(proc)
        stdout, stderr = _drain_after_kill(proc, expired)
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
