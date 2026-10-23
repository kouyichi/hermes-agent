"""Where a pre-tool checkpoint may root: the task's own workspace, and never an OS tree.

A checkpoint taken before a destructive tool call has two jobs this module guards. It must cover
the tree the call will actually touch — a call without ``workdir`` runs in the session's cwd, not
in whatever directory the gateway process was launched from — and it must refuse an OS-managed
tree outright, because the snapshot indexes every file under it and ``_take`` persists a project
registration for the directory it covered (#143481).
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re

# OS trees a snapshot must never cover. ``/usr/local/src`` and ``/var/www`` are deliberately
# absent: both are ordinary places for a user's own tree to live.
_POSIX_OS_ROOTS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys")
# Used only when the environment does not name the Windows directory itself.
_WINDOWS_OS_ROOT_FALLBACK = "C:\\Windows"
_DRIVE_PATH = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_DRIVE_ROOT = re.compile(r"^[a-z]:\\$")
_UNC_PATH = "\\\\"


def _windows_os_roots() -> tuple[str, ...]:
    """Every spelling of the Windows directory this host knows, ``ntpath``-normalized."""
    named = (os.environ.get("SystemRoot") or "", os.environ.get("WINDIR") or "", _WINDOWS_OS_ROOT_FALLBACK)
    return tuple(ntpath.normcase(ntpath.normpath(root)) for root in named if root.strip())


def os_root_reason(path: str) -> str | None:
    """Why *path* is an OS-managed tree a checkpoint must not cover, else ``None``.

    Windows spellings are classified with ``ntpath``, so the answer does not depend on the host
    that judges them: a desktop launch on Windows inherits ``C:\\Windows\\System32`` as its process
    cwd, and a Linux CI run of the guard has to reach the same verdict.
    """
    raw = str(path or "").strip()
    if not raw:
        return None
    if _DRIVE_PATH.match(raw) or raw.startswith(_UNC_PATH):
        normalized = ntpath.normcase(ntpath.normpath(raw))
        if _DRIVE_ROOT.match(normalized):
            return "a drive root"
        for root in _windows_os_roots():
            if normalized == root or normalized.startswith(root + "\\"):
                return "under the Windows system directory"
        return None
    normalized = posixpath.normpath(raw)
    if normalized == "/":
        return "the filesystem root"
    for root in _POSIX_OS_ROOTS:
        if normalized == root or normalized.startswith(root + "/"):
            return f"under {root}"
    return None


def checkpoint_workspace_root(task_id: str = "default") -> str:
    """Directory a pre-tool checkpoint should snapshot for *task_id*.

    Same precedence the terminal tool uses for a command with no ``workdir``: the session's
    recorded cwd, then a registered cwd override, then ``$TERMINAL_CWD``. The process cwd stays
    as the last resort, and :func:`os_root_reason` refuses it when it names an OS tree.
    """
    from tools.terminal_tool import get_session_cwd, resolve_task_overrides

    recorded = get_session_cwd(task_id)
    if recorded:
        return recorded
    override = resolve_task_overrides(task_id).get("cwd")
    if isinstance(override, str) and override.strip():
        return override
    from agent.runtime_cwd import scope_terminal_cwd

    return scope_terminal_cwd() or os.getcwd()
