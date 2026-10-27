"""A staging step that could not save its record must not be reported as staged (#145447).

``stage_write`` used to swallow the disk error and still hand back a record with an ``id``, so
``skill_manage`` and ``memory`` answered ``{"success": true, "staged": true, "pending_id": ...}``
for a pending record that was never written. The model passed that on, and the user went looking
for a review that did not exist. The pending store now raises ``StagingFailed`` and every caller
turns it into a tool error, so "nothing was saved" reaches the user as an error.

The ``StagingFailed`` lookups go through ``getattr`` so a run against the old pending store fails
on the behaviour (nothing raised, ``staged: true`` returned) instead of at import time.
"""

import json
import os
from unittest.mock import patch

import pytest

from tools import write_approval as wa
from tools.memory_tool import MemoryStore, memory_tool
from tools.skill_manager_tool import skill_manage

# The exception the fix adds; the old store cannot raise it, which is the point of the regression.
STAGING_FAILED = getattr(wa, "StagingFailed", RuntimeError)

VALID_SKILL_CONTENT = """\
---
name: gated-skill
description: A gated skill for staging-failure tests.
---

# Gated Skill

Step 1: Do the thing.
"""


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _enable_subsystem(subsystem):
    import hermes_cli.config as cfg

    config = cfg.load_config()
    config.setdefault(subsystem, {})["write_approval"] = True
    cfg.save_config(config)


def _pending_records(subsystem):
    return sorted(wa._pending_path(subsystem, "").parent.glob("*.json"))


@pytest.fixture
def unwritable_store(monkeypatch):
    """Make the pending record's write fail the way a read-only mount or a full disk does."""

    def _fail(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(wa, "atomic_json_write", _fail)


def test_stage_write_raises_instead_of_returning_an_unsaved_record(hermes_home, unwritable_store):
    with pytest.raises(STAGING_FAILED):
        wa.stage_write(
            wa.MEMORY,
            {"action": "add", "target": "memory", "content": "a fact"},
            summary="add to memory",
            origin="foreground",
        )
    assert wa.list_pending(wa.MEMORY) == []
    assert _pending_records(wa.MEMORY) == []


def test_stage_write_raises_on_a_read_only_pending_directory(hermes_home):
    """The reported shape: a pending directory the process may not write to."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    pending_dir = wa._pending_path(wa.SKILLS, "").parent
    pending_dir.mkdir(parents=True)
    pending_dir.chmod(0o500)
    try:
        with pytest.raises(STAGING_FAILED):
            wa.stage_write(
                wa.SKILLS,
                {"action": "create", "name": "gated-skill"},
                summary="create gated-skill",
                origin="foreground",
            )
    finally:
        pending_dir.chmod(0o700)
    assert _pending_records(wa.SKILLS) == []


def test_skill_manage_reports_a_failed_staging_as_an_error(tmp_path, hermes_home, unwritable_store):
    _enable_subsystem(wa.SKILLS)
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), patch(
        "agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]
    ):
        payload = json.loads(
            skill_manage(action="create", name="gated-skill", content=VALID_SKILL_CONTENT)
        )
    assert payload.get("staged") is not True, payload
    assert payload.get("success") is not True, payload
    assert "error" in payload, payload
    assert "Could not stage" in payload["error"], payload
    assert _pending_records(wa.SKILLS) == []
    # Nothing was committed either: the gate stages before the write happens.
    assert not (tmp_path / "gated-skill").exists()


def test_memory_tool_reports_a_failed_staging_as_an_error(tmp_path, hermes_home, unwritable_store, monkeypatch):
    _enable_subsystem(wa.MEMORY)
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store = MemoryStore(memory_char_limit=500, user_char_limit=300)
    store.load_from_disk()
    raw = memory_tool(action="add", target="memory", content="User prefers terse answers.", store=store)
    payload = json.loads(raw)
    assert payload.get("staged") is not True, payload
    assert "error" in payload, payload
    assert "Could not stage" in payload["error"], payload
    assert _pending_records(wa.MEMORY) == []
    assert store._entries_for("memory") == []
