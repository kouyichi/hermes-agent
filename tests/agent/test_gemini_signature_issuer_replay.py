"""A Gemini thought signature is replayed only to the upstream that minted it (#141770).

A turn that falls back to a ``vertex`` route stores the signatures Vertex returned in history. The
replay gate only asked whether the TARGET looked like Gemini, so from the next turn on the restored
primary — a custom OpenAI-compatible gateway serving a model whose id contains ``gemini`` — received
Vertex-minted signatures, rejected the request with HTTP 422 ("signed or encrypted reasoning from
another provider has no representation in Gemini generateContent"), and re-activated the fallback.
Every later turn repeated it, so one fallback pinned the session to the fallback for good.

Stored history keeps every signature (a later return to the minting upstream still replays it); the
stamp only decides which wire copy carries it.
"""

from types import SimpleNamespace

import pytest

from agent.reasoning_carriers import (
    CARRIER_TYPE,
    SIGNATURE_ROUTE_KEY,
    carrier_record,
    shape_wire_carriers,
    signature_route,
)
from agent.transports.chat_completions import ChatCompletionsTransport

SIG = "minted-by-vertex"
VERTEX_BASE = "https://aiplatform.googleapis.com/v1"
GATEWAY_BASE = "https://gateway.example/v1"


def _stamped_history(minted_at: str) -> list[dict]:
    """One tool turn plus one text turn whose signatures were minted at ``minted_at``."""
    route = signature_route(minted_at)
    return [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "terminal", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": SIG}},
            SIGNATURE_ROUTE_KEY: route}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "Done.", "reasoning_details": [
            {"type": CARRIER_TYPE, "extra_content": {"google": {"thought_signature": SIG}},
             SIGNATURE_ROUTE_KEY: route}]},
        {"role": "user", "content": "next"},
    ]


def _wire(history: list[dict], *, model: str, base_url: str, provider: str = "custom") -> list[dict]:
    shaped = shape_wire_carriers(history, model=model, base_url=base_url)
    return ChatCompletionsTransport().convert_messages(
        shaped, model=model, base_url=base_url, provider=provider)


def _signature_count(wire: list[dict]) -> int:
    return sum(str(message).count(SIG) for message in wire)


def test_a_signature_minted_elsewhere_is_not_replayed_to_a_gemini_looking_route():
    wire = _wire(_stamped_history(VERTEX_BASE), model="gemini-3.8-flash", base_url=GATEWAY_BASE)

    assert _signature_count(wire) == 0, "a foreign upstream's signature reached the primary"


def test_the_minting_route_still_reads_its_own_signature():
    wire = _wire(_stamped_history(VERTEX_BASE), model="gemini-3.8-flash", base_url=VERTEX_BASE,
                 provider="vertex")

    assert _signature_count(wire) == 2


@pytest.mark.parametrize("base_url", [GATEWAY_BASE])
def test_unstamped_history_keeps_the_previous_replay(base_url):
    """Rows written before the stamp existed cannot be attributed, so they replay as they always did."""
    legacy = [dict(message) for message in _stamped_history(VERTEX_BASE)]
    legacy[1]["tool_calls"] = [{k: v for k, v in legacy[1]["tool_calls"][0].items()
                                if k != SIGNATURE_ROUTE_KEY}]
    legacy[3]["reasoning_details"] = [{k: v for k, v in legacy[3]["reasoning_details"][0].items()
                                       if k != SIGNATURE_ROUTE_KEY}]

    wire = _wire(legacy, model="gemini-3.8-flash", base_url=base_url)

    assert _signature_count(wire) == 2


def test_the_carrier_record_stamps_the_route_that_minted_the_signature():
    message = SimpleNamespace(extra_content={"google": {"thought_signature": SIG}})

    record = carrier_record(message, route=signature_route(VERTEX_BASE))

    assert record["extra_content"] == {"google": {"thought_signature": SIG}}
    assert record[SIGNATURE_ROUTE_KEY] == "aiplatform.googleapis.com"


def test_a_captured_tool_call_carries_the_stamp():
    from agent.chat_completion_helpers import _assistant_tool_call_dict

    agent = SimpleNamespace(
        provider="vertex", base_url=VERTEX_BASE, model="google/gemini-3.8-flash",
        _split_responses_tool_id=lambda raw: (raw, None),
        _derive_responses_function_call_id=lambda call_id, item_id: item_id,
        _deterministic_call_id=lambda name, arguments, index: "call_stub",
    )
    tool_call = SimpleNamespace(
        id="c1", call_id="c1", type="function",
        function=SimpleNamespace(name="terminal", arguments="{}"),
        extra_content={"google": {"thought_signature": SIG}},
    )

    stored = _assistant_tool_call_dict(agent, tool_call, 0)

    assert stored["extra_content"] == {"google": {"thought_signature": SIG}}
    assert stored[SIGNATURE_ROUTE_KEY] == "aiplatform.googleapis.com"


def test_the_stamp_never_reaches_the_wire():
    wire = _wire(_stamped_history(VERTEX_BASE), model="gemini-3.8-flash", base_url=VERTEX_BASE,
                 provider="vertex")

    assert SIGNATURE_ROUTE_KEY not in str(wire)
