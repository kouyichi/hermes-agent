"""Profile directory removal: wait out the live owner, then rmtree with a short retry.

It also owns what the ``profiles/.deleted`` tombstone means after a FAILED removal: a surviving
populated profile is restored (tombstone cleared) instead of becoming invisible, and the error text
names the state so recovery needs no source read.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import stat
import time

# A multiplexer answers a delete's rescan ``pending`` after 5 s and keeps tearing the profile down;
# its gateway.lock reservation is released last, once the profile's writers are gone.
OWNER_RELEASE_WAIT_SECONDS = 60.0


def wait_profile_unowned(profile_dir: Path, *, timeout: float | None = None) -> bool:
    """True once no gateway runtime holds ``profile_dir``'s ``gateway.lock`` (taken and released
    through ``exclusive_maintenance``); False when it is still held after ``timeout`` seconds."""
    from gateway.runtime_ownership import OwnershipConflict, exclusive_maintenance
    deadline = time.monotonic() + (OWNER_RELEASE_WAIT_SECONDS if timeout is None else timeout)
    while True:
        try:
            with exclusive_maintenance([profile_dir]):
                return True
        except OwnershipConflict:
            pass
        except OSError:
            # A read-only copy (0555 home, 0444 lock) cannot host a reservation: probe it instead.
            if _lock_unheld_read_only(profile_dir / 'gateway.lock'):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _lock_unheld_read_only(path: Path) -> bool:
    """A missing lock, or one our own flock through a read-only descriptor wins, has no holder."""
    if os.name == 'nt':
        return False  # a sharing violation there is a live holder
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _rmtree_make_writable(func, path, exc):
    """onexc/onerror handler: add +w on PermissionError so rmtree can proceed. Covers NixOS-
    style read-only copies where the path itself (0444) or its parent (0555) isn't writable."""
    # onexc(func, path, exc_instance) on 3.12+; onerror(func, path, exc_info_tuple) on 3.11.
    if isinstance(exc, tuple):
        exc = exc[1]
    if not isinstance(exc, PermissionError):
        raise
    for target in (path, os.path.dirname(path)):  # parent needed for unlink/rmdir
        if target:
            with contextlib.suppress(OSError):
                os.chmod(target, os.stat(target).st_mode | stat.S_IWUSR)
    func(path)


def failed_removal_error(profile_name: str, profile_dir: Path, cause: BaseException) -> RuntimeError:
    """The delete error for a tree ``rmtree`` could not remove, and the tombstone's new meaning.

    ``delete_profile`` tombstones before removing (a stale serve/logging ``mkdir`` must not relist
    the name), so a removal that fails would otherwise leave the directory **unreachable and
    invisible**: ``profile_exists()`` answers False, ``profile list`` omits it, ``-p <name>``
    refuses it, and the error text suggests ``profile create <name>`` to recreate it — against
    data that is still on disk. A profile that kept its identity is still deletable, so clear its
    tombstone and let the name resolve again; only a marker-less shell stays tombstoned.
    """
    from hermes_constants import (
        clear_named_profile_deleted, named_profile_has_identity, profile_tombstone_path,
    )
    if named_profile_has_identity(profile_dir):
        clear_named_profile_deleted(profile_dir)
        # A live multiplexer unroutes a tombstoned name immediately; ask it to rescan so the
        # restored profile is served again now instead of at its next periodic pass.
        with contextlib.suppress(Exception):
            from hermes_cli.gateway_multiplex_served import notify_multiplexer_profiles_changed
            notify_multiplexer_profiles_changed(profile_name)
        return RuntimeError(
            f"Could not remove profile directory {profile_dir}: {cause}. "
            f"The profile was left intact and is listed again; retry the delete with: "
            f"hermes profile delete {profile_name}"
        )
    return RuntimeError(
        f"Could not remove profile directory {profile_dir}: {cause}. What is left carries no "
        f"profile identity, so it stays tombstoned ({profile_tombstone_path(profile_dir)}); "
        f"`hermes profile create {profile_name}` can reclaim it."
    )


def missing_profile_error(profile_name: str, profile_dir: Path) -> FileNotFoundError:
    """``profile_dir`` resolves to no LIVE profile: say which of the two states it is.

    Absent (never created, or deleted and removed) keeps the old "create it" advice. A tombstoned
    directory that is still populated is an *unfinished* delete, so name it and the tombstone
    instead — telling the operator to ``profile create`` there would overwrite real user data.
    """
    from hermes_constants import named_profile_has_identity, named_profile_is_deleted, profile_tombstone_path
    if named_profile_is_deleted(profile_dir) and named_profile_has_identity(profile_dir):
        return FileNotFoundError(
            f"Profile '{profile_name}' is marked deleted but its directory is still there: "
            f"{profile_dir}. Its deletion did not finish — remove the tombstone "
            f"{profile_tombstone_path(profile_dir)} to recover it, then retry: "
            f"hermes profile delete {profile_name}"
        )
    return FileNotFoundError(
        f"Profile '{profile_name}' does not exist. Create it with: hermes profile create {profile_name}"
    )


def _rmtree_with_retry(profile_dir: Path, onexc_handler) -> None:
    """``shutil.rmtree`` with a short retry loop: a just-terminated process can leave in-flight
    writes (SQLite -wal/-shm checkpoints, sandbox temp files) landing after rmtree walked
    past a directory — ENOTEMPTY on POSIX, transient PermissionError on Windows."""
    attempts = 3
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            try:
                shutil.rmtree(profile_dir, onexc=onexc_handler)
            except TypeError:  # ``onexc`` is 3.12+; 3.11 has ``onerror``
                shutil.rmtree(profile_dir, onerror=onexc_handler)
            return
        except OSError as e:
            last_exc = e
            if not profile_dir.exists():
                return
            if attempt < attempts - 1:
                time.sleep(0.3 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
