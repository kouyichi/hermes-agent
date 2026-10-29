"""``write_file``'s ``dirs_created`` means "this call made a parent directory".

The field was ``bool(os.path.dirname(path))`` — true for every write that has a
parent component, so writing into a folder that had existed for months reported a
folder creation (#146410). Agents running under a "never create a folder without
asking" policy then told the user a folder had been created that never was. The
flag must stay honest on BOTH lanes: the in-process native write and the shell
backend (remote/docker/Windows), each of which folds ``mkdir -p`` into its own
write.
"""

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

pytestmark = pytest.mark.platforms("posix")


def _ops(tmp_path) -> ShellFileOperations:
    return ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))


@pytest.fixture(params=["shell", "native"])
def ops(request, tmp_path, monkeypatch) -> ShellFileOperations:
    """Both write lanes, asserted to be the lane the id claims."""
    if request.param == "shell":
        monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "0")
    else:
        monkeypatch.delenv("HERMES_NATIVE_FILE_READ", raising=False)
    operations = _ops(tmp_path)
    assert operations._native_read_enabled() is (request.param == "native")
    return operations


def test_existing_parent_reports_no_directory_creation(ops, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    result = ops.write_file(str(existing / "note.md"), "hi\n")
    assert result.error is None
    assert result.dirs_created is False
    assert result.to_dict()["dirs_created"] is False
    assert (existing / "note.md").read_text() == "hi\n"


def test_existing_parent_with_siblings_reports_no_directory_creation(ops, tmp_path):
    """The reported shape: a folder that already holds files is still "created"."""
    busy = tmp_path / "researcher-out"
    busy.mkdir()
    for i in range(3):
        (busy / f"prior-{i}.md").write_text("x\n")
    result = ops.write_file(str(busy / "new.md"), "hi\n")
    assert result.error is None
    assert result.dirs_created is False


def test_missing_parent_chain_reports_creation(ops, tmp_path):
    result = ops.write_file(str(tmp_path / "deep" / "er" / "note.md"), "hi\n")
    assert result.error is None
    assert result.dirs_created is True
    assert (tmp_path / "deep" / "er" / "note.md").read_text() == "hi\n"


def test_write_without_a_parent_component_reports_no_creation(ops, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = ops.write_file("bare.md", "hi\n")
    assert result.error is None
    assert result.dirs_created is False
