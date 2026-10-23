"""The terminal spill file must hold the command's own output.

The bash wrapper appends a per-session ``__HERMES_CWD_<session>__<path>__HERMES_CWD_<session>__``
line to every command's stream; ``BaseEnvironment._extract_cwd_from_output`` strips it from the
returned window, so the spill file used to be the only surface that still carried it (#143632).
"""

import json
from pathlib import Path

import pytest

from tools.environments.base_output import _BoundedOutputCollector


@pytest.fixture
def small_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_constants import hermes_home_key
    import tools.tool_output_limits as lim
    monkeypatch.setattr(lim, "_cached_limits", {hermes_home_key(): {
        "max_bytes": 2000, "max_lines": 2000, "max_line_length": 2000,
    }})
    return tmp_path


def _big_json(directory: Path, rows: int = 200) -> tuple[Path, str]:
    payload = json.dumps([{"i": i, "pad": "x" * 200} for i in range(rows)])
    path = directory / "big.json"
    path.write_text(payload, encoding="utf-8")
    return path, payload


def _big_text(directory: Path, rows: int = 300) -> tuple[Path, str]:
    payload = "".join(f"row_{i} {'z' * 80}\n" for i in range(rows))
    path = directory / "big.txt"
    path.write_text(payload, encoding="utf-8")
    return path, payload


@pytest.mark.platforms("linux")
def test_spilled_json_parses_and_carries_no_cwd_marker(small_cap):
    from tools.terminal_tool import terminal_tool

    source, payload = _big_json(small_cap)
    result = json.loads(terminal_tool(f"cat {source}", task_id="t-spill-marker-1"))

    assert result["exit_code"] == 0
    spill = Path(result["full_output_path"])
    text = spill.read_text(encoding="utf-8")
    assert "__HERMES_CWD_" not in text
    assert json.loads(text) == json.loads(payload)


@pytest.mark.platforms("linux")
def test_spilled_plain_text_is_the_command_output_verbatim(small_cap):
    from tools.terminal_tool import terminal_tool

    source, payload = _big_text(small_cap)
    result = json.loads(terminal_tool(f"cat {source}", task_id="t-spill-marker-2"))

    text = Path(result["full_output_path"]).read_text(encoding="utf-8")
    assert "__HERMES_CWD_" not in text
    assert text == payload


@pytest.mark.platforms("linux")
def test_cwd_is_still_reported_for_a_spilling_command(small_cap):
    from tools.terminal_tool import terminal_tool

    source, _ = _big_json(small_cap)
    target = small_cap / "sub"
    target.mkdir()
    result = json.loads(terminal_tool(
        f"cd {target} && cat {source}", task_id="t-spill-marker-3", workdir=str(small_cap)))

    # The marker is stripped from the spill tee only: cwd extraction reads the same
    # buffered window as before, and the returned output never shows the marker.
    assert result["cwd"] == str(target)
    assert "__HERMES_CWD_" not in result["output"]


def test_marker_split_across_stream_chunks_never_reaches_the_spill(tmp_path):
    marker = "__HERMES_CWD_probe__"
    spill = tmp_path / "out.log"
    collector = _BoundedOutputCollector(50, spill_path=spill, cwd_marker=marker)

    for chunk in ("x" * 40, "\n" + marker, "/tmp", marker, "\n"):
        collector.append(chunk)

    assert collector.close_spill() == str(spill)
    assert spill.read_text(encoding="utf-8") == "x" * 40


def test_a_lone_marker_in_command_output_is_preserved(tmp_path):
    marker = "__HERMES_CWD_probe__"
    spill = tmp_path / "out.log"
    collector = _BoundedOutputCollector(50, spill_path=spill, cwd_marker=marker)
    body = marker + " appears once\n" + "y" * 80

    collector.append(body)

    assert collector.close_spill() == str(spill)
    # No complete marker pair means no transport marker: the text is the command's own.
    assert spill.read_text(encoding="utf-8") == body
