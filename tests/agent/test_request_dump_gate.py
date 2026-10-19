"""``logging.request_dumps`` gates the provider-error request dump (#141277).

Each dump captures the whole request body — the prompt included — into ``<home>/sessions/``, a
copy that sits outside the ``state.db`` retention policy until its session row is pruned. These
pin the switch: ``false`` writes nothing, an absent key keeps the historical dump, and a config
that cannot be read keeps dumping (fail-open) rather than silencing diagnostics.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.agent_runtime_helpers import dump_api_request_debug
from agent.request_dumps import request_dumps_enabled

_UNSET = object()


def _agent(tmp_path):
    return SimpleNamespace(
        api_mode="chat_completions",
        client=SimpleNamespace(api_key="sk-test-key"),
        base_url="https://provider.example.com/v1",
        session_id="sess-141277",
        logs_dir=tmp_path,
        log_prefix="[test] ",
        verbose_logging=False,
        _vprint=lambda *_a, **_k: None,
        _mask_api_key_for_logs=lambda key: "***" if key else "None",
    )


def _config(monkeypatch, cfg):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)


def _dumps(tmp_path):
    return sorted(tmp_path.glob("request_dump_*.json"))


def _requests():
    return {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}


def test_disabled_switch_writes_no_dump(monkeypatch, tmp_path):
    _config(monkeypatch, {"logging": {"request_dumps": False}})

    written = dump_api_request_debug(_agent(tmp_path), _requests(), reason="non_retryable_client_error")

    assert written is None
    assert _dumps(tmp_path) == []


def test_string_false_disables_the_dump(monkeypatch, tmp_path):
    """``hermes config set logging.request_dumps false`` can land as the string, not a bool."""
    _config(monkeypatch, {"logging": {"request_dumps": "false"}})

    assert request_dumps_enabled() is False
    assert dump_api_request_debug(_agent(tmp_path), _requests(), reason="preflight") is None
    assert _dumps(tmp_path) == []


def test_default_config_keeps_dumping(monkeypatch, tmp_path):
    """No ``request_dumps`` key (the shipped default) must keep today's behaviour."""
    _config(monkeypatch, {"logging": {"level": "INFO"}})

    written = dump_api_request_debug(_agent(tmp_path), _requests(), reason="preflight")

    assert written is not None and written.exists()
    payload = json.loads(written.read_text(encoding="utf-8-sig"))
    assert payload["request"]["body"]["messages"][0]["content"] == "hello"
    assert payload["reason"] == "preflight"


def test_dump_still_carries_the_error_block(monkeypatch, tmp_path):
    _config(monkeypatch, {})

    class _ProviderError(Exception):
        status_code = 400

    written = dump_api_request_debug(
        _agent(tmp_path), _requests(), reason="non_retryable_client_error", error=_ProviderError("bad request")
    )

    assert written is not None
    payload = json.loads(written.read_text(encoding="utf-8-sig"))
    assert payload["error"]["status_code"] == 400
    assert payload["error"]["type"] == "_ProviderError"


def test_unreadable_config_keeps_dumping(monkeypatch, tmp_path):
    def _boom():
        raise RuntimeError("config is unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", _boom)

    assert request_dumps_enabled() is True
    assert dump_api_request_debug(_agent(tmp_path), _requests(), reason="preflight") is not None


def test_only_the_logging_section_governs_the_switch(monkeypatch):
    _config(monkeypatch, {"logging": {"request_dumps": True}, "sessions": {"request_dumps": False}})
    assert request_dumps_enabled() is True

    _config(monkeypatch, {"logging": "not-a-mapping"})
    assert request_dumps_enabled() is True
