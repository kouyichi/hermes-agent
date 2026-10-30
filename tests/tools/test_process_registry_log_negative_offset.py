"""``process_manage`` log pagination with a NEGATIVE offset (#146914).

``read_log`` sliced ``lines[offset:offset + limit]``, so a negative offset with
``limit >= -offset`` (``offset=-80, limit=80``) became ``lines[-80:0]`` — always
empty. Models pass negative offsets to mean "the last N lines", and the tool
schema never said they were unsupported, so the call silently returned
``output: ""`` for a process that had output. A negative offset now counts from
the end, and ``limit`` still pages forward from there.
"""

import json
import time

from tools import process_registry as pr_mod
from tools.process_registry import ProcessRegistry, ProcessSession


def _registry_with(sid: str, output: str, *, exited: bool = False) -> ProcessRegistry:
    registry = ProcessRegistry()
    session = ProcessSession(
        id=sid,
        command="echo hello",
        task_id="t1",
        started_at=time.time(),
        exited=exited,
        exit_code=0 if exited else None,
        output_buffer=output,
    )
    registry._running[sid] = session
    return registry


def _numbered(count: int) -> str:
    return "\n".join(f"line {i}" for i in range(count))


def test_a_negative_offset_returns_the_last_lines() -> None:
    registry = _registry_with("proc_neg", _numbered(100))
    result = registry.read_log("proc_neg", offset=-10, limit=10)
    assert result["output"].splitlines() == [f"line {i}" for i in range(90, 100)]
    assert result["total_lines"] == 100
    assert result["showing"] == "10 lines"


def test_a_negative_offset_larger_than_the_log_is_clamped_to_the_first_line() -> None:
    # The reported shape: a one-line process read with offset=-200, limit=200
    # answered output: "" and showing: "0 lines".
    registry = _registry_with("proc_small", "only line")
    result = registry.read_log("proc_small", offset=-200, limit=200)
    assert result["output"] == "only line"
    assert result["showing"] == "1 lines"


def test_a_negative_offset_counts_from_the_end_then_pages_forward() -> None:
    registry = _registry_with("proc_page", _numbered(100))
    result = registry.read_log("proc_page", offset=-50, limit=10)
    assert result["output"].splitlines() == [f"line {i}" for i in range(50, 60)]


def test_a_negative_offset_at_the_exact_log_length_still_returns_every_line() -> None:
    registry = _registry_with("proc_exact", _numbered(20))
    result = registry.read_log("proc_exact", offset=-20, limit=20)
    assert result["output"].splitlines() == [f"line {i}" for i in range(20)]


def test_a_positive_offset_still_pages_forward() -> None:
    registry = _registry_with("proc_pos", _numbered(100))
    result = registry.read_log("proc_pos", offset=10, limit=5)
    assert result["output"].splitlines() == [f"line {i}" for i in range(10, 15)]


def test_the_tool_handler_returns_the_tail_for_a_negative_offset(monkeypatch) -> None:
    registry = _registry_with("proc_tool", _numbered(30))
    monkeypatch.setattr(pr_mod, "process_registry", registry)
    raw = pr_mod._handle_process({"action": "log", "session_id": "proc_tool", "offset": -5, "limit": 5})
    result = json.loads(raw)
    assert result["output"].splitlines() == [f"line {i}" for i in range(25, 30)]
