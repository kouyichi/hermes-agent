"""A plugin transaction's hidden staging dir is never a plugin (#147455).

``hermes plugins install`` clones into ``<plugins>/.install-<rand>/plugin`` (update and remove use
``.update-*`` / ``.<name>.remove-*`` the same way), so for the length of the transaction a complete
plugin tree sits INSIDE the directory discovery scans. A discovery pass that runs meanwhile found it as
``.install-<rand>/plugin``, and ``gate_manifest`` matches ``plugins.enabled`` on the manifest name, so
the staged copy of an enabled plugin got loaded as a second, transient plugin — it registered the
plugin's context engine first and the real plugin then refused to register ("Another ... generation is
already loaded"), while the process kept running code from a folder the installer deleted seconds later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import plugins_discovery
from hermes_cli.plugins import PluginManager
from hermes_cli.plugins_discovery import scan_directory


def _write_plugin(directory: Path, name: str = "demo") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(f"name: {name}\nversion: 1.0.0\ndescription: x\n")
    (directory / "__init__.py").write_text("")


@pytest.mark.parametrize("staging", [".install-j_v0xyy0", ".update-demo-abc123", ".demo.remove-9f2c"])
def test_staging_copy_is_not_scanned(tmp_path: Path, staging: str) -> None:
    """The install / update / remove staging shapes all sit at the top of the plugins root."""
    root = tmp_path / "plugins"
    _write_plugin(root / "demo")
    _write_plugin(root / staging / "plugin")

    found = PluginManager()._scan_directory(root, "user")

    assert [manifest.key for manifest in found] == ["demo"]
    assert [manifest.name for manifest in found] == ["demo"]


def test_staging_copy_does_not_load_next_to_the_real_plugin(tmp_path: Path) -> None:
    """The user-visible half: the staged copy of an ENABLED plugin used to be loadable too."""
    root = tmp_path / "plugins"
    _write_plugin(root / "demo")
    _write_plugin(root / ".install-j_v0xyy0" / "plugin")

    manifests = scan_directory(root, "user")

    assert [manifest.key for manifest in manifests] == ["demo"]


def test_hidden_dir_below_a_plugin_is_still_scanned(tmp_path: Path) -> None:
    """Over-reach guard: a multi-harness repo keeps its real Hermes manifest one level down in
    ``<plugin>/.hermes-plugin/``, which must stay discoverable (see
    ``test_plugin_scanner_recursion.py::test_foreign_harness_dirs_skipped_without_warnings``)."""
    root = tmp_path / "plugins"
    _write_plugin(root / "superpowers" / ".hermes-plugin", name="superpowers")

    found = PluginManager()._scan_directory(root, "user")

    assert [manifest.key for manifest in found] == ["superpowers/.hermes-plugin"]


def test_list_command_does_not_offer_the_staging_copy() -> None:
    """``hermes plugins list`` runs its own scan; it must agree with discovery (``_scan_level`` is
    documented as matching the loader's walk)."""
    import os

    from hermes_cli.plugins_cmd import _discover_all_plugins

    hermes_home = Path(os.environ["HERMES_HOME"])  # set by the hermetic conftest fixture
    root = hermes_home / "plugins"
    _write_plugin(root / "demo")
    _write_plugin(root / ".install-j_v0xyy0" / "plugin")

    keys = [row[5] for row in _discover_all_plugins()]

    assert "demo" in keys
    assert [key for key in keys if key.startswith(".")] == []


def test_predicate_only_rejects_hidden_top_level_children() -> None:
    predicate = getattr(plugins_discovery, "skips_plugin_child", None)
    assert callable(predicate), "plugins_discovery.skips_plugin_child is missing"
    assert predicate(".install-abc123", 0) is True
    assert predicate("demo", 0) is False
    assert predicate(".hermes-plugin", 1) is False
    assert predicate("__pycache__", 1) is True
