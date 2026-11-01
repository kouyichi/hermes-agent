"""Regression tests for NousResearch/hermes-agent#147383.

A memory provider whose ``initialize()`` raises midway (qdrant-memory's ``WriterLease`` raising
``WriterBusyError`` before ``self.ledger`` is assigned) used to stay in ``_providers`` and be
reported as activated: every later lifecycle hook and tool call then hit the attribute its
``initialize()`` body never reached, for the whole lifetime of the process. ``initialize_all`` now
drops it — the same treatment ``claim_instance`` refusals already got — and
``initialize_for_agent``'s user-facing warning names the real reason.
"""

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class HalfBuiltMemoryProvider(MemoryProvider):
    """Provider whose ``initialize()`` raises partway; its tools stay routable if it is kept."""

    def __init__(self, error: Exception):
        self.error = error
        self.initialized = False
        self.shutdown_called = False

    @property
    def name(self) -> str:
        return "half-built"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id, **kwargs):
        raise self.error

    def get_tool_schemas(self):
        return [{"name": "half_built_search", "description": "search", "parameters": {}}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        raise AttributeError("'HalfBuiltMemoryProvider' object has no attribute 'ledger'")

    def shutdown(self):
        self.shutdown_called = True


class HealthyMemoryProvider(MemoryProvider):
    """The builtin half of the pair: always registered, never the reason for a drop."""

    def __init__(self):
        self.initialized = False

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id, **kwargs):
        self.initialized = True

    def get_tool_schemas(self):
        return []


def _manager_with(half_built: HalfBuiltMemoryProvider, healthy: HealthyMemoryProvider) -> MemoryManager:
    manager = MemoryManager()
    manager.add_provider(healthy)
    manager.add_provider(half_built)
    return manager


def test_failed_initialize_drops_the_provider_and_its_tools(tmp_path):
    """The half-built provider leaves the roster: nothing advertises or routes its tools afterwards."""
    broken = HalfBuiltMemoryProvider(RuntimeError("Another writer still owns this profile destination"))
    healthy = HealthyMemoryProvider()
    manager = _manager_with(broken, healthy)

    assert manager.has_tool("half_built_search")  # registered before initialize
    manager.initialize_for_agent(session_id="s-147383", hermes_home=str(tmp_path))

    assert [p.name for p in manager.providers] == ["builtin"]
    assert not manager.has_tool("half_built_search")
    assert healthy.initialized


def test_failed_initialize_is_reported_with_its_error(tmp_path):
    """The startup warning names the initialize failure and its error, not a profile conflict."""
    broken = HalfBuiltMemoryProvider(RuntimeError("Another writer still owns this profile destination"))
    manager = _manager_with(broken, HealthyMemoryProvider())
    said: list[str] = []

    assert manager.initialize_for_agent(say=said.append, session_id="s-147383", hermes_home=str(tmp_path)) is False

    assert len(said) == 1
    assert "'half-built'" in said[0]
    assert "failed to initialize" in said[0]
    assert "Another writer still owns this profile destination" in said[0]
    assert "another profile already uses" not in said[0]


def test_failed_initialize_releases_what_it_acquired(tmp_path):
    """A partial initialize may have taken a lease/connection before raising; the drop releases it."""
    broken = HalfBuiltMemoryProvider(RuntimeError("boom"))
    manager = _manager_with(broken, HealthyMemoryProvider())

    manager.initialize_for_agent(session_id="s-147383", hermes_home=str(tmp_path))

    assert broken.shutdown_called


def test_healthy_provider_is_never_dropped(tmp_path):
    """Control: a provider that initializes cleanly keeps serving the session."""
    manager = MemoryManager()
    healthy = HealthyMemoryProvider()
    manager.add_provider(healthy)

    assert manager.initialize_for_agent(session_id="s-147383", hermes_home=str(tmp_path)) is False  # builtin is not external
    assert [p.name for p in manager.providers] == ["builtin"]
    assert healthy.initialized
