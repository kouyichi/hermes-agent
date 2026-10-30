"""The shell-hook payload's ``cwd`` is the calling session's workspace, not the gateway's own dir.

The gateway process never chdir-es, so ``Path.cwd()`` inside a hook payload was the install root for
every session (#146570) while the plugin API already reported the session workspace through
``hermes_cli.plugin_session.session_cwd()``. ``gateway.session_policy.policy_scope`` binds a turn's
workspace with ``agent.runtime_cwd.set_session_cwd``, which is the same value asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import shell_hooks
from agent.runtime_cwd import clear_session_cwd, reset_session_cwd, set_session_cwd


@pytest.fixture(autouse=True)
def _reset_registration_state():
    shell_hooks.reset_for_tests()
    yield
    shell_hooks.reset_for_tests()


@pytest.fixture
def session_workspace(tmp_path: Path):
    """A bound turn workspace — what ``policy_scope`` installs for the duration of a turn."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    token = set_session_cwd(str(workspace))
    yield workspace
    reset_session_cwd(token)
    clear_session_cwd()


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_payload_cwd_is_the_bound_session_workspace(session_workspace: Path) -> None:
    payload = json.loads(shell_hooks._serialize_payload("pre_tool_call", {"session_id": "sess-1"}))
    assert payload["cwd"] == str(session_workspace)


def test_payload_cwd_matches_the_plugin_api(session_workspace: Path) -> None:
    """One producer: the payload agrees with what a plugin hook would be told."""
    from hermes_cli.plugin_session import session_cwd

    payload = json.loads(shell_hooks._serialize_payload("pre_tool_call", {"session_id": "sess-1"}))
    assert payload["cwd"] == session_cwd() == str(session_workspace)


def test_payload_cwd_falls_back_to_the_process_directory(tmp_path: Path) -> None:
    """No bound turn (bare CLI process, ingress hooks): the launch directory, as before."""
    assert json.loads(shell_hooks._serialize_payload("pre_tool_call", {}))["cwd"] == str(Path.cwd())


def test_spawned_hook_script_receives_the_session_workspace(tmp_path: Path, session_workspace: Path) -> None:
    """Through the real subprocess lane: the script's stdin carries the workspace."""
    capture = tmp_path / "payload.json"
    script = _write_script(
        tmp_path, "capture.sh",
        f"#!/usr/bin/env bash\ncat - > {capture}\nprintf '{{}}\\n'\n",
    )
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=str(script))

    shell_hooks._make_callback(spec)(tool_name="terminal", args={"command": "echo hi"}, session_id="sess-77")

    payload = json.loads(capture.read_text())
    assert payload["session_id"] == "sess-77"
    assert payload["cwd"] == str(session_workspace)
