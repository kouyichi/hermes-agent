"""#143149: the Windows Scheduled Task must be registrable by the account it runs as, and a refused
re-registration must never leave the user with *less* autostart than before it started.

Windows is not needed for either half: ``_build_scheduled_task_xml`` is pure text, and
``_install_scheduled_task`` reaches the OS only through ``_exec_schtasks`` (faked here as the genuine
external dependency, the way the neighbouring suite does). The fake keeps one registered task's XML so
``/Query`` → ``/Delete`` → ``/Create`` can be answered the way ``schtasks`` would, and it refuses a
create by *content* — the reporter's refusal came from the template, so re-registering the XML that
was already there is allowed, exactly as it was on the machine that hit this.
"""

from pathlib import Path

import pytest

from hermes_cli import gateway_windows

_TASK = "Hermes_Gateway_alice"
_USER = r"PC\alice"

# A registration from before the hardened template: version 1.3, no RestartOnFailure.
_OLD_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>PC\\alice</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//B //Nologo "C:\\Users\\alice\\.hermes\\gateway-service\\Hermes_Gateway_alice.vbs"</Arguments>
    </Exec>
  </Actions>
</Task>
"""


class _TaskRegistry:
    """Stand-in for ``schtasks``: one registered task, plus the create refusals this bug needs.

    ``refuse="template"`` answers ``Access is denied`` to a create whose XML differs from the task's
    starting registration (the reporter's template was the unregistrable part) while still accepting
    a re-registration of that starting XML; ``refuse="all"`` refuses every create.
    """

    def __init__(self, xml: str | None = None, refuse: str = "none") -> None:
        self.xml = xml
        self.original = xml
        self.refuse = refuse
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[0]
        if verb == "/Query":
            if self.xml is None:
                return (1, "", "ERROR: The system cannot find the file specified.")
            return (0, self.xml, "")
        if verb == "/Delete":
            existed = self.xml is not None
            self.xml = None
            return (0, "SUCCESS", "") if existed else (1, "", "ERROR: The system cannot find the file specified.")
        if verb == "/Create":
            payload = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
            if self.refuse == "all" or (self.refuse == "template" and payload != self.original):
                return (1, "", "ERROR: Access is denied.")
            self.xml = payload
            return (0, "SUCCESS", "")
        raise AssertionError(f"unexpected schtasks call: {args}")

    def creates(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "/Create"]


@pytest.fixture
def script_path(monkeypatch, tmp_path):
    service = tmp_path / "gateway-service"
    service.mkdir()
    script = service / f"{_TASK}.cmd"
    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: _USER)
    monkeypatch.setattr(gateway_windows, "get_task_script_path", lambda: script)
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: script)
    return script


# ── the template ────────────────────────────────────────────────────────────────────────────────

def test_logon_trigger_names_the_run_as_user_before_the_delay(tmp_path):
    """A ``<LogonTrigger>`` with no ``<UserId>`` means "at logon of any user", which Windows only
    lets an administrator register — the reason a standard account could not recreate its own task.
    The schema's element sequence inside the trigger is ``Enabled`` → ``UserId`` → ``Delay``."""
    xml = gateway_windows._build_scheduled_task_xml(_TASK, tmp_path / f"{_TASK}.vbs", _USER)
    trigger = xml.split("<LogonTrigger>", 1)[1].split("</LogonTrigger>", 1)[0]
    assert f"<UserId>{_USER}</UserId>" in trigger
    assert trigger.index("<UserId>") < trigger.index("<Delay>")
    # The principal keeps its own <UserId>: the trigger's copy must not replace it.
    principal = xml.split('<Principal id="Author">', 1)[1].split("</Principal>", 1)[0]
    assert f"<UserId>{_USER}</UserId>" in principal


def test_unknown_user_leaves_both_user_ids_absent(tmp_path):
    """``_resolve_task_user`` answers None when the environment names no user; the template must stay
    registrable in that shape too (no empty ``<UserId></UserId>``, which schtasks rejects)."""
    xml = gateway_windows._build_scheduled_task_xml(_TASK, tmp_path / f"{_TASK}.vbs", None)
    assert "<UserId>" not in xml


# ── a refused re-registration must not cost the existing task ──────────────────────────────────

def test_successful_replace_registers_once_and_restores_nothing(script_path, monkeypatch):
    registry = _TaskRegistry(xml=_OLD_TASK_XML)
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", registry)

    ok, detail = gateway_windows._install_scheduled_task(_TASK, script_path)

    assert ok is True
    assert detail == f"Created Scheduled Task {_TASK!r}"
    assert len(registry.creates()) == 1
    registered = registry.xml
    assert registered is not None and "<RestartOnFailure>" in registered


def test_refused_registration_restores_the_previous_task(script_path, monkeypatch):
    """The install deletes first (``/Create /F`` is the only verb that re-applies every leaf), so
    when the new registration is refused the exported old XML is re-registered: the account keeps
    the autostart it had (#143149)."""
    registry = _TaskRegistry(xml=_OLD_TASK_XML, refuse="template")
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", registry)

    ok, detail = gateway_windows._install_scheduled_task(_TASK, script_path)

    assert ok is False
    assert "Access is denied" in detail
    assert "previous registration was restored" in detail
    assert registry.xml == _OLD_TASK_XML
    assert not (script_path.with_suffix(".task.xml")).exists()


def test_refused_fresh_install_reports_the_manual_repair(script_path, monkeypatch):
    """Nothing to restore on a first install: say so instead of implying the task came back."""
    registry = _TaskRegistry(xml=None, refuse="template")
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", registry)

    ok, detail = gateway_windows._install_scheduled_task(_TASK, script_path)

    assert ok is False
    assert "hermes gateway install" in detail
    assert "no previous registration to restore" in detail
    assert registry.xml is None


def test_refused_restore_is_reported_honestly(script_path, monkeypatch):
    """A restore that is refused too must not read as a successful one."""
    registry = _TaskRegistry(xml=_OLD_TASK_XML, refuse="all")
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", registry)

    ok, detail = gateway_windows._install_scheduled_task(_TASK, script_path)

    assert ok is False
    assert "could not be restored" in detail
    assert "hermes gateway install" in detail
    assert registry.xml is None


def test_reconcile_keeps_the_old_task_when_the_repair_is_refused(script_path, monkeypatch):
    """End to end over the drift path a gateway start/``hermes update`` takes: the repair is refused,
    and the task that was there before the repair is still registered afterwards."""
    registry = _TaskRegistry(xml=_OLD_TASK_XML, refuse="template")
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", registry)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)

    assert gateway_windows.reconcile_scheduled_task(_TASK) is False
    assert registry.xml == _OLD_TASK_XML
