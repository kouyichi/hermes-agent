"""The stale-stream watchdog decides before the liveness heartbeat runs.

``StreamingWaitMonitor._monitor_loop`` runs the 30s activity heartbeat on the turn
thread, and in a dispatcher-spawned kanban worker that heartbeat is a board write
(``_touch_activity`` → ``heartbeat_current_worker_from_env`` → ``BEGIN IMMEDIATE``).
While the board is held by any long writer the heartbeat parks the thread, so a stale
comparison placed *after* it cannot run and the turn is aborted by the liveness
watchdog instead of being retried by the stale watchdog (#145886).

The watchdog's own decision must not depend on liveness I/O, so the stale/interrupt
evaluation runs first: a threshold already crossed is acted on in the same tick that
would have blocked.
"""

from __future__ import annotations

import itertools
import threading
from types import SimpleNamespace

from agent import chat_completion_stream_monitor as monitor_module


def _stepping_clock(monkeypatch, start: float = 1000.0, step: float = 31.0) -> None:
    """Freeze wall clock into one 31s tick per read: the heartbeat interval is due."""
    ticks = itertools.count(start, step)
    monkeypatch.setattr(monitor_module, "time", SimpleNamespace(time=lambda: next(ticks)))


def _monitor(order: list[str], *, silence_seconds: float) -> monitor_module.StreamingWaitMonitor:
    mon = monitor_module.StreamingWaitMonitor()
    mon.agent = SimpleNamespace(
        base_url="", _interrupt_requested=False,
        _emit_wait_notice=lambda *args, **kwargs: None,
        _touch_activity=lambda *args, **kwargs: None,
    )
    mon.last_chunk_time = {"t": 1000.0 - silence_seconds}
    mon._stream_stale_timeout = 300.0
    mon._call_done = threading.Event()

    def _kill(elapsed: float) -> None:
        order.append("stale_kill")
        mon._call_done.set()

    def _heartbeat(waiting_secs: int) -> None:
        order.append("heartbeat")
        mon._call_done.set()

    mon._kill_stale_stream = _kill  # type: ignore[method-assign]
    mon._heartbeat = _heartbeat  # type: ignore[method-assign]
    mon._abort_for_interrupt = lambda elapsed: order.append("interrupt")  # type: ignore[method-assign]
    return mon


def test_stale_kill_is_decided_before_the_heartbeat(monkeypatch):
    _stepping_clock(monkeypatch)
    order: list[str] = []
    mon = _monitor(order, silence_seconds=1000.0)

    mon._monitor_loop()

    assert order[0] == "stale_kill", order


def test_a_pending_interrupt_is_honoured_before_the_heartbeat(monkeypatch):
    _stepping_clock(monkeypatch)
    order: list[str] = []
    mon = _monitor(order, silence_seconds=1.0)
    mon.agent._interrupt_requested = True

    mon._monitor_loop()

    assert order == ["interrupt"], order
