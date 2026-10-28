"""``/api/profiles`` says whether a profile has a server-side avatar (#145855).

The Desktop's Bot Mode union roster is filled from this endpoint for profiles the
active gateway does not serve itself. Without the flag the client could not tell
"the server has no avatar" from "this response never said", so it treated every
such row as avatar-less and pushed its own cached face over real art.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.profiles import list_profiles


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolated environment: Path.home() and HERMES_HOME under tmp_path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return default_home


def _row() -> dict:
    from hermes_cli.web_routers.profiles import _profile_to_dict

    return _profile_to_dict(list_profiles()[0])


def test_reports_false_when_no_avatar_asset(profile_env):
    assert _row()["has_avatar"] is False


def test_reports_true_for_every_supported_extension(profile_env):
    assets = profile_env / "assets"
    assets.mkdir()

    for ext in ("png", "jpg", "webp"):
        (assets / f"avatar.{ext}").write_bytes(b"avatar")
        assert _row()["has_avatar"] is True, ext
        (assets / f"avatar.{ext}").unlink()


def test_ignores_a_foreign_asset_name(profile_env):
    assets = profile_env / "assets"
    assets.mkdir()
    (assets / "avatar.png.bak").write_bytes(b"avatar")

    assert _row()["has_avatar"] is False
