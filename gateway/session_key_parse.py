"""Read a gateway session key back into the route fields it was built from.

``build_session_key`` (``gateway.session``) owns the layout::

    <ns>:<platform>:<chat_type>[:<slack scope_id>][:<chat_id>][:<thread_id>][:<user>]

So a positional read is only correct for the unscoped shapes: the Slack workspace slot shifts
every later slot by one, and a ``thread_sessions_per_user`` route carries a participant after
the thread. Callers that REBUILD a route from its key — a completion wake whose routing origin
was pruned, or a delegation event that never carried per-message metadata — get the wrong
``chat_id``/``user_id`` from a positional read, re-key to a different route, and are refused
with ``admission_conflict`` on every retry (#147196/#147197).
"""
import re

from hermes_state_keys import profile_from_session_key_namespace

_PROFILE_ID_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# ``build_session_key`` appends the workspace slot for Slack alone. The ids it puts there are
# workspace/enterprise ids (``T…``/``E…``), while a chat slot holds a channel/group/DM/user id
# (``C…``/``G…``/``D…``/``U…``) and a thread slot a ``<seconds>.<micros>`` stamp. ID SHAPE is
# what separates the two readings: a bare "extra part means scope" rule would misread an
# unscoped ``slack:dm:<chat>:<stamp>`` key, a real relay shape, and deliver to the stamp.
_SLACK_CHAT_ID_RE = re.compile(r"^[CDGU][A-Z0-9][A-Z0-9_]*$")
_SLACK_SCOPE_ID_RE = re.compile(r"^[TE][A-Z0-9][A-Z0-9_]*$")
# A Slack thread id is the reply's ``<seconds>.<micros>`` message stamp. Slack keys a thread reply
# under the CHANNEL's own chat_type (``group``/``dm``, ``adapter._thread_session_source``), so for a
# Slack key the stamp — not the chat_type — is what marks the thread slot.
_SLACK_THREAD_TS_RE = re.compile(r"^\d+\.\d+$")
_SLACK_USER_ID_RE = re.compile(r"^[UWB][A-Z0-9][A-Z0-9_]*$")


def _slack_scope_leads(rest: list[str]) -> bool:
    """True when the slots after *chat_type* start with a Slack workspace slot."""
    return (
        len(rest) >= 2
        and bool(_SLACK_SCOPE_ID_RE.match(rest[0]))
        and bool(_SLACK_CHAT_ID_RE.match(rest[1]))
    )


def parse_session_key(session_key: str) -> dict | None:
    """Parse a session key (``agent:{ns}:{platform}:{chat_type}...``); None for anything else.

    ``{ns}`` is ``main`` for the default profile, ``main~`` for a profile literally named ``main``
    (``gateway.session._session_key_namespace``), or a named-profile id (profile ids match
    ``[a-z0-9][a-z0-9_-]{0,63}`` — never contain ``:`` — so a plain split stays unambiguous).
    Named profiles are reported as ``profile``; ``main`` keys keep their historical shape exactly
    (no ``profile`` key) so equality assertions on parsed dicts stay stable.

    ``scope_id`` is reported for a scoped Slack key, ``thread_id`` for a thread slot and ``user_id``
    for a participant slot — the fields a rebuilt source needs to re-key to its own route. A
    group/channel key's suffix stays unread on the platforms whose thread and user ids share one
    shape (Discord snowflakes, Telegram ids): there it is as likely a participant as a thread.
    """
    parts = session_key.split(":")
    if not (
        len(parts) >= 5
        and parts[0] == "agent"
        and (parts[1] in ("main", "main~") or _PROFILE_ID_KEY_RE.match(parts[1]))
    ):
        return None
    platform, chat_type = parts[2], parts[3]
    rest = parts[4:]
    result = {"platform": platform, "chat_type": chat_type}
    if parts[1] != "main":
        result["profile"] = profile_from_session_key_namespace(parts[1])
    if platform == "slack" and _slack_scope_leads(rest):
        result["scope_id"] = rest.pop(0)
    result["chat_id"] = rest[0]
    tail = rest[1:]
    if tail and (chat_type in {"dm", "thread"} or (platform == "slack" and _SLACK_THREAD_TS_RE.match(tail[0]))):
        result["thread_id"] = tail[0]
        tail = tail[1:]
    if tail and (chat_type == "thread" or (platform == "slack" and _SLACK_USER_ID_RE.match(tail[0]))):
        result["user_id"] = tail[0]
    return result
