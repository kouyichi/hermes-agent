"""Security-boundary regressions for the TUI ``shell.exec`` RPC."""

from __future__ import annotations

from agent import redact
from tui_gateway import server


def test_shell_exec_scrubs_child_env_and_force_redacts_rpc_output(monkeypatch):
    secret = "sk-shell-exec-secret-1234567890"
    captured = {}

    class _FakePopen:
        """The RPC's spawn seam: ``shell.exec`` reaps its own process group now (#147103), so
        the child is started through ``capture_exec.run_captured`` rather than ``subprocess.run``."""

        def __init__(self, command, **kwargs):
            captured.update(kwargs)
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            return f"visible\n{secret}\n", f"diagnostic {secret}"

    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    monkeypatch.setattr("tui_gateway.capture_exec.subprocess.Popen", _FakePopen)

    response = server.handle_request(
        {"id": "security", "method": "shell.exec", "params": {"command": "echo safe"}}
    )

    assert "OPENROUTER_API_KEY" not in captured["env"]
    assert secret not in response["result"]["stdout"]
    assert secret not in response["result"]["stderr"]
    assert "visible" in response["result"]["stdout"]
