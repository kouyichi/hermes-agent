"""Identity of a gateway process whose profile exists only in its environment.

``hermes_cli.gateway_runtime_start.spawn_unmanaged_gateway()`` starts the on-demand
``gateway run --quiet --idle-exit`` daemon for a ``<root>/profiles/<name>`` home WITHOUT
``--profile``: it hands the home over through the child's environment, which ``/proc/<pid>/environ``
still holds at exec time. ``gateway.status._command_line_belongs_to_profile`` reads argv alone, so
that daemon was rejected as another profile's process and every reader of
``resolve_gateway_liveness()`` (``gateway list``, ``profile list``, the dashboard profiles page)
reported it stopped while it held the lock and served the profile (#147147). This module is the
second place that identity exists; ``gateway/status.py`` consults it only when argv names no
identity of its own, so an explicit ``-p``/``--profile``/``HERMES_HOME=`` keeps winning.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Optional


def live_process_hermes_home(pid: int) -> Optional[Path]:
    """``HERMES_HOME`` from ``pid``'s environment, or None when it is unreadable or unset.

    ``/proc`` first: one small read and no fork, since this runs per live gateway on every status
    poll. psutil answers where procfs is absent (macOS). Never raises -- an unreadable environment
    leaves the caller on the argv-only rule it had before.
    """
    with contextlib.suppress(OSError):
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        return _home_from_environ(raw.decode("utf-8", errors="replace").split("\0"))
    with contextlib.suppress(Exception):
        import psutil  # type: ignore

        environ = psutil.Process(pid).environ()
        return _home_from_environ([f"{key}={value}" for key, value in environ.items()])
    return None


def _home_from_environ(tokens: list[str]) -> Optional[Path]:
    """Value of the last ``HERMES_HOME=`` token, or None (an empty value never names a home)."""
    for token in reversed(tokens):
        if token.startswith("HERMES_HOME="):
            value = token.partition("=")[2].strip()
            return Path(value) if value else None
    return None


def command_line_belongs_to_live_profile(pid: int, command: str, profile_home: Path) -> bool:
    """True when ``command`` -- or, failing that, the environment ``pid`` was spawned with --
    belongs to ``profile_home``.

    The argv rule stays authoritative wherever it names an identity of its own: a foreign
    ``-p/--profile`` or ``HERMES_HOME=`` on the command line is a deliberate launch spelling and
    must never be overridden by whatever the process environment happens to say. Only an argv that
    names neither is answered from the live environment, which is where the runtime puts the
    profile of the on-demand daemon it spawns (#147147).
    """
    from gateway import status

    if status._command_line_belongs_to_profile(command, profile_home):
        return True
    if status.profile_flag_value(command) is not None:
        return False
    if status.hermes_home_assignments(command.lower().replace("\\", "/")):
        return False
    env_home = live_process_hermes_home(pid)
    return env_home is not None and status._same_hermes_home(env_home, profile_home)
