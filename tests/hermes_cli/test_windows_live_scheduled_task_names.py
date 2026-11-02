"""A live Windows module touches only Scheduled Tasks it named for this run (#147544).

``Hermes_Gateway`` is ``gateway_windows._TASK_NAME_DEFAULT`` — the live autostart task of the
default profile on any Windows install.  A ``*live*.py`` module that hands that literal to
``schtasks`` rewrites a contributor's real task with a stub action and then deletes it, so their
gateway stops starting at the next logon.  Live modules therefore key the task on a per-run name:
either a literal that is not the production default (``f"Hermes_Test_{os.getpid()}"`` in
``test_gateway_windows.py``) or the bare name renamed for the run with
``monkeypatch.setattr(gateway_windows, "_TASK_NAME_DEFAULT", bare)``, which every reader of the
bare name resolves at call time.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

from hermes_cli import gateway_windows, gateway_windows_legacy

_TESTS_ROOT = Path(__file__).resolve().parents[1]


def _live_modules() -> list[Path]:
    return [path for path in sorted(_TESTS_ROOT.rglob("*live*.py")) if path.name != Path(__file__).name]


def _task_name_literals(source: str) -> list[tuple[int, str]]:
    """``(lineno, name)`` for every ``/TN`` argument this module passes as a literal."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        items = node.elts
        for index, item in enumerate(items[:-1]):
            following = items[index + 1]
            if (
                isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value.casefold() == "/tn"
                and isinstance(following, ast.Constant)
                and isinstance(following.value, str)
            ):
                found.append((following.lineno, following.value))
    return found


def _task_name_renames(source: str) -> list[tuple[int, str]]:
    """``(lineno, name)`` for every call that passes ``_TASK_NAME_DEFAULT`` a literal name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        literals = [
            argument for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        ]
        if any(argument.value == "_TASK_NAME_DEFAULT" for argument in literals):
            found.extend(
                (argument.lineno, str(argument.value))
                for argument in literals
                if argument.value != "_TASK_NAME_DEFAULT"
            )
    return found


def test_live_modules_never_pass_the_production_task_name_to_schtasks() -> None:
    production = gateway_windows._TASK_NAME_DEFAULT.casefold()
    offending = [
        f"{path.relative_to(_TESTS_ROOT)}:{lineno} passes {name!r} to schtasks /TN"
        for path in _live_modules()
        for lineno, name in _task_name_literals(path.read_text(encoding="utf-8"))
        if name.casefold() == production
    ]
    assert offending == [], (
        "a live module would create and then delete the real gateway autostart task; key the task "
        "on a per-run name instead: " + "; ".join(offending)
    )


def test_live_modules_never_rename_the_bare_task_to_the_production_name() -> None:
    production = gateway_windows._TASK_NAME_DEFAULT.casefold()
    offending = [
        f"{path.relative_to(_TESTS_ROOT)}:{lineno} sets _TASK_NAME_DEFAULT to {name!r}"
        for path in _live_modules()
        for lineno, name in _task_name_renames(path.read_text(encoding="utf-8"))
        if name.casefold() == production
    ]
    assert offending == [], "; ".join(offending)


def test_legacy_launcher_artifacts_follow_the_renamed_bare_task(monkeypatch, tmp_path) -> None:
    """The rename keeps the production path real: with ``_TASK_NAME_DEFAULT`` re-pointed, the
    legacy sweep queries and deletes the test's own task, never the production one."""
    bare = f"Hermes_Gateway_livetest_{uuid.uuid4().hex[:8]}"
    home = tmp_path / "hermes"
    (home / "gateway-service").mkdir(parents=True)
    appdata = tmp_path / "AppData" / "Roaming"
    appdata.joinpath("Microsoft/Windows/Start Menu/Programs/Startup").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(gateway_windows, "_TASK_NAME_DEFAULT", bare)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: f"{bare}_feedface")

    calls: list[list[str]] = []

    def fake_schtasks(argv, *_args, **_kwargs):
        calls.append(list(argv))
        action = f"{home}/gateway-service/Hermes_Gateway.vbs"
        return 0, f"<Task><Actions><Exec><Arguments>{action}</Arguments></Exec></Actions></Task>", ""

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)

    artifacts = gateway_windows_legacy.legacy_launcher_artifacts()

    assert ("task", "legacy pre-suffix Scheduled Task", bare) in artifacts
    assert calls == [["/Query", "/TN", bare, "/XML"]]


def test_uninstall_only_ever_names_the_renamed_task(monkeypatch, tmp_path) -> None:
    """The live module's rename reaches production ``uninstall()``: every ``schtasks`` argv it
    issues names this run's task and none names the default profile's autostart task."""
    production = gateway_windows._TASK_NAME_DEFAULT
    bare = f"Hermes_Gateway_livetest_{uuid.uuid4().hex[:8]}"
    home = tmp_path / "hermes"
    (home / "gateway-service").mkdir(parents=True)
    appdata = tmp_path / "AppData" / "Roaming"
    appdata.joinpath("Microsoft/Windows/Start Menu/Programs/Startup").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(gateway_windows, "_TASK_NAME_DEFAULT", bare)
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)

    argvs: list[list[str]] = []

    def fake_schtasks(argv, *_args, **_kwargs):
        argvs.append(list(argv))
        action = f"{home}/gateway-service/Hermes_Gateway.vbs"
        return 0, f"<Task><Actions><Exec><Arguments>{action}</Arguments></Exec></Actions></Task>", ""

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)

    gateway_windows.uninstall()

    named = [argv[argv.index("/TN") + 1] for argv in argvs if "/TN" in argv]
    assert named, "uninstall issued no schtasks call at all"
    assert all(name.startswith(bare) for name in named), named
    assert production not in named

