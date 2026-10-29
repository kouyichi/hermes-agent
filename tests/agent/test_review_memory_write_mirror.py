"""Committed background-review memory writes must reach external providers (#146448).

``build_cache_parity_fork`` builds the review fork with ``skip_memory=True`` — an external
provider scoped to the parent's ``session_id`` would otherwise archive the review's harness
prompt into the user's real memory namespace — so the fork carries no ``MemoryManager``. It
still writes USER.md / MEMORY.md through the built-in store the caller re-binds onto it, and
``inline_tool_executors._memory`` mirrors a committed write only when a manager is set. Review
adds, patches and consolidations therefore never reached ``MemoryProvider.on_memory_write``,
so providers that keep a copy of the built-in files drifted until the next session boundary,
and a review consolidation (entries merged to stay under the char limits) was indistinguishable
from a user-driven removal.

The fix binds the PARENT's manager on the fork for mirroring only (``_memory_write_mirror``).
No other hook (``sync_turn`` / ``prefetch`` / ``on_session_end``) is routed, so the harness-prompt
leak that motivated ``skip_memory=True`` stays closed, and ``write_origin=background_review``
gives providers the provenance.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import agent.background_review as bg
from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _RecordingProvider(MemoryProvider):
    """External provider that records the ``on_memory_write`` calls it receives."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return "recording"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def shutdown(self) -> None:
        pass

    def on_memory_write(self, action, target, content, metadata=None):
        self.calls.append({
            "action": action,
            "target": target,
            "content": content,
            "metadata": dict(metadata or {}),
        })


def _manager():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


def _store(tmp_path, monkeypatch):
    from tools.memory_tool_store import MemoryStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store = MemoryStore(memory_char_limit=500, user_char_limit=300)
    store.load_from_disk()
    return store


def _agent(**attrs):
    """Memory-tool agent stub; the caller supplies the store and the manager wiring."""
    agent = SimpleNamespace(**attrs)
    agent._build_memory_write_metadata = lambda **kw: bg.build_memory_write_metadata(agent, **kw)
    return agent


def _write_memory(agent):
    return INLINE_TOOL_EXECUTORS["memory"](
        agent, {"action": "add", "target": "memory", "content": "review-added fact"},
        InlineToolContext(effective_task_id=""),
    )


class TestReviewForkCarriesTheParentManager:
    def test_fork_keeps_skip_memory_and_binds_the_parents_manager_for_mirroring(self):
        """skip_memory stays (no prompt leak); the parent's manager is bound on the fork."""
        import run_agent

        captured: dict = {}

        class _Fork:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        parent = SimpleNamespace(
            model="test-model", platform="cli", provider="openai", session_id="sess-review",
            _cached_system_prompt="PARENT-PROMPT-BYTES", session_start=None, tools=[],
            _memory_store="PARENT-STORE", _memory_enabled=True, _user_profile_enabled=False,
            _memory_manager=MemoryManager(),
        )
        parent._conversation_root_id = lambda: "root-id"

        with patch.object(bg, "_resolve_review_runtime", lambda agent, task_cfg=None: {}), \
                patch.object(run_agent, "AIAgent", _Fork):
            fork, _rt, _routed = bg.build_cache_parity_fork(parent, max_iterations=3)

        assert captured["skip_memory"] is True, "the review fork must stay memory-provider-free"
        assert getattr(fork, "_memory_write_mirror", None) is parent._memory_manager
        assert fork._memory_write_origin == "background_review"


class TestReviewForkMemoryToolMirrors:
    def test_review_write_reaches_the_provider_with_review_provenance(self, tmp_path, monkeypatch):
        mgr, provider = _manager()
        agent = _agent(
            _memory_store=_store(tmp_path, monkeypatch), _memory_manager=None,
            _memory_write_mirror=mgr, session_id="sess-review", _parent_session_id=None,
            platform="cli", _memory_write_origin="background_review",
            _memory_write_context="background_review",
        )

        result = json.loads(_write_memory(agent))

        assert result["success"] is True
        assert len(provider.calls) == 1, "a committed review write must be mirrored once"
        call = provider.calls[0]
        assert call["action"] == "add"
        assert call["content"] == "review-added fact"
        assert call["metadata"]["write_origin"] == "background_review"
        assert call["metadata"]["execution_context"] == "background_review"
        assert call["metadata"]["session_id"] == "sess-review"

    def test_foreground_agent_still_mirrors_through_its_own_manager(self, tmp_path, monkeypatch):
        """Control: the ordinary agent path is unchanged (mirror attr absent, own manager used)."""
        mgr, provider = _manager()
        agent = _agent(
            _memory_store=_store(tmp_path, monkeypatch), _memory_manager=mgr,
            session_id="sess-foreground", _parent_session_id=None, platform="cli",
            _memory_write_origin="assistant_tool",
        )

        assert json.loads(_write_memory(agent))["success"] is True

        assert len(provider.calls) == 1
        assert provider.calls[0]["metadata"]["write_origin"] == "assistant_tool"
