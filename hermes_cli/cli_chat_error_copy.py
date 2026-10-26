"""Plain-language copy for chat-turn failures shown in the CLI response panel.

The chat panel used to echo ``Error: HTTP 401: Invalid API key`` as the assistant's answer. These
helpers map the classifier verdict (``agent/error_classifier.py``) to WHAT happened + WHAT TO DO,
and demote the raw provider text to a ``Details:`` line. Copy lives in the i18n catalog under
``cli.error.*`` and is resolved at render time for the active language.
"""

from __future__ import annotations

from typing import Any

from agent.i18n import t

_SUMMARY_LIMIT = 120

# FailoverReason.value -> catalog key suffix. Reasons sharing copy point at one key.
_REASON_KEYS: dict[str, str] = {
    "auth": "auth",
    "auth_permanent": "auth",
    "billing": "billing",
    "model_not_found": "model_not_found",
    "rate_limit": "rate_limit",
    "upstream_rate_limit": "rate_limit",
    "upstream_blocked": "upstream_blocked",
    "overloaded": "overloaded",
    "server_error": "server_error",
    "timeout": "timeout",
}
_UNKNOWN_KEY = "unknown"


def _short(text: str, limit: int | None = _SUMMARY_LIMIT) -> str:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return first if limit is None or len(first) <= limit else first[: limit - 1].rstrip() + "…"


def refusal_text(error: BaseException | str) -> str:
    """The refusal as the client shows it: the reason code, plus the check that fired when the owner
    named one (``invalid_params (toolsets: unknown or invalid toolset name: 'mcp-codegraph')``).

    The gateway sends two spellings of that naming in the JSON-RPC error's ``data``: handler-owned
    refusals carry the singular ``field``/``detail`` (``gateway/session_controls.py``), while the
    canonical dispatcher's closed-key-set refusal carries ``fields`` — a list of key paths
    (``canonical_param_problems``). Both are read here, so a refusal names its check whichever lane
    it came from. A refusal with no ``data`` reads exactly as before."""
    if str(error) == "launch_credentials_unavailable":
        # The chat's launch key changed: every retry repeats the refusal, so name the way out.
        from agent.turn_failure_copy import launch_credentials_copy

        return launch_credentials_copy(error)
    data = getattr(error, "data", None)
    data = data if isinstance(data, dict) else {}
    field, detail = str(data.get("field") or "").strip(), str(data.get("detail") or "").strip()
    if not field:
        field, detail = _fields_refusal(data.get("fields"), detail)
    if not field and not detail:
        return str(error)
    return f"{error} ({field or 'unnamed field'}{': ' + detail if detail else ''})"


def _fields_refusal(fields: Any, detail: str) -> tuple[str, str]:
    """The canonical dispatcher's aggregate refusal as the singular pair: the refused key paths,
    comma-joined when several were refused, plus the wire's own detail when it sent one. The
    dispatcher sends key paths only and never a detail, so joining them here keeps the panel from
    reading ``(model: unknown or refused keys: model, toolsets)``."""
    names = [str(item).strip() for item in fields if str(item).strip()] if isinstance(fields, list) else []
    return ", ".join(names), detail


def chat_error_response(
    error: Exception | str, *, provider: str = "", model: str = "", failure_reason: str | None = None,
) -> str:
    """Two-line panel text: plain sentence with the fix command, then ``Details: <raw>``.

    ``failure_reason`` is the verdict the turn loop already stamped on its result
    (``agent/turn_failure_copy.stamp_failure``). When present it is used as-is instead of
    re-classifying a summarised string (which has no status code and almost always lands on
    'unknown'); for the loop's own site codes the ``error`` text is already the user-facing copy,
    so it is returned verbatim rather than wrapped and demoted to a Details line."""
    reason = str(failure_reason or "").strip()
    if reason:
        from agent.turn_failure_copy import SITE_FAILURE_CODES

        text = str(error or "").strip()
        if reason in SITE_FAILURE_CODES and text:
            return text
    else:
        from agent.error_classifier import classify_api_error

        exc = error if isinstance(error, Exception) else Exception(str(error))
        reason = classify_api_error(exc, provider=provider or "", model=model or "").reason.value
    lead = t(
        f"cli.error.{_REASON_KEYS.get(reason, _UNKNOWN_KEY)}",
        provider=provider or t("cli.error.the_provider"),
        model=model or t("cli.error.the_current_model"),
    )
    return t("cli.error.with_details", lead=lead, details=_short(refusal_text(error), 300))


def agent_init_failure_message(error: BaseException) -> str:
    """Copy for a failed AIAgent build on first message: the user's turn was dropped.

    A gateway that cannot be reached keeps its whole first line: its discovery detail is already
    bounded (``DiscoveryError.bounded_detail``) and ends in the repair command, which the summary
    limit would cut mid-path.

    A ``session.create``/``resume`` refusal (``invalid_params`` from the owner) never reached a
    provider, so it takes its own line: the agent-init copy sends the user to ``hermes doctor`` and
    ``/model``, neither of which can fix a launch-policy refusal (#145286)."""
    from hermes_cli.gateway_client import GatewayRPCError, GatewayUnavailableError

    limit = None if isinstance(error, GatewayUnavailableError) else _SUMMARY_LIMIT
    refusal = _short(refusal_text(error), limit) or type(error).__name__
    key = "cli.error.session_refused" if _is_params_refusal(error) else "cli.error.agent_init_failed"
    return t(key, error=refusal)


def _is_params_refusal(error: BaseException) -> bool:
    """The owner's bounded ``invalid_params``: the session request was refused by a launch check
    (the canonical key set or a policy check) rather than by a provider."""
    from hermes_cli.gateway_client import GatewayRPCError

    return isinstance(error, GatewayRPCError) and str(error) == "invalid_params"
