"""A catalog re-pin clones only the plugin's subdirectory, in preview and in publication.

``_clone_plugin_repo(subdir=...)`` is a blobless clone with a sparse checkout of that subdirectory;
``install`` already passes it, but both catalog-``update`` call sites resolved the subdir out of the
install identifier and then dropped it, so every update of a monorepo plugin downloaded the whole
repository — twice (#142417). These tests observe the clones the update path really issues.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _git(repo, *args) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _commit(repo, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _monorepo(root, version: str) -> str:
    """A repo whose plugin lives in ``./my-plugin`` and whose root holds unrelated bulk."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# monorepo docs\n", encoding="utf-8")
    site = root / "site"
    site.mkdir(exist_ok=True)
    for index in range(5):
        (site / f"page{index}.html").write_text("<h1>x</h1>\n", encoding="utf-8")
    plugin = root / "my-plugin"
    plugin.mkdir(exist_ok=True)
    (plugin / "plugin.yaml").write_text(
        f"name: my-plugin\nversion: {version}\nmanifest_version: 1\ndescription: subdir plugin\n",
        encoding="utf-8")
    (plugin / "__init__.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    return _commit(root, version)


@pytest.fixture
def pm_home(tmp_path, monkeypatch):
    """Fake core repo + temp HERMES_HOME, so the real ``pm.install`` transaction runs under tmp.

    The plugin-update tests elsewhere in this package take the same setup from a shared fixture;
    importing that fixture (rather than a plain helper) would re-bind its name in every test
    signature, so the minimal environment is rebuilt here instead.
    """
    uv = shutil.which("uv")
    assert uv, "uv is required for the plugin publication transaction"
    core = tmp_path / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text(
        "[project]\n"
        'name = "fake-core"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\ndependencies = []\n',
        encoding="utf-8",
    )
    # The venv package's stamp digest and lock seed read core/uv.lock; no deps resolve offline.
    subprocess.run([uv, "lock"], cwd=core, check=True, capture_output=True, text=True, timeout=120)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")

    import pm.paths
    import pm.workspace as ws

    ensure = importlib.import_module("pm.install")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(pm.paths, "repo_root", lambda: core)
    monkeypatch.setattr(ws.paths, "repo_root", lambda: core)
    monkeypatch.setattr(ensure, "lazy_installs_allowed", lambda: True)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))

    def in_process_sync(*args, project_root=None, **kwargs):
        assert project_root is None or Path(project_root).resolve() == core.resolve()
        return ensure.sync_venv(*args, **kwargs)

    monkeypatch.setattr("pm.client.sync_venv", in_process_sync)
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), Path(sys.executable)))
    return tmp_path, home


@pytest.fixture
def catalog_subdir(pm_home, monkeypatch):
    """A monorepo catalog entry pinned at 1.0.0, installed through the real install core."""
    from hermes_cli import plugin_catalog, plugins_cmd, plugins_cmd_catalog

    root, _home = pm_home
    repo = root / "plugin-origin"
    state = SimpleNamespace(repo=repo, sha=_monorepo(repo, "1.0.0"), target=None)

    def entry():
        return plugin_catalog.PluginCatalogEntry(
            name="mono-plugin", repo=repo.as_uri(), sha=state.sha, description="fixture",
            maintainer="fixture", subdir="my-plugin")

    monkeypatch.setattr(plugin_catalog, "fetch_live_catalog", lambda **kwargs: None)
    monkeypatch.setattr(plugin_catalog, "load_catalog", lambda catalog_dir=None: [entry()])
    monkeypatch.setattr(plugin_catalog, "load_removed_list", lambda catalog_dir=None: [])
    monkeypatch.setattr(plugins_cmd, "_scan_on_install_enabled", lambda: False)
    target, _manifest, installed_name = plugins_cmd_catalog.install_catalog_entry(entry(), force=False)
    assert installed_name == "my-plugin"
    state.target = target
    return state


def _record_clones(monkeypatch) -> list:
    """Every ``git clone`` argv the plugin Git layer issues from here on."""
    from hermes_cli import plugins_cmd

    clones: list = []
    real = plugins_cmd._run_plugin_git

    def spy(git_exe, target, *args, **kwargs):
        if args and args[0] == "clone":
            clones.append(list(args))
        return real(git_exe, target, *args, **kwargs)

    monkeypatch.setattr(plugins_cmd, "_run_plugin_git", spy)
    return clones


def test_catalog_update_clones_only_the_subdir(catalog_subdir, monkeypatch):
    """Both the consent preview and the publication clone are sparse, not whole-repo."""
    from hermes_cli import plugins_cmd

    catalog_subdir.sha = _monorepo(catalog_subdir.repo, "2.0.0")
    clones = _record_clones(monkeypatch)

    result = plugins_cmd.dashboard_update_user_plugin("my-plugin")

    assert result["ok"] is True, result
    assert result["sha"] == catalog_subdir.sha
    assert "VERSION = '2.0.0'" in (catalog_subdir.target / "__init__.py").read_text(encoding="utf-8")
    assert len(clones) == 2, f"expected a preview clone and a publication clone: {clones}"
    whole_repo = [argv for argv in clones if "--filter=blob:none" not in argv]
    assert not whole_repo, f"the update downloaded the whole repository: {whole_repo}"
    assert not (catalog_subdir.target / "README.md").exists()


def test_consent_preview_clone_is_sparse(catalog_subdir, monkeypatch):
    """The preview runs before any consent decision, so it must not pay for the whole repository."""
    from hermes_cli import plugins_cmd, plugins_transaction

    catalog_subdir.sha = _monorepo(catalog_subdir.repo, "2.0.0")
    clones = _record_clones(monkeypatch)

    class _StopBeforePublication(Exception):
        pass

    def stop(*_args, **_kwargs):
        raise _StopBeforePublication

    monkeypatch.setattr(plugins_transaction, "update_plugin", stop)

    with pytest.raises(_StopBeforePublication):
        plugins_cmd.dashboard_update_user_plugin("my-plugin")

    assert len(clones) == 1, f"expected exactly the preview clone: {clones}"
    assert "--filter=blob:none" in clones[0], f"preview cloned the whole repository: {clones[0]}"
