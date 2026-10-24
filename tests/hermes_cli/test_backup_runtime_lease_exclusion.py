"""A runtime lease file a live process holds must never be selected for archiving.

``runtime/wake-word.lock`` is held with a mandatory 1-byte byte-range lock on Windows, so
``zipfile`` cannot even open the member for reading: the archive is reported incomplete,
``hermes backup`` exits 1, and ``--keep`` pruning is skipped — on every run that overlaps
the lease holder (#143667). Nothing in the file is portable (``hermes import`` already
refuses to restore PID/lock/registry state), so the archive skips the whole lease class
instead of reporting a permanently unreadable member.

The ticket's traceback is Windows-only, but the *result* it reports is not: an unreadable
member is an unreadable member. ``chmod 0`` reproduces it on POSIX, which is what makes these
tests real evidence on this host.
"""

import os
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_home(root: Path) -> Path:
    """A minimal Hermes home with one file worth restoring."""
    home = root / ".hermes"
    (home / "runtime").mkdir(parents=True)
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (home / "config.yaml").write_text("model:\n  provider: openrouter\n", encoding="utf-8")
    return home


def _make_unreadable(path: Path) -> None:
    """Make *path* exist but fail ``open()`` — the shape a held byte-range lock produces."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\x00", encoding="utf-8")
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("this user can read a 0o000 file (root): mode bits deny nobody")


def _run_backup(home: Path, root: Path, out_zip: Path, monkeypatch) -> bool:
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: root)
    from hermes_cli.backup import run_backup

    return run_backup(Namespace(output=str(out_zip)))


# ---------------------------------------------------------------------------
# Archive policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", [
    "runtime/wake-word.lock",
    "runtime/active_sessions.lock",
    "runtime/bot.pid",
    "runtime/Wake-Word.LOCK",  # the reporting platform is case-insensitive
    "profiles/coder/runtime/wake-word.lock",
])
def test_runtime_lease_files_are_excluded_from_a_backup(rel):
    from hermes_cli.backup import _should_exclude

    assert _should_exclude(Path(rel))


@pytest.mark.parametrize("rel", [
    "runtime/active_sessions.json",                  # the PID registry itself still travels
    "runtime/bot_live_delivery/queue.lock",          # a deeper entry is spool, not a lease
    "skills/metrics/runtime/collector.lock",         # a skill's own runtime/ is user data
    "profiles/coder/skills/metrics/runtime/x.lock",
    "logs/agent.log",
])
def test_non_lease_paths_stay_archivable(rel):
    from hermes_cli.backup import _should_exclude

    assert not _should_exclude(Path(rel))


# ---------------------------------------------------------------------------
# End to end: the reported exit status
# ---------------------------------------------------------------------------


def test_backup_completes_while_a_runtime_lease_is_held(tmp_path, monkeypatch):
    """The reported bug: one unreadable lease made a complete archive report exit 1."""
    home = _seed_home(tmp_path)
    _make_unreadable(home / "runtime" / "wake-word.lock")
    out_zip = tmp_path / "backup.zip"

    assert _run_backup(home, tmp_path, out_zip, monkeypatch) is True
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
    assert "config.yaml" in names
    assert not [n for n in names if n.startswith("runtime/")], names


def test_named_profile_lease_does_not_fail_the_run(tmp_path, monkeypatch):
    home = _seed_home(tmp_path)
    lease = home / "profiles" / "coder" / "runtime" / "wake-word.lock"
    _make_unreadable(lease)
    out_zip = tmp_path / "backup.zip"

    assert _run_backup(home, tmp_path, out_zip, monkeypatch) is True
    with zipfile.ZipFile(out_zip) as zf:
        assert "profiles/coder/runtime/wake-word.lock" not in zf.namelist()
    # Policy, not cleanup: the lease on this machine is left alone.
    assert lease.exists()


def test_an_unreadable_file_outside_runtime_still_fails_the_run(tmp_path, monkeypatch):
    """Same unreadable member, ordinary location: the exit-1 contract must survive."""
    home = _seed_home(tmp_path)
    _make_unreadable(home / "skills" / "demo" / "SKILL.md")
    out_zip = tmp_path / "backup.zip"

    assert _run_backup(home, tmp_path, out_zip, monkeypatch) is False
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
    assert "skills/demo/SKILL.md" not in names
    assert "config.yaml" in names


# ---------------------------------------------------------------------------
# The restore half: archives that predate the archive-side exclusion
# ---------------------------------------------------------------------------


def test_import_does_not_restore_an_archived_runtime_lease():
    """A foreign lease must never overwrite the destination's live one on restore."""
    from hermes_cli.backup import _import_skipped

    assert _import_skipped("runtime/wake-word.lock")
    assert _import_skipped("profiles/coder/runtime/active_sessions.lock")
    assert not _import_skipped("runtime/active_sessions.json")
    assert not _import_skipped("config.yaml")
