"""A profile delete that cannot remove the tree must not leave a ghost behind.

``delete_profile`` writes the ``profiles/.deleted/<name>`` tombstone *before* ``rmtree``
(on purpose: a stale serve/logging mkdir must not relist the name). If the removal then
fails, the tombstone used to stay — the directory is still fully populated, but
``profile_exists()`` answers False, ``profile list`` omits it, ``-p <name>`` refuses it
with "does not exist. Create it with: hermes profile create <name>" (a destructive
suggestion against real user data), and no supported path can see or delete it.

These tests pin the rollback: the tombstone is cleared whenever the directory survived
with its identity, so the profile stays reachable and the delete can be retried.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.profiles import (
    _missing_profile_error,
    create_profile,
    delete_profile,
    list_profile_names,
    profile_exists,
)
from hermes_constants import (
    named_profile_has_identity,
    named_profile_is_live,
    mark_named_profile_deleted,
    profile_tombstone_path,
)


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _delete_failing_removal(name: str):
    """Run ``delete_profile`` with a failing ``rmtree``; returns the raised error."""
    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ), patch(
        "hermes_cli.profiles._rmtree_with_retry", side_effect=PermissionError("locked")
    ):
        with pytest.raises(RuntimeError, match="Could not remove profile directory") as excinfo:
            delete_profile(name, yes=True)
    return excinfo.value


class TestFailedRemovalKeepsTheProfileReachable:
    def test_tombstone_is_cleared_when_the_tree_survives(self, profile_env):
        profile_dir = create_profile("ghost", no_alias=True, no_skills=True)

        _delete_failing_removal("ghost")

        assert profile_dir.is_dir()
        assert named_profile_has_identity(profile_dir)
        assert not profile_tombstone_path(profile_dir).exists()
        assert named_profile_is_live(profile_dir)
        assert profile_exists("ghost")
        assert "ghost" in list_profile_names()

    def test_failure_message_says_the_profile_was_left_intact(self, profile_env):
        profile_dir = create_profile("ghost", no_alias=True, no_skills=True)

        error = _delete_failing_removal("ghost")

        text = str(error)
        assert str(profile_dir) in text
        assert "left intact" in text
        assert "hermes profile delete ghost" in text
        assert not profile_tombstone_path(profile_dir).exists()

    def test_marker_less_leftover_stays_tombstoned(self, profile_env):
        """A partially removed shell carries no identity: keep it tombstoned (never relist)."""
        profile_dir = create_profile("ghost", no_alias=True, no_skills=True)

        def _strip_identity_then_fail(target: Path, *_args, **_kwargs):
            for marker in ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db"):
                (target / marker).unlink(missing_ok=True)
            raise PermissionError("locked")

        with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
            "hermes_cli.profiles._stop_profile_backends"
        ), patch(
            "hermes_cli.profiles._rmtree_with_retry", side_effect=_strip_identity_then_fail
        ):
            with pytest.raises(RuntimeError, match="Could not remove profile directory") as excinfo:
                delete_profile("ghost", yes=True)

        assert profile_tombstone_path(profile_dir).exists()
        assert not profile_exists("ghost")
        assert "ghost" not in list_profile_names()
        # The leftover's state is named, so nobody has to read the source to explain it.
        assert str(profile_tombstone_path(profile_dir)) in str(excinfo.value)


class TestMissingProfileErrorOnATombstonedDirectory:
    def test_names_the_tombstone_instead_of_suggesting_create(self, profile_env):
        profile_dir = create_profile("monty", no_alias=True, no_skills=True)
        mark_named_profile_deleted(profile_dir)

        text = str(_missing_profile_error("monty"))

        assert str(profile_dir) in text
        assert str(profile_tombstone_path(profile_dir)) in text
        assert "profile create monty" not in text

    def test_plain_missing_profile_keeps_the_old_message(self, profile_env):
        text = str(_missing_profile_error("nosuch"))

        assert text == "Profile 'nosuch' does not exist. Create it with: hermes profile create nosuch"
