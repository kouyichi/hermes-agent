"""The Bot Desktop start lock cannot assume POSIX.

``tool_placement()`` reaches ``start()`` -> ``_start_in_sandbox()`` -> ``_flocked()`` *before* the
Linux-only gate in ``start()``: a screen on a ``terminal`` placement lives inside the terminal
backend (docker/ssh), so it is meant to come up from whatever host the gateway runs on. ``_flocked``
imported ``fcntl`` inline under a ``# windows-footgun: ok`` marker whose justification ("is_supported_host
gates start") only holds for the gateway-host branch, so a Windows gateway with
``terminal.backend: docker`` died with ``ModuleNotFoundError: No module named 'fcntl'`` on every
browser_exec call (#143567).

Driven here without a Windows box: ``fcntl`` is hidden and a fake ``msvcrt`` stands in for the
byte-range lock Windows needs, which takes the same shape as ``agent/vault_store.py``.
"""

from __future__ import annotations

import os

import pytest

from tools.bot_desktop import runtime


class _FakeMsvcrt:
    """The two constants and the byte-range lock ``_flocked`` may take; records what it was asked."""

    LK_LOCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.sizes: list[int] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes))
        self.sizes.append(os.fstat(fd).st_size)


@pytest.fixture
def no_fcntl(monkeypatch):
    """A host without ``fcntl``: ``raising=False`` so the same fixture loads against the unfixed module."""
    fake = _FakeMsvcrt()
    monkeypatch.setattr(runtime, "fcntl", None, raising=False)
    monkeypatch.setattr(runtime, "msvcrt", fake, raising=False)
    return fake


def test_the_start_lock_takes_a_byte_range_lock_without_fcntl(tmp_path, no_fcntl):
    lock = tmp_path / "start.lock"
    with runtime._flocked(lock) as fh:
        assert not fh.closed
    assert no_fcntl.calls == [(no_fcntl.LK_LOCK, 1), (no_fcntl.LK_UNLCK, 1)]
    # msvcrt locks a byte RANGE, so the byte has to exist before the lock is taken.
    assert no_fcntl.sizes[0] >= 1


def test_a_failing_body_still_releases_the_byte_range(tmp_path, no_fcntl):
    """A lock left held wedges every later start() of that profile, so the release cannot depend on the body."""
    with pytest.raises(RuntimeError, match="boom"):
        with runtime._flocked(tmp_path / "start.lock"):
            raise RuntimeError("boom")
    assert no_fcntl.calls[-1] == (no_fcntl.LK_UNLCK, 1)


def test_a_live_lock_file_is_reused_in_place(tmp_path, no_fcntl):
    """A lock file another start already grew must not be rewritten: truncating it would drop the byte the
    other process holds a range lock on."""
    lock = tmp_path / "start.lock"
    lock.write_text("x", encoding="utf-8")
    with runtime._flocked(lock):
        pass
    assert lock.read_text(encoding="utf-8") == "x"
