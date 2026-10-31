"""A daemon the runtime spawns with its profile only in the environment must read as running.

``hermes_cli.gateway_runtime_start.spawn_unmanaged_gateway()`` starts the on-demand
``gateway run --quiet --idle-exit`` daemon for a ``<root>/profiles/<name>`` home WITHOUT
``--profile``: the home arrives in the child's environment only, so the argv rule in
``gateway.status`` rejected the live PID and ``gateway list`` / ``profile list`` / the dashboard
profiles page reported that profile stopped while it held the lock and served it (#147147).

These fixtures spawn a REAL child: the identity under test lives in that child's environment, which
no in-process monkeypatch can stand in for (``os.environ`` mutations never reach it).
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import status
from gateway.status_process_env import live_process_hermes_home
from tests.waiting import wait_until

pytestmark = [
    # The stub child's argv is `gateway run`, the shape the live-system guard looks for; it is this
    # launcher, reaped by the fixture below.
    pytest.mark.spawns_gateway_lookalike,
    pytest.mark.skipif(sys.platform == "win32", reason="the identity comes from the child's POSIX environment"),
]


def _record(pid: int, home: Path) -> dict:
    """The identity record the daemon writes under its own home (``gateway.pid``)."""
    return {
        "pid": pid,
        "kind": "hermes-gateway",
        "argv": ["hermes_cli/main.py", "gateway", "run", "--quiet", "--idle-exit"],
        "start_time": status._get_process_start_time(pid),
        "hermes_home": str(home),
        "gateway_state": "running",
    }


@pytest.fixture
def spawn(tmp_path):
    """Start gateway-shaped children under a chosen HERMES_HOME; reap every one at teardown.

    The launcher blocks on stdin instead of sleeping, so it also exits when pytest is killed: the
    pipe closes with the parent.
    """
    launcher = tmp_path / "hermes"
    launcher.write_text("#!/bin/sh\ncat >/dev/null\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    children: list[subprocess.Popen] = []

    def _spawn(home: Path, *args: str) -> subprocess.Popen:
        env = {**os.environ, "HERMES_HOME": str(home)}
        child = subprocess.Popen(
            [str(launcher), *(args or ("gateway", "run", "--quiet", "--idle-exit"))],
            env=env, stdin=subprocess.PIPE,
        )
        children.append(child)
        # Readiness is the exec'd command line, not the clock.
        wait_until(lambda: "gateway run" in (status._read_process_cmdline(child.pid) or ""),
                   what=f"child {child.pid} to report a gateway command line")
        return child

    yield _spawn
    for child in children:
        child.terminate()
        child.wait(timeout=10)


def test_on_demand_daemon_is_running_for_the_profile_its_environment_names(tmp_path, spawn):
    """The reported bug: argv is bare, the home is in the environment, the profile reads stopped."""
    home = (tmp_path / "profiles" / "mediabuyer").resolve()
    home.mkdir(parents=True)
    child = spawn(home)
    cmdline = status._read_process_cmdline(child.pid) or ""

    # The argv-only rule still cannot see this daemon's profile -- the defect's premise.
    assert "gateway run" in cmdline
    assert status.profile_flag_value(cmdline) is None
    assert status.hermes_home_assignments(cmdline.lower()) == []
    assert status._command_line_belongs_to_profile(cmdline, home) is False

    assert live_process_hermes_home(child.pid) == home
    assert status._record_matches_live_gateway_pid(_record(child.pid, home), child.pid, expected_home=home) is True
    # The reader behind `gateway list` and the dashboard profiles page (`_check_gateway_running`).
    assert status.get_runtime_status_running_pid(_record(child.pid, home), expected_home=home) == child.pid


def test_a_sibling_profiles_daemon_is_not_this_profiles_gateway(tmp_path, spawn):
    """The environment answer stays exact: the sibling's daemon is still another profile's."""
    ops = (tmp_path / "profiles" / "ops").resolve()
    sibling = (tmp_path / "profiles" / "mediabuyer").resolve()
    ops.mkdir(parents=True)
    sibling.mkdir(parents=True)
    child = spawn(ops)

    assert live_process_hermes_home(child.pid) == ops
    assert status._record_matches_live_gateway_pid(_record(child.pid, ops), child.pid, expected_home=sibling) is False
    assert status.get_runtime_status_running_pid(_record(child.pid, ops), expected_home=sibling) is None


def test_an_explicit_profile_flag_on_argv_still_wins_over_the_environment(tmp_path, spawn):
    """An argv identity is a deliberate launch spelling: the environment never overrides it."""
    ops = (tmp_path / "profiles" / "ops").resolve()
    mediabuyer = (tmp_path / "profiles" / "mediabuyer").resolve()
    ops.mkdir(parents=True)
    mediabuyer.mkdir(parents=True)

    # argv names the sibling while the environment names the profile: argv wins, in both directions.
    foreign = spawn(ops, "-p", "mediabuyer", "gateway", "run")
    assert status._record_matches_live_gateway_pid(
        _record(foreign.pid, ops), foreign.pid, expected_home=ops
    ) is False

    own = spawn(mediabuyer, "-p", "ops", "gateway", "run")
    assert status._record_matches_live_gateway_pid(
        _record(own.pid, mediabuyer), own.pid, expected_home=ops
    ) is True

    # Same for an explicit HERMES_HOME= on argv: it is argv's identity, never the environment's.
    explicit = spawn(ops, "HERMES_HOME=/elsewhere/profiles/ops", "gateway", "run")
    assert status._record_matches_live_gateway_pid(
        _record(explicit.pid, ops), explicit.pid, expected_home=ops
    ) is False


def test_an_unreadable_process_environment_answers_none(tmp_path):
    """A gone PID keeps the caller on the argv-only rule it had before this module existed."""
    home = (tmp_path / "profiles" / "ops").resolve()
    home.mkdir(parents=True)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=30)

    assert live_process_hermes_home(gone.pid) is None
