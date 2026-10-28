"""A foreign write into the active-session registry must not refuse every new session.

Regression for #145807. ``active_sessions.json`` was found holding one complete JSON document
followed by a 24-byte TLS ``close_notify`` record (a socket closed out from under a live reader,
whose file descriptor the atomic registry write then reused). The strict reader reported the file
as unreadable, so every new gateway session was refused with ``SESSION_COORDINATION_UNAVAILABLE``
until an operator moved the file aside by hand.

What the registry owes its callers: a COMPLETE document is authoritative, and bytes after it are
not part of the document — drop them and keep the fail-closed contract for a file that really is
unparseable (empty, truncated, mid-write). The refusal also has to name its real reason: the
gateway logged every refusal as "max_concurrent_sessions reached", which pointed operators at a
concurrency knob that had nothing to do with it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_cli.active_sessions import (
    SESSION_COORDINATION_UNAVAILABLE,
    ActiveSessionRegistryError,
    _read_entries,
    active_session_limit_message,
    try_acquire_active_session,
)

# 17 03 03 00 13 + 19 bytes: one TLS 1.3 application-data record with a 0x13-byte payload, the
# exact tail captured in the report.
TLS_TAIL = b"\x17\x03\x03\x00\x13" + bytes(range(0x13))


def _registry_path(home: Path) -> Path:
    return home / "runtime" / "active_sessions.json"


def _seed_registry(home, payload: bytes) -> None:
    path = _registry_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _claim(home, session_id: str):
    return try_acquire_active_session(
        session_id=session_id,
        surface="gateway:telegram",
        config=None,
        registry_home=home,
    )


def test_a_complete_document_with_trailing_bytes_is_read(tmp_path):
    _seed_registry(tmp_path, b'{"entries": []}' + TLS_TAIL)

    assert _read_entries(_registry_path(tmp_path), strict=True) == []


def test_trailing_bytes_do_not_drop_a_live_lease(tmp_path):
    lease, refusal = _claim(tmp_path, "agent:main:telegram:dm:a")
    assert refusal is None and lease is not None
    _seed_registry(tmp_path, _registry_path(tmp_path).read_bytes() + TLS_TAIL)

    entries = _read_entries(_registry_path(tmp_path), strict=True)

    assert [entry["session_id"] for entry in entries] == ["agent:main:telegram:dm:a"]


def test_a_new_session_is_admitted_over_a_torn_registry_and_the_file_heals(tmp_path):
    _seed_registry(tmp_path, b'{"entries": []}' + TLS_TAIL)

    lease, refusal = _claim(tmp_path, "agent:main:telegram:dm:b")

    assert refusal is None, refusal
    assert lease is not None and lease.enabled
    # The claim's own write replaces the file wholesale, so the tail is gone afterwards.
    stored = json.loads(_registry_path(tmp_path).read_text(encoding="utf-8"))
    assert [entry["session_id"] for entry in stored["entries"]] == ["agent:main:telegram:dm:b"]


def test_a_truncated_document_stays_unreadable(tmp_path):
    _seed_registry(tmp_path, b'{"entries": [{"lease_id": "a"')

    with pytest.raises(ActiveSessionRegistryError):
        _read_entries(_registry_path(tmp_path), strict=True)

    lease, refusal = _claim(tmp_path, "agent:main:telegram:dm:c")
    assert lease is None
    assert getattr(refusal, "reason", None) == SESSION_COORDINATION_UNAVAILABLE


def _gateway_runner(config):
    import gateway.run as run_mod

    runner = run_mod.GatewayRunner.__new__(run_mod.GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner.config = config
    return runner


def test_the_claim_log_names_the_registry_rather_than_the_session_cap(tmp_path, monkeypatch, caplog):
    """The refusal is a registry problem, so the log must not point at a concurrency knob."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_registry(tmp_path, b'{"entries": [{"lease_id": "a"')
    runner = _gateway_runner({})
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1")

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        lease, refusal = runner._claim_active_session_slot("agent:main:telegram:dm:x", source)

    assert lease is None
    assert getattr(refusal, "reason", None) == SESSION_COORDINATION_UNAVAILABLE
    assert "could not read the active-session registry" in caplog.text
    assert "max_concurrent_sessions reached" not in caplog.text

    # The capacity refusal keeps its own wording: one decision point, two reasons.
    from gateway.run_busy import log_slot_refusal

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        log_slot_refusal("agent:main:telegram:dm:y", active_session_limit_message(2, 2))

    [capped] = [record for record in caplog.records if record.name == "gateway.run"]
    assert capped.levelno == logging.INFO
    assert "max_concurrent_sessions reached" in capped.getMessage()


def test_a_tolerated_registry_keeps_its_entries_readable_after_a_later_write(tmp_path):
    _seed_registry(tmp_path, b'{"entries": []}' + TLS_TAIL)
    first, _ = _claim(tmp_path, "agent:main:telegram:dm:f")
    assert first is not None
    _seed_registry(tmp_path, _registry_path(tmp_path).read_bytes() + TLS_TAIL)
    _second, refusal = _claim(tmp_path, "agent:main:telegram:dm:g")

    assert refusal is None, refusal
    sessions = {entry["session_id"] for entry in _read_entries(_registry_path(tmp_path), strict=True)}
    assert sessions == {"agent:main:telegram:dm:f", "agent:main:telegram:dm:g"}
