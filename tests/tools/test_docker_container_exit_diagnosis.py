"""A docker sandbox that cannot exec reports the entrypoint's own failure (issue #146746).

``docker exec`` answers ``container … is not running`` for any container whose PID 1 already
exited, so the daemon reply names the symptom and discards the cause. On a host whose LSM stack
refuses ``--security-opt no-new-privileges`` the sandbox entrypoint cannot exec at all, every
command fails with that one opaque line, and the operator is never told that one flag is the
reason — ``snap_compat`` works only for a reader of the source.

These cases drive ``DockerEnvironment.execute()`` with the base runner replaced, because the
defect is what the TOOL RESULT says, not how the CLI is spawned.
"""
import subprocess

import pytest

from tools.environments import docker as docker_env
from tools.environments.base import BaseEnvironment

_GONE_OUTPUT = "Error response from daemon: container c0ffee123456 is not running"


def _env(*, snap_compat: bool = False, persist: bool = False, run_args: list[str] | None = None):
    """A DockerEnvironment without constructor side effects, for the execute() path only."""
    env = docker_env.DockerEnvironment.__new__(docker_env.DockerEnvironment)
    env.cwd = "/root"
    env.timeout = 60
    env._container_id = "c0ffee123456"
    env._docker_exe = "/usr/bin/docker"
    env._snap_compat = snap_compat
    env._persist_across_processes = persist
    env._all_run_args = (
        ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        if run_args is None else run_args
    )

    return env


def _fake_docker(monkeypatch, *, running: bool, exit_code: int, log_lines: list[str],
                 inspect_fails: bool = False) -> list[list[str]]:
    """Answer the diagnosis probes (``inspect`` / ``logs``) and record every docker argv."""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        if "inspect" in cmd:
            if inspect_fails:
                raise OSError("docker: not installed")

            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{'true' if running else 'false'} {exit_code}\n", stderr="")
        if "logs" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(log_lines), stderr="")
        # Other probes (ownership handback's ``docker info``) run on their own thread.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)

    return calls


def _stub_base_execute(monkeypatch, results: list[dict]):
    """Replace the inherited runner so each call returns the next canned tool result."""
    remaining = list(results)

    def _execute(self, command, cwd="", **kwargs):
        return dict(remaining.pop(0)) if remaining else {"returncode": 0, "output": ""}

    monkeypatch.setattr(BaseEnvironment, "execute", _execute)


def test_a_sandbox_that_cannot_exec_names_the_no_new_privileges_flag(monkeypatch):
    """Exit 126 + "operation not permitted" is the LSM/no-new-privileges family: name both."""
    _fake_docker(monkeypatch, running=False, exit_code=126,
                 log_lines=["exec /usr/bin/sleep: operation not permitted"])
    _stub_base_execute(monkeypatch, [{"returncode": 1, "output": _GONE_OUTPUT}])

    output = _env().execute("echo hello")["output"]

    assert _GONE_OUTPUT in output
    assert "exit code 126" in output
    assert "exec /usr/bin/sleep: operation not permitted" in output
    assert "docker_snap_compat" in output


def test_the_remedy_is_not_offered_when_the_flag_was_already_dropped(monkeypatch):
    """snap_compat already omits the flag, so pointing at it again would be a dead end."""
    _fake_docker(monkeypatch, running=False, exit_code=126,
                 log_lines=["exec /usr/bin/sleep: operation not permitted"])
    _stub_base_execute(monkeypatch, [{"returncode": 1, "output": _GONE_OUTPUT}])

    output = _env(snap_compat=True, run_args=["--cap-drop", "ALL"]).execute("echo hello")["output"]

    assert "exit code 126" in output
    assert "docker_snap_compat" not in output


def test_an_unrelated_entrypoint_failure_is_reported_without_a_remedy(monkeypatch):
    """The exit code and the container's own log line are worth showing on their own."""
    _fake_docker(monkeypatch, running=False, exit_code=127,
                 log_lines=["standard_init_linux.go:211: exec user process caused: no such file or directory"])
    _stub_base_execute(monkeypatch, [{"returncode": 1, "output": _GONE_OUTPUT}])

    output = _env().execute("echo hello")["output"]

    assert "exit code 127" in output
    assert "no such file or directory" in output
    assert "docker_snap_compat" not in output


def test_a_container_that_is_still_running_leaves_the_error_untouched(monkeypatch):
    """A container that dies between the exec and the probe must not gain a false diagnosis."""
    _fake_docker(monkeypatch, running=True, exit_code=0, log_lines=[])
    _stub_base_execute(monkeypatch, [{"returncode": 1, "output": _GONE_OUTPUT}])

    assert _env().execute("echo hello")["output"] == _GONE_OUTPUT


def test_an_unreachable_docker_cli_keeps_the_original_error(monkeypatch):
    """Diagnosis is best-effort: a failed probe never replaces the daemon's own message."""
    _fake_docker(monkeypatch, running=False, exit_code=0, log_lines=[], inspect_fails=True)
    _stub_base_execute(monkeypatch, [{"returncode": 1, "output": _GONE_OUTPUT}])

    assert _env().execute("echo hello")["output"] == _GONE_OUTPUT


def test_recovery_is_still_attempted_before_any_diagnosis(monkeypatch):
    """The out-of-band-removal recovery keeps precedence; a recovered run reports nothing."""
    calls = _fake_docker(monkeypatch, running=False, exit_code=126, log_lines=["boom"])
    _stub_base_execute(monkeypatch, [{"returncode": 1, "output": _GONE_OUTPUT},
                                     {"returncode": 0, "output": "hello"}])
    env = _env(persist=True)
    monkeypatch.setattr(env, "_recreate_container", lambda: True)

    assert env.execute("echo hello")["output"] == "hello"
    assert not any("inspect" in call for call in calls)


@pytest.mark.parametrize("state", ["true 0", "false 126"])
def test_state_probe_is_parsed_from_the_inspect_format(monkeypatch, state):
    """The probe reads ``{{.State.Running}} {{.State.ExitCode}}`` as inspect prints it."""
    running, exit_code = state.split()
    calls = _fake_docker(monkeypatch, running=running == "true", exit_code=int(exit_code),
                         log_lines=["exec /usr/bin/sleep: operation not permitted"])
    _stub_base_execute(monkeypatch, [{"returncode": 1, "output": _GONE_OUTPUT}])

    output = _env().execute("echo hello")["output"]

    assert any("inspect" in call for call in calls)
    assert ("exit code 126" in output) is (running == "false")
