"""``max_tokens`` must never exceed the model's own output ceiling.

Anthropic rejects ``max_tokens`` above the model's declared output limit
(``invalid_params: max_tokens: 131072 > 128000``), and the adapter only
clamped an explicit value against ``context_length`` — which is the *total*
window (131072 on the same models), so the clamp was a no-op for exactly the
value that overflows. The ceiling the adapter already uses when
``max_tokens=None`` is the bound: asking for more than the adapter itself
would send can only buy a 400.
"""

from agent.anthropic_adapter import (
    _ANTHROPIC_DEFAULT_OUTPUT_LIMIT,
    _get_anthropic_max_output,
    build_anthropic_kwargs,
)

MESSAGES = [{"role": "user", "content": "hi"}]


def _max_tokens(model: str, max_tokens, context_length=None) -> int:
    kwargs = build_anthropic_kwargs(
        model=model,
        messages=list(MESSAGES),
        tools=None,
        max_tokens=max_tokens,
        reasoning_config=None,
        context_length=context_length,
    )

    return kwargs["max_tokens"]


def test_context_window_sized_max_tokens_is_clamped_to_the_output_ceiling():
    """The reported shape: the context window (131072) forwarded as ``max_tokens``."""
    ceiling = _get_anthropic_max_output("claude-sonnet-5")

    assert ceiling == 128_000

    assert _max_tokens("claude-sonnet-5", 131_072, context_length=131_072) == ceiling


def test_context_clamp_cannot_land_above_the_output_ceiling():
    """``context_length - 1`` is not a bound: 131071 > the 128000 output limit."""
    assert _max_tokens("claude-sonnet-5", 200_000, context_length=131_072) == 128_000


def test_fractional_overshoot_is_clamped_too():
    """One token over the ceiling is still a rejection upstream."""
    assert _max_tokens("claude-sonnet-5", 128_001) == 128_000


def test_value_below_the_ceiling_is_sent_untouched():
    """A deliberate small output cap is the caller's, not ours to raise."""
    assert _max_tokens("claude-sonnet-5", 64_000) == 64_000
    assert _max_tokens("claude-3-5-sonnet", 4_096) == 4_096


def test_none_still_resolves_to_the_model_ceiling():
    assert _max_tokens("claude-sonnet-5", None) == 128_000


def test_unknown_model_is_bounded_by_the_adapter_default_ceiling():
    """An unrecognised slug is bounded by the same value ``max_tokens=None``
    resolves to, so the clamp can never be more restrictive than the default."""
    assert _max_tokens("some-anthropic-compat-model", 200_000) == _ANTHROPIC_DEFAULT_OUTPUT_LIMIT


def test_third_party_ceiling_wins_over_the_default():
    """``qwen3`` on DashScope allows 65536; the adapter's own ceiling still bounds it."""
    assert _max_tokens("qwen3.5-plus", 131_072) == _get_anthropic_max_output("qwen3.5-plus")
