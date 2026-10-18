"""``hermes import`` must give a newly restored file the mode the archive recorded.

``hermes backup`` writes every member with ``ZipFile.write``, so each entry
carries its Unix mode in ``ZipInfo.external_attr`` (``mode << 16``). The restore
side publishes members through ``mkstemp`` and, for a target that does not exist
yet, used only a umask-derived default — so an executable came back as ``0644``
and the archive's own record was ignored. Nothing raises: a script invoked
through an interpreter keeps working, and the loss surfaces at first use (or on
the first ``os.access(X_OK)`` probe of a staged language-server binary), days
after the restore.

The tests here pin the three rules the fix has to keep together: the archive's
recorded bits apply to a *newly created* target, ``setuid``/``setgid`` are never
carried from the archive, and a target that already exists keeps its own mode
(the pre-existing contract ``test_backup.py``'s
``test_restore_preserves_existing_file_mode`` guards).
"""

import os
import stat
import zipfile
from argparse import Namespace
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@pytest.fixture(autouse=True)
def _no_gateway_revive(monkeypatch):
    """Keep the post-restore service start out of a unit test (it is not the subject).

    ``run_import`` revives the gateway service for what it believes is the
    default home; the conftest live-system guard would block that ``systemctl``
    call and print a warning into this module's output.
    """
    monkeypatch.setattr("hermes_cli.gateway._is_service_running", lambda: True)


def _member(filename: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename)
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _write_zip(zip_path: Path, members: list[tuple[zipfile.ZipInfo, bytes]]) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for info, data in members:
            zf.writestr(info, data)


def _home(tmp_path: Path, monkeypatch) -> Path:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return hermes_home


def _import(zip_path: Path) -> None:
    from hermes_cli.backup import run_import

    run_import(Namespace(zipfile=str(zip_path), force=True))


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_archive_executable_bit_survives_a_fresh_restore(tmp_path, monkeypatch):
    """A script the archive recorded as 0755 must not land as 0644."""
    hermes_home = _home(tmp_path, monkeypatch)
    zip_path = tmp_path / "backup.zip"
    _write_zip(
        zip_path,
        [
            (_member("config.yaml", 0o600), b"model: {}\n"),
            (_member("scripts/poll.sh", 0o755), b"#!/bin/sh\necho hi\n"),
        ],
    )

    _import(zip_path)

    script = hermes_home / "scripts" / "poll.sh"
    assert script.read_text() == "#!/bin/sh\necho hi\n"
    assert _mode(script) == 0o755, f"restored {oct(_mode(script))}, archive recorded 0o755"
    # A member that recorded owner-only bits keeps them too — the archive's
    # record replaces the umask default in both directions.
    assert _mode(hermes_home / "config.yaml") == 0o600


def test_archive_setuid_and_setgid_are_dropped(tmp_path, monkeypatch):
    """Only the twelve ordinary bits may come out of the archive."""
    hermes_home = _home(tmp_path, monkeypatch)
    zip_path = tmp_path / "backup.zip"
    _write_zip(
        zip_path,
        [
            (_member("config.yaml", 0o600), b"model: {}\n"),
            (_member("helper.sh", 0o4755), b"#!/bin/sh\necho helper\n"),
            (_member("shared.sh", 0o2755), b"#!/bin/sh\necho shared\n"),
        ],
    )

    _import(zip_path)

    for rel in ("helper.sh", "shared.sh"):
        mode = _mode(hermes_home / rel)
        assert mode == 0o755, f"{rel} restored as 0o{mode:o}"
        assert not mode & stat.S_ISUID, f"{rel} kept setuid (0o{mode:o})"
        assert not mode & stat.S_ISGID, f"{rel} kept setgid (0o{mode:o})"


def _member_without_mode(filename: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename)
    info.external_attr = 0
    return info


class _FakeZip:
    """The two ``ZipFile`` calls :func:`_archive_member_mode` makes."""

    def __init__(self, infos: dict[str, zipfile.ZipInfo]):
        self._infos = infos

    def getinfo(self, name: str) -> zipfile.ZipInfo:
        if name not in self._infos:
            raise KeyError(name)
        return self._infos[name]


def test_helper_reads_the_recorded_mode_and_masks_it():
    """Unit-level contract: mode << 16 in, 0o777-masked mode out."""
    from hermes_cli.backup_restore import _archive_member_mode

    infos = {
        "plain": _member("plain", 0o644),
        "exec": _member("exec", 0o755),
        "setuid": _member("setuid", 0o4755),
        "unrecorded": _member_without_mode("unrecorded"),
    }
    fake = cast("zipfile.ZipFile", _FakeZip(infos))

    assert _archive_member_mode(fake, "plain") == 0o644
    assert _archive_member_mode(fake, "exec") == 0o755
    assert _archive_member_mode(fake, "setuid") == 0o755
    # No usable mode recorded -> the caller's own fallback applies.
    assert _archive_member_mode(fake, "unrecorded") is None
    assert _archive_member_mode(fake, "absent") is None


def test_existing_target_keeps_its_own_mode(tmp_path, monkeypatch):
    """The archive's mode applies only to a target that does not exist yet.

    A restore over an existing file has always kept that file's permissions
    (Docker/NAS installs that rely on broader bits), so the archive must not
    become the authority over a file the user already has.
    """
    hermes_home = _home(tmp_path, monkeypatch)
    target = hermes_home / "config.yaml"
    target.write_text("model: original\n")
    os.chmod(target, 0o640)

    zip_path = tmp_path / "backup.zip"
    _write_zip(zip_path, [(_member("config.yaml", 0o600), b"model: restored\n")])

    _import(zip_path)

    assert target.read_text() == "model: restored\n"
    assert _mode(target) == 0o640
