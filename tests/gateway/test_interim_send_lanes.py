"""Regression: every gateway-side mid-turn send lane is marked interim
(PR 85796 review, B5 + heartbeat/inactivity findings).

Seal-interception on stream-is-the-message adapters converts the first
unmarked send to an armed (chat, turn) key into draft(final=true). The
consumer's own interim lanes (commentary, tail flush) were marked, but
four gateway-side lanes that fire DURING a streaming turn were not:

  - long-running heartbeat ("⏳ Working — N min"), default every 180s
  - inactivity warning ("⚠️ No activity …")
  - plain-text approval fallback (button lane failed)
  - background-review notice

Live probe: the 3-minute heartbeat sealed the live stream with status
text; the real final then posted as a duplicate, and later frames were
silently swallowed by the seal tombstone.

These tests pin the helper's contract and the adapter-level behavior for
a heartbeat-shaped send; the lane wiring itself is a one-line metadata
wrap per call site (grep `_interim_metadata(` in gateway/run.py).
"""

import asyncio
import threading
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner, _interim_metadata
from tests.gateway.relay.test_relay_live_cards import _connected_adapter
from tools.process_registry import ProcessRegistry, ProcessSession


class RecordingTransport:
    def __init__(self):
        self.ops = []

    async def send_outbound(self, payload, platform=None):
        self.ops.append(dict(payload))
        return {"success": True, "message_id": "m.1"}


class TestInterimMetadataHelper:


    def test_does_not_mutate_input(self):
        src = {"thread_id": "t1"}
        _interim_metadata(src)
        assert "_interim_send" not in src


class TestHeartbeatShapedSendDoesNotSeal:
    @pytest.mark.asyncio
    async def test_status_send_with_interim_marker_leaves_stream_open(self):
        """The exact live-probe scenario, fixed: an armed stream + a
        heartbeat-shaped status send marked via _interim_metadata → the
        stream stays open, the status posts as its own message, and the
        true turn-final still seals with the answer."""
        adapter, _ = _connected_adapter()
        t = RecordingTransport()
        adapter._transport = t
        md = {"thread_ts": "1700.5", "message_id": "1700.501"}
        await adapter.send_draft("C1", 7, "partial answer", metadata=md)

        r = await adapter.send(
            "C1",
            "⏳ Working — 3 min — terminal",
            metadata=_interim_metadata(dict(md)),
        )
        assert r.success
        # Status went out as a plain send; no seal, marker stripped.
        sends = [o for o in t.ops if o["op"] == "send"]
        assert len(sends) == 1
        assert "_interim_send" not in (sends[0].get("metadata") or {})
        assert not [o for o in t.ops if o["op"] == "draft" and o.get("final")]

        # The real final still seals with the ANSWER, not the status text.
        rf = await adapter.send("C1", "partial answer, done.", metadata=dict(md))
        assert rf.success
        seals = [o for o in t.ops if o["op"] == "draft" and o.get("final")]
        assert len(seals) == 1
        assert seals[0]["content"] == "partial answer, done."


class _RecordingWatcherAdapter:
    """Process-watcher transport stand-in: records outbound metadata, admits injection."""

    def __init__(self):
        self.sends: list[dict] = []
        self._active_sessions: dict[str, object] = {}

    async def send(self, chat_id, message, metadata=None):
        self.sends.append({"chat_id": chat_id, "text": message, "metadata": dict(metadata or {})})
        return SimpleNamespace(success=True, message_id="m.1")

    async def handle_message(self, event):
        event._gateway_accepted = True
        return True


def _watcher_runner(adapter, mode="concise"):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._load_background_notifications_mode = lambda: mode
    runner._background_tasks = set()
    # Completion dedupe/ledger state the watcher's delivery seam touches.
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    return runner


def _watcher(session_id: str, *, key: str = "agent:main:telegram:dm:123", notify_on_complete: bool,
             thread_id: str = "") -> dict:
    return {
        "session_id": session_id,
        "check_interval": 0,
        "session_key": key,
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "thread_id": thread_id,
        "notify_on_complete": notify_on_complete,
    }


def _finished_session(registry, session_id: str, *, output: str = "done\n") -> ProcessSession:
    session = ProcessSession(
        id=session_id,
        command="echo done",
        task_id="task",
        started_at=1000.0,
        output_buffer=output,
        exited=True,
        exit_code=0,
        notify_on_complete=True,
    )
    registry._finished[session.id] = session
    return session


def _asyncio_sleep_noop(monkeypatch):
    async def _instant(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


class TestProcessWatcherLanes:
    """A watcher receipt that lands DURING the launching turn is not the turn-final
    (#144152): unmarked, a stream-is-the-message adapter seals the live stream with the
    receipt text and the real answer arrives after the remote session already completed."""

    def test_active_turn_receipt_is_marked_interim(self, monkeypatch):
        import tools.process_registry as pr_module

        registry = ProcessRegistry()
        monkeypatch.setattr(pr_module, "process_registry", registry)
        session = _finished_session(registry, "proc_receipt_active_turn")
        adapter = _RecordingWatcherAdapter()
        watcher = _watcher(session.id, notify_on_complete=True, thread_id="77")
        # The launching turn is still inside a turn on this adapter: the completion only
        # queues a follow-up, so the gateway sends the concise receipt now.
        adapter._active_sessions[watcher["session_key"]] = object()
        runner = _watcher_runner(adapter)
        _asyncio_sleep_noop(monkeypatch)

        asyncio.run(runner._run_process_watcher(watcher))

        assert len(adapter.sends) == 1, "the active-turn receipt must still be delivered"
        sent = adapter.sends[0]
        assert sent["metadata"].get("_interim_send") is True
        assert sent["metadata"].get("thread_id") == "77", "thread routing must survive the marker"
        assert sent["chat_id"] == "123"

    def test_idle_completion_notice_stays_the_turn_final(self, monkeypatch):
        import tools.process_registry as pr_module

        registry = ProcessRegistry()
        monkeypatch.setattr(pr_module, "process_registry", registry)
        session = _finished_session(registry, "proc_receipt_idle")
        adapter = _RecordingWatcherAdapter()
        runner = _watcher_runner(adapter)
        _asyncio_sleep_noop(monkeypatch)

        asyncio.run(runner._run_process_watcher(_watcher(session.id, notify_on_complete=False)))

        assert len(adapter.sends) == 1
        # No launching turn is live, so this one IS the answer the adapter must seal on.
        assert "_interim_send" not in adapter.sends[0]["metadata"]

    def test_running_update_is_interim_and_the_final_notice_is_not(self, monkeypatch):
        import tools.process_registry as pr_module

        registry = ProcessRegistry()
        monkeypatch.setattr(pr_module, "process_registry", registry)
        session = ProcessSession(
            id="proc_receipt_running",
            command="sleep 5",
            task_id="task",
            started_at=1000.0,
            output_buffer="partial\n",
            exited=False,
            exit_code=None,
            notify_on_complete=False,
        )
        registry._running[session.id] = session
        adapter = _RecordingWatcherAdapter()
        runner = _watcher_runner(adapter, mode="all")
        ticks = {"n": 0}

        async def _exit_after_first_update(_delay):
            ticks["n"] += 1
            if ticks["n"] >= 2:
                session.exited = True
                session.exit_code = 0

        monkeypatch.setattr(asyncio, "sleep", _exit_after_first_update)

        asyncio.run(runner._run_process_watcher(_watcher(session.id, notify_on_complete=False)))

        assert len(adapter.sends) >= 2
        assert adapter.sends[0]["metadata"].get("_interim_send") is True, adapter.sends
        assert "_interim_send" not in adapter.sends[-1]["metadata"], adapter.sends
