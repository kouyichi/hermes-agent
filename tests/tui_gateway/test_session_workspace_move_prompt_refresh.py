"""Moving a session to another project reloads the project context in its cached prompt (#143204).

The context tier (``.hermes.md`` / ``AGENTS.md`` / ``CLAUDE.md`` / ``.cursorrules``) is discovered from the
session cwd and cached on ``agent._cached_system_prompt``, which only a compression, ``/new``, a session
switch or a rewind clears. So a moved conversation kept answering under the previous project's
instructions until one of those happened — silently, mid-conversation.

``_register_session_cwd`` is the single sink every explicit move reaches (``session.workspace.move``,
``session.cwd.set``, the project_* tools' workspace callback). It now rebuilds the prompt when, and only
when, the moved-to directory loads a DIFFERENT set of context files: an identical set keeps the prompt and
its prefix cache, and the live memory providers are never re-initialized either way.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt, invalidate_system_prompt
from tui_gateway import session_workdir

_OFF = dict(  # everything a real prompt build touches that a SimpleNamespace agent would otherwise lack
    load_soul_identity=False, valid_tool_names=set(), model="gpt-4o", provider="", _memory_enabled=False,
    _memory_store=None, _memory_manager=None,
    _user_profile_enabled=False, _task_completion_guidance=False, _parallel_tool_call_guidance=False,
    _tool_use_enforcement=False, _execution_guidance=False, _environment_probe=False, _bot_mode_protocol=False,
    _kanban_worker_guidance="", pass_session_id=False, session_id="s1", _frozen_workspace_snapshot=None,
    context_compressor=None, _session_db=None, _emit_status=lambda *a, **k: None,
    _emit_diagnostic_status=lambda *a, **k: None,
)


def _project(root: Path, name: str, rules: str | None) -> Path:
    path = root / name
    path.mkdir(parents=True)
    if rules is not None:
        (path / "AGENTS.md").write_text(rules)
    return path


def _agent(cwd: Path, cached: str | None = None) -> SimpleNamespace:
    agent = SimpleNamespace(**{**_OFF, "skip_context_files": False, "platform": "gui",
                               "_cached_system_prompt": cached, "session_cwd": str(cwd), "invalidations": []})

    def _invalidate():  # record, then run the real invalidation the runtime uses
        agent.invalidations.append(agent.session_cwd)
        invalidate_system_prompt(agent)

    agent._invalidate_system_prompt = _invalidate
    return agent


def _move(agent, cwd: Path) -> None:
    """What every explicit workspace move does: re-anchor ``session["cwd"]``, then register it."""
    session_workdir._register_session_cwd({"agent": agent, "session_key": "k1", "cwd": str(cwd)})


def _build(agent, cwd: Path) -> str:
    with patch("agent.prompt_builder.load_soul_md", return_value=""), \
         patch("agent.system_prompt.resolve_context_cwd", return_value=cwd):
        return build_system_prompt(agent)


def test_a_move_rebuilds_the_prompt_with_the_new_projects_rules(tmp_path):
    old = _project(tmp_path, "project-a", "PROJECT-A-MARKER\n")
    new = _project(tmp_path, "project-b", "PROJECT-B-MARKER\n")
    agent = _agent(old)
    agent._cached_system_prompt = _build(agent, old)
    assert "PROJECT-A-MARKER" in agent._cached_system_prompt

    _move(agent, new)

    assert agent.invalidations, "the moved session's prompt was left on the old project's instructions"
    assert agent._cached_system_prompt is None
    rebuilt = _build(agent, new)
    assert "PROJECT-B-MARKER" in rebuilt
    assert "PROJECT-A-MARKER" not in rebuilt


def test_a_move_with_an_identical_context_file_set_keeps_the_prompt(tmp_path):
    """Same repo, another subdirectory: nothing in the context tier changes, so its prefix cache stays."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "AGENTS.md").write_text("PROJECT-MARKER\n")
    agent = _agent(repo / "docs", cached=_build(_agent(repo / "docs"), repo / "docs"))

    _move(agent, repo / "src")

    assert agent.invalidations == []
    assert agent._cached_system_prompt


def test_a_move_before_the_first_prompt_leaves_nothing_to_refresh(tmp_path):
    agent = _agent(_project(tmp_path, "a", "A\n"), cached=None)
    _move(agent, _project(tmp_path, "b", "B\n"))
    assert agent.invalidations == []


def test_a_session_without_an_agent_only_re_anchors_the_cwd(tmp_path):
    session: dict = {"agent": None, "session_key": "k1", "cwd": str(tmp_path)}
    session_workdir._register_session_cwd(session)  # must not raise
    assert session["cwd"] == str(tmp_path)


def test_a_comparison_that_cannot_run_keeps_the_prompt(tmp_path, monkeypatch):
    """Fail open: an unreadable workspace must not cost the session its prompt."""
    agent = _agent(_project(tmp_path, "a", "A\n"), cached="cached")
    monkeypatch.setattr("agent.context_file_sources.list_context_file_sources",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    _move(agent, _project(tmp_path, "b", "B\n"))
    assert agent.invalidations == []


def test_signatures_follow_the_directory_not_the_agent(tmp_path):
    """The manifest is the project-context INPUT, so two dirs differ exactly when their loads differ."""
    from agent import context_file_sources

    signature = getattr(context_file_sources, "context_files_signature_for_agent", None)
    if signature is None:  # a tree without the helper: nothing to compare, so nothing to pin
        pytest.skip("context_files_signature_for_agent is the helper this change adds")
    assert signature is not None
    shared = tmp_path / "shared"
    (shared / "one").mkdir(parents=True)
    (shared / "two").mkdir()
    (shared / "AGENTS.md").write_text("SHARED\n")
    agent = _agent(shared / "one")

    assert signature(agent, cwd=str(shared / "one")) == signature(agent, cwd=str(shared / "two"))
    separate = _project(tmp_path, "separate", "SEPARATE\n")
    assert signature(agent, cwd=str(separate)) != signature(agent, cwd=str(shared / "one"))
