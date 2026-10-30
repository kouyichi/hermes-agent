"""A code-health run that compared a tree with itself measured nothing — that is not a pass.

``run()`` treated every empty ``head_paths`` as "nothing to do" and returned 0, so
``scripts/check --only health --base HEAD`` printed ``=== health (ok) ===`` for a run that
measured no file at all. A degenerate base (a ``--depth 1`` clone, a worktree on ``main``, a
manual ``--base HEAD``) therefore looked like the strongest possible signal. Base == head is
never a measurement: it refuses with the "broken run" exit code instead.

The legitimate empty case — two different commits whose diff touches no measured file (a
docs-only commit, which the pre-commit hook must keep passing) — stays a pass, so the
distinction is the commit identity, not the emptiness.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.code_health import cli

REPO = Path(__file__).resolve().parents[2]


def _env() -> dict[str, str]:
    """The caller's env minus anything that would point git at another repo or config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, env=_env(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=600, check=False)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout.strip()


def _write(repo: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _commit(repo: Path, files: Mapping[str, str]) -> str:
    _write(repo, files)
    _git(repo, "add", "--all", "--", *files)
    _git(repo, "commit", "-q", "-m", "step")
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    """A repo with the ruff pin and the rule policy (so a measured diff can be judged) and one
    clean module."""
    path = tmp_path / "repo"
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    shutil.copy(REPO / "pyproject.toml", path / "pyproject.toml")
    (path / "scripts/ci").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/ci/profile_scope_patterns.json",
                 path / "scripts/ci/profile_scope_patterns.json")
    _commit(path, {"pkg/a.py": "def a():\n    return 1\n"})
    return path


def test_a_tree_compared_with_itself_is_not_a_pass(tmp_path, capsys):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    assert cli.run(repo, head, head) == 2, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "nothing measured" in out and "same commit" in out and head[:12] in out, out
    assert "no measured files changed" not in out, out


def test_the_reported_repro_refuses_through_the_cli(tmp_path, monkeypatch, capsys):
    """`scripts/check --only health --base HEAD` on a clean tree: base is HEAD, the head is that
    same working tree, so the run can never measure anything."""
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    assert cli.main(["--base", "HEAD"]) == 2
    out = capsys.readouterr().out
    assert "nothing measured" in out, out
    assert "files vs" not in out, out  # no measurement line: this run judged nothing


def test_an_identical_commit_head_refuses_too(tmp_path, monkeypatch, capsys):
    """The commit lane (`--head <sha>`) takes the same verdict as the working-tree lane."""
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    assert cli.main(["--base", head, "--head", head]) == 2
    assert "same commit" in capsys.readouterr().out


def test_json_refusal_keeps_stdout_json_but_exits_2(tmp_path, capsys):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    assert cli.run(repo, head, head, as_json=True) == 2
    out, err = capsys.readouterr()
    assert out.strip() == "[]", out  # the machine channel keeps its shape
    assert "same commit" in err, err  # ... and the reason is on stderr, never a silent pass


def test_a_docs_only_commit_still_passes(tmp_path, capsys):
    """The pre-commit hook runs on docs-only commits: two different commits with an empty measured
    diff is a legitimate pass, so only the identical-commit case refuses."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"README.md": "docs only\n"})
    assert head != base
    assert cli.run(repo, base, head) == 0, capsys.readouterr().out
    assert "no measured files changed" in capsys.readouterr().out


def test_an_uncommitted_measured_change_is_still_measured(tmp_path, monkeypatch, capsys):
    """The guard must not swallow a working-tree run that has something to judge."""
    repo = _repo(tmp_path)
    _write(repo, {"pkg/b.py": "def b():\n    return 2\n"})
    monkeypatch.chdir(repo)
    assert cli.main(["--base", "HEAD"]) == 0, capsys.readouterr().out
    assert "1 files vs" in capsys.readouterr().out
