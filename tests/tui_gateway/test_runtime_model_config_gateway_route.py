"""A Desktop/TUI model switch must move the nested ``gateway_runtime`` route too.

``_runtime_model_config`` is the persist path for a Desktop/TUI session (``_persist_live_session_runtime``
and ``session.create``). It merges the agent's live identity onto the row's top-level ``model_config``
keys only, while resume reads the NESTED ``gateway_runtime`` dict first
(``SessionDB.session_gateway_runtime``, shared with CLI ``--resume``). A row that already carried a
gateway route therefore kept the provider the user left, and a later resume ran the new model on the old
provider's endpoint (#146345). ``SessionDB.update_session_model`` and the CLI switch already write both
shapes from one route dict, so these tests pin the desktop path to the same rule.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_state import SessionDB
from tui_gateway.agent_factory import _runtime_model_config

NOUS_ROUTE = {
    "provider": "nous",
    "base_url": "https://inference-api.nousresearch.com/v1",
    "api_mode": "chat_completions",
}


def _agent(*, model="claude-opus-5-5", provider="anthropic",
           base_url="https://api.anthropic.com", api_mode="anthropic_messages"):
    return SimpleNamespace(
        model=model,
        provider=provider,
        requested_provider="",
        base_url=base_url,
        api_mode=api_mode,
        reasoning_config=None,
        service_tier=None,
    )


def _row(config: dict) -> dict:
    """The row a resume reads: the model column plus the JSON route the persist path wrote."""
    return {"model": config.get("model"), "model_config": json.dumps(config)}


def test_switched_session_rewrites_the_nested_gateway_route():
    existing = {"model": "openai/gpt-5.6", **NOUS_ROUTE, "gateway_runtime": dict(NOUS_ROUTE)}

    config = _runtime_model_config(_agent(), existing)

    assert config["provider"] == "anthropic"
    assert config["gateway_runtime"] == {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_mode": "anthropic_messages",
    }


def test_resume_reads_the_switched_route_not_the_one_the_user_left():
    existing = {"model": "openai/gpt-5.6", **NOUS_ROUTE, "gateway_runtime": dict(NOUS_ROUTE)}

    route = SessionDB.session_gateway_runtime(_row(_runtime_model_config(_agent(), existing)))

    assert route["provider"] == "anthropic"
    assert route["base_url"] == "https://api.anthropic.com"


def test_a_switch_with_no_new_endpoint_clears_the_previous_route_values():
    """or-None discipline: the left provider's endpoint must not survive a switch (as #79536)."""
    existing = {**NOUS_ROUTE, "gateway_runtime": dict(NOUS_ROUTE)}

    config = _runtime_model_config(
        _agent(model="deepseek-v4-flash", provider="deepseek", base_url="", api_mode=""), existing
    )

    assert config["gateway_runtime"] == {"provider": "deepseek", "base_url": None, "api_mode": None}


def test_a_row_without_a_nested_route_still_gets_none():
    """The pinned contract of this function: it never invents a gateway-only route."""
    existing = {"model": "openai/gpt-5.6", **NOUS_ROUTE}

    config = _runtime_model_config(_agent(), existing)

    assert "gateway_runtime" not in config
    assert config["provider"] == "anthropic"


def test_an_unknown_provider_leaves_the_stored_route_untouched(monkeypatch):
    """Same rule as ``update_session_model``: a caller without provider knowledge keeps the route."""
    monkeypatch.setattr("hermes_cli.runtime_provider_custom.session_provider_identity",
                        lambda *_args, **_kwargs: "")

    config = _runtime_model_config(_agent(provider="", base_url="", api_mode=""),
                                   {"gateway_runtime": dict(NOUS_ROUTE)})

    assert config["gateway_runtime"] == NOUS_ROUTE


def test_a_malformed_nested_route_is_replaced_by_the_known_one():
    config = _runtime_model_config(_agent(), {"gateway_runtime": "nous"})

    assert config["gateway_runtime"] == {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_mode": "anthropic_messages",
    }
