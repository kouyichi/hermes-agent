"""A route rebuilt from its session key keeps the slots the key actually encodes.

``build_session_key`` puts the Slack workspace before the chat id and the participant after a
thread, so a positional read of the key reports the scope id as ``chat_id`` and drops the
participant. A completion wake whose routing origin was pruned rebuilds its source from that key
(#144006), re-keys to a different route and is refused with ``admission_conflict`` forever
(#147196/#147197).
"""
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner, _parse_session_key
from gateway.session import SessionSource, build_session_key
from gateway.session_authority import SessionAuthority, initialize_session_authority

# Shapes ``build_session_key`` emits, read back.
PARSED = [
    (
        "agent:main:slack:dm:T01234ABC:D0BJTDCSR7C",
        {"platform": "slack", "chat_type": "dm", "chat_id": "D0BJTDCSR7C", "scope_id": "T01234ABC"},
    ),
    (
        "agent:main:slack:group:T01234ABC:C123",
        {"platform": "slack", "chat_type": "group", "chat_id": "C123", "scope_id": "T01234ABC"},
    ),
    (
        "agent:main:slack:group:T01234ABC:C123:1700000000.000001",
        {
            "platform": "slack",
            "chat_type": "group",
            "chat_id": "C123",
            "scope_id": "T01234ABC",
            "thread_id": "1700000000.000001",
        },
    ),
    (
        "agent:main:slack:group:T01234ABC:C123:1700000000.000001:U9",
        {
            "platform": "slack",
            "chat_type": "group",
            "chat_id": "C123",
            "scope_id": "T01234ABC",
            "thread_id": "1700000000.000001",
            "user_id": "U9",
        },
    ),
    (
        "agent:work:slack:dm:T01234ABC:D0BJTDCSR7C",
        {
            "profile": "work",
            "platform": "slack",
            "chat_type": "dm",
            "chat_id": "D0BJTDCSR7C",
            "scope_id": "T01234ABC",
        },
    ),
    (
        "agent:main:discord:thread:7:42:99",
        {"platform": "discord", "chat_type": "thread", "chat_id": "7", "thread_id": "42", "user_id": "99"},
    ),
]

# Keys the fix must not re-read: an unscoped Slack thread puts its stamp where a workspace slot
# would sit, so a "one extra part means scope" rule would deliver to the stamp.
PRESERVED = [
    (
        "agent:main:slack:dm:D0BJTDCSR7C:1786298425.877239",
        {
            "platform": "slack",
            "chat_type": "dm",
            "chat_id": "D0BJTDCSR7C",
            "thread_id": "1786298425.877239",
        },
    ),
    ("agent:main:slack:dm:D123", {"platform": "slack", "chat_type": "dm", "chat_id": "D123"}),
    ("agent:main:slack:channel:C123:171717", {"platform": "slack", "chat_type": "channel", "chat_id": "C123"}),
    (
        "agent:work:discord:group:chan1:user9",
        {"profile": "work", "platform": "discord", "chat_type": "group", "chat_id": "chan1"},
    ),
    (
        "agent:main:telegram:dm:123:42",
        {"platform": "telegram", "chat_type": "dm", "chat_id": "123", "thread_id": "42"},
    ),
]


@pytest.mark.parametrize("key, expected", PARSED)
def test_parse_session_key_reads_the_slots_the_key_encodes(key, expected):
    assert _parse_session_key(key) == expected


@pytest.mark.parametrize("key, expected", PRESERVED)
def test_parse_session_key_leaves_the_shapes_it_already_read(key, expected):
    assert _parse_session_key(key) == expected


def _source(parsed: dict) -> SessionSource:
    """The source ``build_session_key`` would have keyed from *parsed*'s fields."""
    return SessionSource(
        platform=Platform(parsed["platform"]),
        chat_id=parsed["chat_id"],
        chat_type=parsed["chat_type"],
        thread_id=parsed.get("thread_id"),
        user_id=parsed.get("user_id"),
        scope_id=parsed.get("scope_id"),
    )


@pytest.mark.parametrize("key, _expected", PARSED)
def test_a_parsed_key_rebuilds_its_own_route(key, _expected):
    """The invariant behind the wake rebuild: parse then build returns the same key."""
    parsed = _parse_session_key(key)
    assert parsed is not None
    rebuilt = build_session_key(
        _source(parsed), thread_sessions_per_user=True, profile=parsed.get("profile")
    )
    assert rebuilt == key


def _runner(tmp_path, monkeypatch, **config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(SessionAuthority, "_schedule", lambda self, ref: None)
    monkeypatch.setattr("gateway.session_bot._watch_reply", lambda *args: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    vars(runner.session_store.config).update(config)
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    runner.session_store._db = db
    runner._session_db = db
    return runner, db


async def _authority(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch)
    authority = await initialize_session_authority(
        runner, profile_id="default", instance_id="session-key-parse", db=db
    )
    return runner, authority


@pytest.mark.parametrize(
    "key, extra",
    [
        # A scoped Slack DM: without the workspace slot the rebuild keys the workspace as the chat.
        ("agent:main:slack:dm:T01234ABC:D0BJTDCSR7C", {"platform": "slack", "chat_type": "dm"}),
        # A Slack thread reply is keyed under the channel's chat_type; the stamp is the thread slot.
        (
            "agent:main:slack:group:T01234ABC:C123:1700000000.000001",
            {"platform": "slack", "chat_type": "group"},
        ),
        # A per-user thread route the delegation ledger kept only as a key.
        (
            "agent:main:discord:thread:7:42:99",
            {"platform": "discord", "chat_type": "thread"},
        ),
    ],
)
def test_a_wake_source_rebuilt_from_its_key_rekeys_to_its_own_route(tmp_path, monkeypatch, key, extra):
    runner, db = _runner(tmp_path, monkeypatch, group_sessions_per_user=True, thread_sessions_per_user=True)
    try:
        evt = {"type": "completion", "session_id": "proc_x", "session_key": key, **extra}
        source = runner._build_process_event_source(evt)
        assert source is not None
        assert runner.session_store._generate_session_key(source) == key
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_per_user_thread_wake_is_admitted_instead_of_refused(tmp_path, monkeypatch):
    """End to end: the pruned route's completion still lands in the route it was started from."""
    from gateway.session_ingress_context import native_callback, register_transport_home
    from plugins.platforms.discord.adapter import DiscordAdapter

    runner, authority = await _authority(tmp_path, monkeypatch)
    vars(runner.session_store.config).update({"thread_sessions_per_user": True})
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    runner._wire_adapter_handlers(adapter)
    register_transport_home(runner, None, tmp_path)
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "99")
    source = adapter.build_source(chat_id="7", chat_type="thread", thread_id="42", user_id="99",
                                  user_name="human")
    human = MessageEvent(
        text="human",
        source=adapter.build_source(chat_id="7", chat_type="thread", thread_id="42", user_id="99",
                                    user_name="human", message_id="human-1"),
        message_id="human-1",
    )
    try:
        with native_callback(runner, human, tmp_path):
            await authority.admit_native(human)
        route = runner.session_store._generate_session_key(source)
        assert route == "agent:main:discord:thread:7:42:99"
        runner.session_store._entries[route].origin = None  # the routing origin was pruned (#144006)
        evt = {
            "type": "completion",
            "session_id": "proc_x",
            "session_key": route,
            "platform": "discord",
            "chat_id": "7",
            "chat_type": "thread",
            "exit_code": 0,
            "output": "done",
            "started_at": 1.0,
            "parent_session_id": runner.session_store.peek_session_id(route),
        }
        assert await runner._inject_watch_notification("[done]", evt) is True
    finally:
        authority.db.close()
