"""A task's ``images`` that live only in a non-local terminal backend still reach the child.

``_build_child_goal_message`` reads image paths off the HOST, which is right for a local backend and wrong
for a Docker/sandbox one: the path that ``vision_analyze`` opens fine inside the container is not a host
file, so the attachment was dropped and the caller saw only a ``skipped ... unreadable image(s)`` log line
(#146982). These tests pin the fixed behaviour: sandbox paths are read through the shared media resolver
before routing, attach natively (or hint text-mode children) under their ORIGINAL path, and a local backend
still treats a missing path as a missing file.
"""

import base64

import pytest
from typing import Any
from unittest.mock import patch

from tools.delegate_tool_child_run import _ChildRun, _build_child_goal_message

try:  # the helper this fix adds: importing it plainly would turn the sabotage run into a collection error
    from tools.delegate_tool_child_run import _read_backend_images
except ImportError:  # pragma: no cover - only on a tree without the fix
    _read_backend_images = None

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)
_SANDBOX_PATH = "/root/tmpx/review.png"  # never exists on the host in these tests


class _FakeChild:
    provider = "openrouter"
    model = "some/vision-model"


class _ResolvedImage:
    def __init__(self, data: bytes, mime: str, origin: str = "container"):
        self.data, self.mime, self.origin = data, mime, origin


async def _fake_resolve(src, ctx, *, permitted=("image",)):
    assert src == _SANDBOX_PATH
    assert ctx.task_id == "parent-task"
    assert permitted == ("image",)
    return _ResolvedImage(_PNG, "image/png")


async def _refusing_resolve(src, ctx, *, permitted=("image",)):
    from tools.image_source import SourceNotFound

    raise SourceNotFound(f"media file not found: '{src}'", src=src, origin="file")


def _backend_env(resolver):
    return (
        patch("tools.image_source._is_local_terminal_backend", return_value=False),
        patch("tools.image_source.resolve_image_source", side_effect=resolver),
    )


def _require_helper() -> Any:
    """The helper under test, or a skip on a tree without the fix (keeps the sabotage run behavioural)."""
    if _read_backend_images is None:
        pytest.skip("_read_backend_images is not on this tree")
    return _read_backend_images


def test_read_backend_images_returns_a_data_url_and_the_original_path():
    read_backend_images = _require_helper()
    local, resolver = _backend_env(_fake_resolve)
    with local, resolver:
        resolved, unreadable = read_backend_images([_SANDBOX_PATH], "parent-task")
    assert unreadable == []
    assert len(resolved) == 1
    data_url, path = resolved[0]
    assert path == _SANDBOX_PATH
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == _PNG


def test_read_backend_images_never_reads_a_host_path_under_a_local_backend():
    read_backend_images = _require_helper()
    with patch("tools.image_source._is_local_terminal_backend", return_value=True), \
            patch("tools.image_source.resolve_image_source", side_effect=AssertionError("must not resolve")):
        assert read_backend_images(["/tmp/missing.png"], "parent-task") == ([], [])


def test_an_unreadable_sandbox_path_is_reported_and_falls_back_to_a_text_goal():
    read_backend_images = _require_helper()
    local, resolver = _backend_env(_refusing_resolve)
    with local, resolver:
        resolved, unreadable = read_backend_images([_SANDBOX_PATH], "parent-task")
    assert resolved == [] and unreadable == [_SANDBOX_PATH]
    with patch("agent.image_routing.decide_image_input_mode", return_value="native"):
        assert _build_child_goal_message("Goal", [_SANDBOX_PATH], _FakeChild(), backend_images=resolved) == "Goal"


def test_a_native_child_gets_the_sandbox_pixels_and_the_sandbox_path_hint():
    backend = [("data:image/png;base64," + base64.b64encode(_PNG).decode("ascii"), _SANDBOX_PATH)]
    with patch("agent.image_routing.decide_image_input_mode", return_value="native"):
        parts = _build_child_goal_message("Look at the screenshot", [_SANDBOX_PATH], _FakeChild(), backend_images=backend)
    assert isinstance(parts, list)
    assert [p["type"] for p in parts].count("image_url") == 1
    assert parts[1]["image_url"]["url"] == backend[0][0]
    # The hint keeps the path the CHILD can open (its own backend), not a host copy.
    assert f"[Image attached at: {_SANDBOX_PATH}]" in parts[0]["text"]


def test_a_text_mode_child_gets_a_hint_for_a_sandbox_path_it_can_open():
    backend = [("data:image/png;base64," + base64.b64encode(_PNG).decode("ascii"), _SANDBOX_PATH)]
    with patch("agent.image_routing.decide_image_input_mode", return_value="text"):
        msg = _build_child_goal_message("Inspect it", [_SANDBOX_PATH], _FakeChild(), backend_images=backend)
    assert isinstance(msg, str)
    assert f"[Image attached at: {_SANDBOX_PATH}]" in msg and "vision_analyze" in msg


def test_child_run_forwards_a_sandbox_image_to_the_first_turn():
    """End to end through ``await_child``: on the unfixed tree the goal turn carried no image at all."""
    seen = {}

    class _Child(_FakeChild):
        _delegate_images = [_SANDBOX_PATH]

        def run_conversation(self, user_message, **kw):
            seen["user_message"] = user_message
            return {"final_response": "ok", "completed": True}

    class _Parent:
        _current_task_id = "parent-task"

    run = _ChildRun(_Child(), _Parent(), 0, "Describe the review", None, None)
    local, resolver = _backend_env(_fake_resolve)
    with patch("tools.delegate_tool_child_run._create_isolated_worktree", return_value=None), \
            patch("agent.image_routing.decide_image_input_mode", return_value="native"), local, resolver:
        run.seed_workspace()
        result, failure, _ = run.await_child()
    assert failure is None and result["final_response"] == "ok"
    content = seen["user_message"]
    assert isinstance(content, list), content
    assert [p["type"] for p in content].count("image_url") == 1
    assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]) == _PNG
    assert f"[Image attached at: {_SANDBOX_PATH}]" in content[0]["text"]
