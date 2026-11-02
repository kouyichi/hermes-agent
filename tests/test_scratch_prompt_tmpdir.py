"""#147309: the prompt's temporary-directory line names the directory that actually wins.

Hermes points ``TMPDIR``/``TMP``/``TEMP`` at ``get_scratch_dir()`` at boot but never overrides a
value the user set, so a user-exported temp dir (``terminal.temp_dir``) stays in force. The prompt
used to state unconditionally that TMPDIR points at the scratch dir, which sent agents (and
delegated sub-agents) to the Hermes home instead of the user's bounded temp volume.
"""

import pytest

from hermes_constants import get_scratch_dir


@pytest.fixture()
def scratch_home(tmp_path, monkeypatch):
    """``(hints, scratch, monkeypatch)`` for a home whose scratch dir is not the process temp dir."""
    from agent.prompt_builder import _local_host_hints

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.delenv(key, raising=False)
    return _local_host_hints, get_scratch_dir(tmp_path / "home", prune=False), monkeypatch


def test_prompt_names_a_user_set_temp_dir(scratch_home, tmp_path):
    hints, scratch, monkeypatch = scratch_home
    user_tmp = tmp_path / "user-tmp"
    user_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(user_tmp))

    host = hints()[0]

    assert f"Temporary directory: {user_tmp}" in host
    assert f"Scratch directory: {scratch}" not in host
    assert "TMPDIR points here" in host


def test_prompt_keeps_the_scratch_line_when_hermes_owns_the_temp_dir(scratch_home):
    hints, scratch, monkeypatch = scratch_home
    monkeypatch.setenv("TMPDIR", str(scratch))

    host = hints()[0]

    assert f"Scratch directory: {scratch} (TMPDIR points here" in host
    assert "entries idle for 24h are pruned" in host
    assert "Temporary directory:" not in host


def test_prompt_reads_tmp_and_temp_on_platforms_without_tmpdir(scratch_home, tmp_path):
    hints, scratch, monkeypatch = scratch_home
    windows_tmp = tmp_path / "windows-temp"
    windows_tmp.mkdir()
    monkeypatch.setenv("TMP", str(windows_tmp))

    host = hints()[0]

    assert f"Temporary directory: {windows_tmp}" in host
    assert f"Scratch directory: {scratch}" not in host


def test_prompt_falls_back_to_the_scratch_line_without_any_temp_var(scratch_home):
    hints, scratch, _monkeypatch = scratch_home

    host = hints()[0]

    assert f"Scratch directory: {scratch} (TMPDIR points here" in host
