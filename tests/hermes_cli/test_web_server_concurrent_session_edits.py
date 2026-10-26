"""Invariant: two offline session edits served at once by ONE ``hermes serve`` process
serialize instead of refusing each other (#145242).

``_mutate_offline_store`` takes the profile's ``gateway.lock`` through
``exclusive_maintenance``, and ``exclusive_maintenance`` builds a fresh ``ProfileOwnership``
per outermost call. flock belongs to the open file description, so a second edit in the
same process finds the first one's lock and ``reserve`` refuses it with the gateway's
wording — although no gateway runs. The dashboard parks those edits on ``asyncio.to_thread``,
so a Desktop reopening a session turns that into a burst of 409s and lost edits.

Both tests drive the production entry point ``_with_session_maintenance`` (the funnel for
``_mutate_offline_store`` and the bulk prune route), not a private helper.
"""

from __future__ import annotations

import threading

import pytest

from hermes_cli import web_server_cron
from hermes_cli import web_server_sessions as wss

pytestmark = pytest.mark.platforms("posix")  # POSIX flock holder


def _isolated_home(tmp_path, monkeypatch):
    """Point ``_with_session_maintenance`` at a throwaway profile home."""
    home = tmp_path / "profile-home"
    home.mkdir()
    monkeypatch.setattr(web_server_cron, "_cron_profile_home", lambda profile: ("default", home))
    return home


def test_two_concurrent_offline_edits_serialize_instead_of_refusing(tmp_path, monkeypatch):
    """The reported shape: one process, two edits at once, no gateway.

    Records the order the operations actually ran in, so "both completed" cannot pass by
    running them side by side: the second must start only after the first released.
    """
    _isolated_home(tmp_path, monkeypatch)
    inside = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def hold():
        def operation():
            events.append("hold:enter")
            inside.set()
            release.wait(2.0)
            events.append("hold:exit")

        wss._with_session_maintenance("default", operation)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert inside.wait(5.0), "the first edit never took the maintenance lock"
        ran = []
        wss._with_session_maintenance("default", lambda: (events.append("second:enter"), ran.append(True)))
        assert ran == [True]
    finally:
        release.set()
        holder.join(timeout=5.0)

    assert not holder.is_alive()
    assert events == ["hold:enter", "hold:exit", "second:enter"]


def test_a_home_held_by_another_owner_still_answers_409(tmp_path, monkeypatch):
    """In-process queueing must not swallow a real owner: a gateway (or another process's
    maintenance) holding ``gateway.lock`` still refuses, and the refusal leaves the queue
    free for the next edit."""
    from fastapi import HTTPException

    from gateway.runtime_ownership import ProfileOwnership

    home = _isolated_home(tmp_path, monkeypatch)
    foreign = ProfileOwnership()
    foreign.reserve([home])
    try:
        with pytest.raises(HTTPException) as refused:
            wss._with_session_maintenance("default", lambda: pytest.fail("ran without the owner lock"))
        assert refused.value.status_code == 409
        assert "Exclusive maintenance refused" in str(refused.value.detail)
    finally:
        foreign.release(home)

    ran = []
    wss._with_session_maintenance("default", lambda: ran.append(True))
    assert ran == [True]
