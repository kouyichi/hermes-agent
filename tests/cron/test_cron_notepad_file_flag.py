"""``hermes cron notepad <job> set <key>`` can take the value from a file or stdin.

A multi-kilobyte notepad entry (a daily brief) had to travel as a quoted here-doc inside the
terminal tool's ``eval``, where the model kept getting the quoting wrong (#147688). ``--file PATH``
and ``--file -`` (stdin) let the caller pipe the text instead, with the same 16 KB per-value /
64 KB per-job caps as the positional form.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def notepad(monkeypatch, tmp_path):
    import cron.notepad as notepad_mod

    monkeypatch.setattr(notepad_mod, "NOTEPAD_FILE", tmp_path / "cron" / "notepad.db")
    return notepad_mod


def _ns(**kw):
    base = dict(job_id="job-1", notepad_action="set", key="brief", value=None, value_file=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cron_parser_exposes_the_file_flag():
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser()
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda _args: 0)
    ns = parser.parse_args(["cron", "notepad", "job-1", "set", "brief", "--file", "brief.txt"])
    assert ns.value_file == "brief.txt"
    assert ns.value is None


def test_set_reads_the_value_from_a_file(notepad, tmp_path, capsys):
    from hermes_cli.cron import cron_notepad

    source = tmp_path / "brief.txt"
    source.write_text("Morning brief\nsecond line\n", encoding="utf-8-sig")

    assert cron_notepad(_ns(value_file=str(source))) == 0
    # The newline the writer left behind is not part of the entry.
    assert notepad.get_note("job-1", "brief") == "Morning brief\nsecond line"
    assert "brief" in capsys.readouterr().out


def test_set_reads_the_value_from_stdin(notepad, monkeypatch):
    from hermes_cli.cron import cron_notepad

    monkeypatch.setattr(sys, "stdin", io.StringIO("piped brief\n"))
    assert cron_notepad(_ns(value_file="-")) == 0
    assert notepad.get_note("job-1", "brief") == "piped brief"


def test_a_value_and_a_file_together_are_refused(notepad, tmp_path, capsys):
    from hermes_cli.cron import cron_notepad

    source = tmp_path / "brief.txt"
    source.write_text("from file", encoding="utf-8-sig")

    assert cron_notepad(_ns(value="from argv", value_file=str(source))) == 1
    assert "either" in capsys.readouterr().out.lower()
    assert notepad.get_note("job-1", "brief") is None


def test_an_unreadable_file_writes_nothing(notepad, tmp_path, capsys):
    from hermes_cli.cron import cron_notepad

    missing = tmp_path / "nope.txt"
    assert cron_notepad(_ns(value_file=str(missing))) == 1
    assert str(missing) in capsys.readouterr().out
    assert notepad.get_note("job-1", "brief") is None


def test_the_per_value_cap_still_applies_to_a_file(notepad, tmp_path, capsys):
    from hermes_cli.cron import cron_notepad

    oversized = tmp_path / "big.txt"
    oversized.write_text("x" * (notepad.MAX_VALUE_BYTES + 1), encoding="utf-8-sig")

    assert cron_notepad(_ns(value_file=str(oversized))) == 1
    assert "too large" in capsys.readouterr().out
    assert notepad.get_note("job-1", "brief") is None


def test_set_still_requires_a_value_or_a_file(notepad, capsys):
    from hermes_cli.cron import cron_notepad

    assert cron_notepad(_ns(value=None, value_file=None)) == 1
    assert "Usage" in capsys.readouterr().out
