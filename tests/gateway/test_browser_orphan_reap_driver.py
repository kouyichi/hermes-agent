"""A gateway reaps the Chromium trees a force-killed predecessor leaked, without ever touching a browser.

``reap_orphaned_browser_sessions`` was reachable from exactly one periodic driver, the browser
janitor, and ``start_cleanup_thread`` was called only from a browser SESSION's creation
(``tools/browser_session.py``). An unclean kill skips the atexit reap, and a restarted gateway that
serves no browser request never started the janitor — so the orphan tree and its launch lease
outlived every later gateway generation (#145010). These tests pin the gateway bootstrap as a driver.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway import run_bootstrap
from tools import browser_session as bs
from tools import browser_session_lifecycle as lifecycle


@pytest.fixture(autouse=True)
def _no_live_janitor():
    """Never let a started janitor tick into the next test (or the host): stop and reset it."""
    yield
    lifecycle.stop_cleanup_thread()
    bs._cleanup_thread = None
    bs._cleanup_running = False


class _Recorder:
    """Records the startup hooks the gateway body calls, so the wiring itself is assertable."""

    def __init__(self):
        self.started = 0
        self.threads: list[threading.Thread] = []

    def start_cleanup_thread(self):
        self.started += 1

    def start_writer(self, thread):
        self.threads.append(thread)


class _FakeTicker:
    def __init__(self, target, *, args=(), kwargs=None, stop_event=None, name="cron-scheduler"):
        self.target, self.args, self.kwargs = target, args, dict(kwargs or {})

    def restart_if_dead(self) -> bool:
        return False


class _FakeCronProvider:
    name = "fake-cron"

    def start(self, stop_event):  # pragma: no cover - never started by the body under test
        stop_event.wait()


def _driver_runner():
    from gateway.config import Platform

    return SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        adapters={Platform.API_SERVER: object()},
        _primary_profile_name="default",
        _draining=False,
        _external_drain_active=False,
    )


def test_gateway_startup_starts_the_browser_orphan_reaper(monkeypatch):
    """The startup body must start the reap's driver — the gateway may never open a browser."""
    recorder = _Recorder()
    monkeypatch.setattr(lifecycle, "start_cleanup_thread", recorder.start_cleanup_thread)
    monkeypatch.setattr("gateway.run._cron_tick_profile_homes", lambda config: [])
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: "in-process")
    monkeypatch.setattr(
        "cron.scheduler_provider.scheduler_for_profile_mode",
        lambda provider, multiplex_profiles: _FakeCronProvider(),
    )
    monkeypatch.setattr("cron.scheduler_thread.SupervisedTickerThread", _FakeTicker)
    from gateway import runtime_ownership

    monkeypatch.setattr(runtime_ownership, "process_ownership", recorder)

    async def _call():
        return run_bootstrap._start_gateway_start_cron_and_housekeeping(_driver_runner())

    asyncio.run(_call())

    assert recorder.started == 1, "gateway startup never started the browser orphan reaper"


def test_the_driver_reaps_orphans_in_a_process_that_opened_no_browser(monkeypatch):
    """Behavioral half: with zero sessions, the started janitor still runs the reap immediately."""
    start = getattr(run_bootstrap, "_start_gateway_browser_orphan_driver", None)
    if start is None:  # reverted source: the same assertion the wiring test makes, spelled here
        pytest.fail("gateway bootstrap does not expose a browser orphan reap driver")
    assert start is not None

    reaps: list[bool] = []
    reaped = threading.Event()

    def _reap() -> int:
        reaps.append(bool(bs._active_sessions))
        reaped.set()
        return 0

    monkeypatch.setattr(lifecycle, "reap_orphaned_browser_sessions", _reap)
    monkeypatch.setattr(lifecycle, "cleanup_inactive_sessions", lambda: None)

    assert not bs._active_sessions, "fixture: this process never opened a browser session"
    start()
    assert reaped.wait(timeout=30), "the janitor never drove the orphan reap"
    assert reaps == [False]
