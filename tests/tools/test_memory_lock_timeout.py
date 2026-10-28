"""A stalled memory-lock holder must not park every later write forever (#146076).

``MemoryStore._file_lock`` serialises MEMORY.md / USER.md writes through a sidecar
``.lock`` file. It used to acquire that file with a plain blocking
``fcntl.flock(fd, LOCK_EX)``: one stuck holder — a sibling cron thread in the same
gateway process (flock conflicts between separate ``open()`` calls, so no second
process is needed), a SIGSTOP'ed CLI session, a leaked descriptor — parked the caller
inside the syscall. No log line, no tool error, no timeout that could ever fire, and a
cron worker that never finishes and never frees its pool slot.

These tests hold the lock from a second descriptor and drive the real store. The
default timeout is patched down so the suite never waits the production budget.
"""

import logging
import os
import threading
import time
from contextlib import contextmanager

import pytest

fcntl = pytest.importorskip("fcntl", reason="POSIX flock semantics")

from tools import memory_tool_store
from tools.memory_tool_store import MemoryStore

# Short enough to keep the suite fast, long enough for a retry loop to spin twice.
TEST_LOCK_TIMEOUT = 0.25
# A write that is still blocked after this is parked in the syscall, not merely slow.
DEADLINE = 5.0


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    # raising=False: on the pre-fix tree the constant does not exist, and the sabotage run
    # has to fail on the blocked write, not on a missing attribute at fixture setup.
    monkeypatch.setattr(memory_tool_store, "_MEMORY_LOCK_TIMEOUT_SECONDS", TEST_LOCK_TIMEOUT, raising=False)
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.load_from_disk()
    return s


@contextmanager
def _held_lock(directory):
    """Hold the store's sidecar lock from a SECOND open file description.

    ``flock`` is per open file description, so this contends with the store exactly like
    a stuck sibling thread or process would.
    """
    lock_path = directory / "MEMORY.md.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield lock_path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _run_bounded(store, call):
    """Run *call* in a thread and fail loudly if it is still parked at DEADLINE.

    The thread is a daemon so a pre-fix run (blocked inside ``fcntl.flock``) cannot hang
    the session; the assertion below is what fails.
    """
    result = {}

    def _target():
        result.update(call())

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(DEADLINE)
    assert not thread.is_alive(), "the memory write never returned: it is blocked in flock, not waiting on a bound"
    return result


class TestBoundedMemoryLock:
    def test_a_stalled_holder_yields_an_error_payload_instead_of_blocking(self, store, tmp_path):
        with _held_lock(tmp_path):
            result = _run_bounded(store, lambda: store.add("memory", "written while blocked"))

        assert result["success"] is False
        assert "lock" in result["error"].lower()
        assert "nothing was written" in result["error"].lower()
        # The refusal must not be a half-applied write: no entry reached the store file.
        memory_file = tmp_path / "MEMORY.md"
        assert not memory_file.exists() or "written while blocked" not in memory_file.read_text(encoding="utf-8-sig")

    def test_the_batch_path_is_bounded_too(self, store, tmp_path):
        with _held_lock(tmp_path):
            result = _run_bounded(
                store, lambda: store.apply_batch("memory", [{"action": "add", "content": "batched while blocked"}]))

        assert result["success"] is False
        assert "lock" in result["error"].lower()

    def test_a_contended_lock_is_logged_once_with_its_path(self, store, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="tools.memory_tool"):
            with _held_lock(tmp_path):
                result = _run_bounded(store, lambda: store.add("memory", "logged while blocked"))

        assert result["success"] is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "MEMORY.md.lock" in r.getMessage()]
        assert len(warnings) == 1, f"expected one warning naming the lock file, got {[r.getMessage() for r in caplog.records]}"
        assert "0.2" in warnings[0].getMessage(), warnings[0].getMessage()

    def test_a_write_that_waits_out_a_short_hold_still_lands(self, store, tmp_path):
        """Bounded is not fail-fast: a holder that lets go within the budget is waited for."""
        lock_path = tmp_path / "MEMORY.md.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            result = {}

            def _write():
                result.update(store.add("memory", "landed after the holder let go"))

            thread = threading.Thread(target=_write, daemon=True)
            thread.start()
            # The hold IS the slow operation under test: release it mid-wait, before the budget.
            time.sleep(TEST_LOCK_TIMEOUT / 2)  # health: allow HX013 -- the hold duration is what the waiter must outlast
            fcntl.flock(fd, fcntl.LOCK_UN)
            thread.join(DEADLINE)
            assert not thread.is_alive(), "the write did not proceed after the holder released"
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert result["success"] is True
        assert "landed after the holder let go" in store.memory_entries

    def test_an_uncontended_write_is_unaffected(self, store):
        result = store.add("memory", "plain write")

        assert result["success"] is True
        assert "plain write" in store.memory_entries
