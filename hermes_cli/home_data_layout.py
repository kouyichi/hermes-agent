"""Machine-specific state excluded from portable home transfers."""
from __future__ import annotations


# Dependency generations, downloaded tools, and partial transfers belong to this machine.
PM_RUNTIME_ROOT_DIRS = frozenset({"installs", "tools", "cache"})


def profile_root_entry(parts: tuple[str, ...]) -> str | None:
    """Locate a home-root entry without excluding same-named skill/plugin data."""
    if len(parts) >= 3 and parts[0] == "profiles":
        return parts[2]
    return parts[0] if parts else None


# Lease suffixes a live process holds open: ``wake-word.lock`` (the mic lease) and
# ``active_sessions.lock`` are the current producers; a PID lease follows the same shape.
_RUNTIME_LEASE_SUFFIXES = (".lock", ".pid")


def is_runtime_lease_file(parts: tuple[str, ...]) -> bool:
    """True for a ``*.lock``/``*.pid`` file sitting directly inside a home root's ``runtime/``.

    ``runtime/`` holds only source-machine state, and a lease there is held by a live process
    for as long as it runs, so it is never portable. On Windows the lease is a 1-byte
    byte-range lock and byte-range locks are mandatory: any other handle that reads it gets
    ``ERROR_LOCK_VIOLATION`` (``PermissionError`` here), so archiving the file turns a
    complete backup into "Backup incomplete" + exit 1 on every run that overlaps the holder
    (#143667). Root and named-profile homes both qualify; deeper entries under ``runtime/``
    and same-named dirs in skill/plugin trees are user data and stay archivable.
    """
    home_parts = parts[2:] if len(parts) >= 3 and parts[0] == "profiles" else parts
    return (len(home_parts) == 2 and home_parts[0] == "runtime"
            and home_parts[1].lower().endswith(_RUNTIME_LEASE_SUFFIXES))

