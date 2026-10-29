"""Aux usage cost estimation must reuse the credential that authenticated the call (#146655).

The reported symptom: with an OpenAI-compatible endpoint that requires a bearer key, every
auxiliary call (title generation, compression) was followed by an unauthenticated
``GET <base_url>/models`` answering 401, so the usage row kept ``estimated_cost_usd`` NULL.
``record_aux_usage`` called ``estimate_usage_cost`` without a key, and the pricing route's
endpoint-metadata probe sends ``Authorization`` only when it is given one.

Two seams are covered: the accounting chokepoint forwards the key it is handed, and the aux
call itself hands it the credential the resolved client authenticates with.
"""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB

BASE_URL = "http://127.0.0.1:4000/v1"


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


@pytest.fixture
def priced(monkeypatch):
    """Capture the kwargs the accounting layer hands the pricing layer."""
    from agent import usage_pricing

    seen = {}

    def _estimate(model_name, usage, *, provider=None, base_url=None, api_key=None):
        seen.update(model=model_name, provider=provider, base_url=base_url, api_key=api_key)
        return SimpleNamespace(amount_usd=None)

    monkeypatch.setattr(usage_pricing, "estimate_usage_cost", _estimate)
    return seen


def _response(model="aux-model", prompt=100, completion=20):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion,
        ),
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
    )


def _record(db, **hints):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context
    from agent.auxiliary_client import _validate_llm_response

    db.create_session("s1", source="cli")
    token = set_accounting_context(db, "s1")
    try:
        assert _validate_llm_response(_response(), "title", **hints) is not None
    finally:
        reset_accounting_context(token)


def test_validate_llm_response_forwards_the_api_key(db, priced):
    _record(db, provider="custom", base_url=BASE_URL, api_key="sk-live-1")
    assert priced["api_key"] == "sk-live-1"
    assert priced["base_url"] == BASE_URL


def test_no_key_still_records_usage_without_a_probe(db, priced):
    """Callers that pass no hint keep working (fallback paths record an empty route)."""
    _record(db, provider="openrouter")
    assert priced["api_key"] is None


def _prepared(client, resolved_api_key):
    from agent import auxiliary_client as ac

    return ac._PreparedAuxRequest(
        client=client, final_model="aux-model", kwargs={},
        resolved_provider="custom", request_provider="custom", resolved_model="aux-model",
        resolved_base_url=BASE_URL, resolved_api_key=resolved_api_key, resolved_api_mode=None,
        effective_timeout=30.0, effective_extra_body={}, base_info=BASE_URL,
    )


@pytest.fixture
def aux_call(monkeypatch):
    """Drive the real sync entry point with a stubbed transport, capturing the hints it forwards."""
    from agent import auxiliary_client as ac

    seen = {}
    state = {}

    def _validate(response, task=None, **hints):
        seen.clear()
        seen.update(hints)
        return "RESPONSE"

    def _install(client, resolved_api_key):
        req = _prepared(client, resolved_api_key)
        monkeypatch.setattr(ac, "_plan_aux_call", lambda *a, **k: (req, {}, {}))
        monkeypatch.setattr(ac, "_relay_sync_completion", lambda *a, **k: "RAW")
        monkeypatch.setattr(ac, "_validate_llm_response", _validate)
        state["call"] = lambda: ac._call_llm_impl(task="title", messages=[])

    return SimpleNamespace(install=_install, seen=seen, state=state)


@pytest.mark.parametrize(
    ("client_key", "resolved_key", "expected"),
    [
        # The resolved SDK client carries the key that authenticates this route.
        ("sk-client", None, "sk-client"),
        ("sk-client", "sk-config", "sk-client"),
        # Adapter clients that expose no ``api_key`` fall back to the resolved route's key.
        ("", "sk-config", "sk-config"),
    ],
)
def test_aux_call_hands_the_client_key_to_accounting(aux_call, client_key, resolved_key, expected):
    client = SimpleNamespace(api_key=client_key, base_url=BASE_URL)
    aux_call.install(client, resolved_key)
    assert aux_call.state["call"]() == "RESPONSE"
    assert aux_call.seen["api_key"] == expected
    assert aux_call.seen["base_url"] == BASE_URL
