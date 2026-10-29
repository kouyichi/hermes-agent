"""The steer/redirect seams refuse a text-only copy of the live turn's own prompt (#84417).

The queue path already drops a self-copy of the live user text so a drain cannot
re-fire it. The two correction seams — ``_handle_busy_submit``'s
``_ac_try_correction`` (``busy_input_mode`` steer/interrupt) and the
``session.steer`` / ``session.redirect`` RPCs (``_apply_correction``) — injected
the same text straight back into the running turn instead. Any client resend (a
retry after a WS timeout, a double-submit, a replayed pending correction) then
produced duplicate user text inside one turn with a single acceptance log and a
single durable row, which is the hardest duplication signature to diagnose.

A genuinely different correction must still steer through, an authored copy is
another sender's message, and a turn retained after a failure is a retry the
user still wants.
"""

import threading
import types

from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "transport": None,
        "attached_images": [],
        **extra,
    }


def _inflight(user, **extra):
    return {"user": user, "assistant": "partial reply", "streaming": True, **extra}


def _steering_agent(calls, *, redirect=True):
    """An agent that records the text it was steered/redirected with."""
    agent = types.SimpleNamespace(steer=lambda text: calls.append(("steer", text)) or True)
    if redirect:
        agent._supports_active_turn_redirect = True
        agent.redirect = lambda text: calls.append(("redirect", text)) or True
    return agent


# ── busy submit (prompt.submit while the turn runs) ────────────────────────


def test_busy_interrupt_mode_drops_self_copy_of_live_prompt(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    calls = []
    session = _session(agent=_steering_agent(calls), inflight_turn=_inflight("do the thing"))

    resp = server._handle_busy_submit("r1", "sid", session, "do the thing", "ws-1")

    assert calls == []
    assert not session["inflight_turn"].get("corrections")
    assert session["inflight_turn"]["user"] == "do the thing"
    # The queue path answers ``queued`` when it drops the same self-copy; the seam answers the same.
    assert resp["result"]["status"] == "queued"


def test_busy_steer_mode_drops_self_copy_of_live_prompt(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    calls = []
    session = _session(agent=_steering_agent(calls), inflight_turn=_inflight("original request"))

    resp = server._handle_busy_submit("r1", "sid", session, "  original request  ", "ws-1")

    assert calls == []
    assert not session["inflight_turn"].get("corrections")
    assert resp["result"]["status"] == "queued"


def test_busy_steer_mode_still_steers_a_genuine_correction(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    calls = []
    session = _session(agent=_steering_agent(calls), inflight_turn=_inflight("original request"))

    resp = server._handle_busy_submit("r1", "sid", session, "use Postgres instead", "ws-1")

    assert calls == [("steer", "use Postgres instead")]
    assert session["inflight_turn"]["corrections"] == ["use Postgres instead"]
    assert resp["result"]["status"] == "steered"


def test_busy_submit_authored_copy_of_live_prompt_still_steers(monkeypatch):
    """Another sender typing the same words is a message, not a duplicate (#84417)."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    calls = []
    session = _session(agent=_steering_agent(calls), inflight_turn=_inflight("deploy it"))

    server._handle_busy_submit(
        "r1", "sid", session, "deploy it", "ws-1", turn_author={"id": "u2", "name": "second peer"}
    )

    assert calls == [("steer", "deploy it")]


def test_dropped_self_copy_does_not_escalate_to_a_hard_interrupt(monkeypatch):
    """No redirect support: the queue drops the copy, so Stop-adjacent interrupt must not fire."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    interrupts = []
    monkeypatch.setattr(server, "_interrupt_busy_session", lambda *a, **k: interrupts.append(a))
    session = _session(agent=_steering_agent([], redirect=False), inflight_turn=_inflight("keep going"))

    resp = server._handle_busy_submit("r1", "sid", session, "keep going", "ws-1")

    assert interrupts == []
    assert session.get("queued_prompt") is None
    assert resp["result"]["status"] == "queued"


def test_retained_failed_turn_self_copy_still_steers(monkeypatch):
    """A turn kept for replay after a failure is a retry: the resend must reach the agent."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    calls = []
    session = _session(
        agent=_steering_agent(calls),
        inflight_turn=_inflight("run the suite", status="error", error="provider 500"),
    )

    server._handle_busy_submit("r1", "sid", session, "run the suite", "ws-1")

    assert calls == [("redirect", "run the suite")]


# ── session.steer / session.redirect RPCs ─────────────────────────────────


def _rpc(method, session, text):
    server._sessions["sid"] = session
    try:
        return server.handle_request(
            {"id": "1", "method": method, "params": {"session_id": "sid", "text": text}}
        )
    finally:
        server._sessions.pop("sid", None)


def test_session_steer_rpc_drops_self_copy_of_live_prompt():
    calls = []
    session = _session(agent=_steering_agent(calls), inflight_turn=_inflight("original request"))

    resp = _rpc("session.steer", session, "original request")

    assert calls == []
    assert not session["inflight_turn"].get("corrections")
    assert resp["result"] == {"status": "queued", "text": "original request"}


def test_session_redirect_rpc_drops_self_copy_of_live_prompt():
    calls = []
    session = _session(agent=_steering_agent(calls), inflight_turn=_inflight("original request"))

    resp = _rpc("session.redirect", session, "original request")

    assert calls == []
    assert resp["result"] == {"status": "redirected", "text": "original request"}


def test_session_redirect_rpc_still_applies_a_genuine_correction():
    calls = []
    session = _session(agent=_steering_agent(calls), inflight_turn=_inflight("original request"))

    resp = _rpc("session.redirect", session, "use Postgres")

    assert calls == [("redirect", "use Postgres")]
    assert session["inflight_turn"]["corrections"] == ["use Postgres"]
    assert resp["result"] == {"status": "redirected", "text": "use Postgres"}
