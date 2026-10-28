"""Transport diagnostics must never be parsed as a command's own output (#146135).

The Docker and SSH backends spawn their *client* with ``stderr=STDOUT``
(``tools/environments/base_output.py::_popen_bash``), so anything the local client prints is
prepended to the remote command's stdout — most visibly the OpenSSH identity warning a stale
``terminal.ssh_key`` produces:

    Warning: Identity file /home/user/.ssh/id_rsa not accessible: No such file or directory.

The read path already survives that: it decodes a sentinel-fenced segment and validates the
segment's shape (``_fenced_read``, ``_matches_size``). The three probes below used to read the
reply positionally instead, which turned the warning into a digest, a "command exists" verdict
and a ripgrep path — see the issue for the user-visible fallout (``write_file`` reporting a hash
mismatch for a write that persisted, ``search_files`` failing on a populated tree).
"""

import base64
import importlib
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

# What the local ssh client prints on stderr before the remote command's own bytes. The
# transport merges the two streams, so every command's stdout carries it first.
SSH_CLIENT_NOISE = (
    "Warning: Identity file /home/agent/.ssh/id_rsa not accessible: "
    "No such file or directory.\n"
)

PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _noisy(ops):
    """Make every command's stdout carry the ssh client's warning first."""
    real_exec = ops._exec

    def noisy_exec(command, *args, **kwargs):
        result = real_exec(command, *args, **kwargs)
        return type(result)(
            stdout=SSH_CLIENT_NOISE + (result.stdout or ""),
            exit_code=result.exit_code,
            cwd_error=result.cwd_error,
        )

    ops._exec = noisy_exec
    return ops


class _NoisyEnv:
    """A backend whose command output is prefixed with the client's own diagnostics."""

    env_type = "ssh"

    def __init__(self, reply: str, returncode: int = 0):
        self.cwd = "/workspace"
        self.reply = reply
        self.returncode = returncode
        self.commands: list[str] = []

    def execute(self, command, cwd=None, **kwargs):
        self.commands.append(command)
        return {"output": SSH_CLIENT_NOISE + self.reply, "returncode": self.returncode}


def test_write_file_verifies_a_digest_the_transport_prefixed(tmp_path, monkeypatch):
    """The bytes landed, so a warning ahead of the digest must not read as a mismatch."""
    env = LocalEnvironment(cwd=str(tmp_path))
    ops = ShellFileOperations(env, cwd=str(tmp_path))
    # Take the digest from the shell, exactly as an ssh/docker backend must.
    monkeypatch.setattr(ops, "_native_read_enabled", lambda: False)
    _noisy(ops)

    target = tmp_path / "note.txt"
    result = ops.write_file(str(target), "hello\n")

    assert target.read_text(encoding="utf-8") == "hello\n"
    assert result.error is None


def test_command_probe_answers_true_behind_client_noise(tmp_path, monkeypatch):
    """``_has_command`` decides a command's existence from its own reply."""
    ops = _noisy(ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path)))

    assert ops._has_command("sh") is True
    # The warning's own words must not be mistaken for a hit either.
    assert ops._has_command("hermes-no-such-command-146135") is False


def test_rg_resolution_ignores_client_noise():
    """The resolved rg path is what the sandbox's shell printed, not the warning line."""
    env = _NoisyEnv("/usr/bin/rg\n")
    ops = ShellFileOperations(env, cwd="/workspace")

    assert ops._resolve_command("rg") == "/usr/bin/rg"
    assert "rg" in env.commands[-1]


@pytest.fixture(autouse=True)
def _no_real_sandbox_bringup(monkeypatch):
    """Keep the resolver's lazy sandbox bring-up from spawning a real ssh/docker env."""
    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, "ensure_task_env", lambda *a, **k: None)


def _reload(monkeypatch, hermes_home):
    from tools import image_source

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import hermes_constants

    importlib.reload(hermes_constants)
    importlib.reload(image_source)
    return image_source


@pytest.mark.asyncio
async def test_container_image_read_ignores_client_noise(tmp_path, monkeypatch):
    """The sandbox's base64 payload decodes even when the client warned first."""
    image_source = _reload(monkeypatch, tmp_path / "hermes")
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    encoded = base64.b64encode(PNG).decode()

    def fake_execute(cmd, **kwargs):
        # A shell that echoes the read's marker first, then its base64 payload — with the
        # client's warning merged in ahead, as the ssh/docker transport delivers it.
        marker = re.search(r"__HERMES_IS_[0-9a-f]{32}__", cmd)
        body = f"{marker.group(0)}\n{encoded}" if marker else encoded
        return {"returncode": 0, "output": SSH_CLIENT_NOISE + body}

    with patch(
        "tools.image_source._get_active_env",
        return_value=SimpleNamespace(execute=fake_execute),
    ):
        resolved = await image_source.resolve_image_source(
            "/workspace/chart.png", image_source.ResolveContext(task_id="t1")
        )

    assert resolved.origin == "container"
    assert resolved.data == PNG
