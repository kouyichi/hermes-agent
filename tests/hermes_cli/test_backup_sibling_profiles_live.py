"""A pre-update snapshot may only adopt a LIVE sibling profile (#144748).

``_sibling_profile_homes`` used to enumerate ``profiles/`` with nothing but an ``is_dir()`` check,
so a tombstoned profile whose directory a stale writer resurrected with a bare ``mkdir`` was
treated as an install and had ``.backup.lock``/``state-snapshots`` written into it — invisible in
the update log, because the empty husk snapshots to ``None``.
"""

import shutil
from pathlib import Path

import pytest

from hermes_cli import backup, profiles as profiles_mod
from hermes_constants import mark_named_profile_deleted


def _mk_profile(path: Path, *, markers: tuple[str, ...] = ("config.yaml",)) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for marker in markers:
        (path / marker).write_text("model: {}\n", encoding="utf-8")
    return path


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    """default home + p1 (tombstoned husk) + p2 (invoking) + p3 (live) + a marker-less ghost."""
    root = tmp_path / "home"
    profiles_root = root / "profiles"
    root.mkdir()
    homes = {
        "p1": _mk_profile(profiles_root / "p1"),
        "p2": _mk_profile(profiles_root / "p2"),
        "p3": _mk_profile(profiles_root / "p3"),
    }
    # A proper delete: tombstone first, tree second.
    mark_named_profile_deleted(homes["p1"])
    shutil.rmtree(homes["p1"])
    homes["p1"].mkdir()  # a stale process's bare mkdir resurrects the husk
    ghost = profiles_root / "ghost-shell"
    ghost.mkdir()  # runtime side-effect dir: no tombstone, no identity marker

    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: root)
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: profiles_root)
    return {"root": root, "profiles": profiles_root, "ghost": ghost, **homes}


def _sibling_names(invoking: Path) -> list[str]:
    return [name for name, _home in backup._sibling_profile_homes(invoking)]


class TestSiblingEnumeration:
    def test_tombstoned_husk_is_not_a_sibling(self, sandbox):
        assert "p1" not in _sibling_names(sandbox["p2"])

    def test_markerless_shell_is_not_a_sibling(self, sandbox):
        assert "ghost-shell" not in _sibling_names(sandbox["p2"])

    def test_live_siblings_are_kept(self, sandbox):
        assert _sibling_names(sandbox["p2"]) == ["default", "p3"]

    def test_identity_is_any_marker_not_only_config_yaml(self, sandbox):
        """A profile whose identity is ``.env``/``auth.json`` alone is live and still snapshotted."""
        env_only = _mk_profile(sandbox["profiles"] / "env-only", markers=(".env",))
        assert "env-only" in _sibling_names(sandbox["p2"])
        assert env_only.is_dir()


class TestPreUpdateSnapshots:
    def test_husk_and_ghost_are_not_written_into(self, sandbox):
        backup.create_pre_update_snapshots_all_profiles(invoking_home=sandbox["p2"], keep=1)

        for path in (sandbox["p1"], sandbox["ghost"]):
            assert not (path / ".backup.lock").exists()
            assert not (path / "state-snapshots").exists()
            assert sorted(path.iterdir()) == []

    def test_live_sibling_is_still_snapshotted(self, sandbox):
        result = backup.create_pre_update_snapshots_all_profiles(invoking_home=sandbox["p2"], keep=1)

        assert set(result) == {"p3"}
        snap_dir = sandbox["p3"] / "state-snapshots" / result["p3"]
        assert (snap_dir / "config.yaml").is_file()
