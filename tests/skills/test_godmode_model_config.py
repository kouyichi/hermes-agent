"""Regression tests for the bundled godmode skill's model detection.

``auto_jailbreak()`` detects the current model from ``<HERMES_HOME>/config.yaml``.
The documented schema spells it ``model.default`` (``model.provider`` /
``model.base_url`` beside it); ``model.name`` is the legacy form, and a bare
string ``model:`` is also accepted. Reading only ``name`` made every config
written in the current schema look like a missing model, so the pipeline
returned "No model specified and couldn't read config.yaml" before it ever
reached the transport (#142286). A config.yaml that could not be read or parsed
produced the same message, so a broken file read as "no model configured".

The probe runs the script in a child interpreter because importing it executes
its sibling modules into the caller's globals.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "optional-skills" / "security" / "godmode" / "scripts" / "auto_jailbreak.py"

_PROBE = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path

    script = Path(sys.argv[1])
    ns = {"__file__": str(script), "__name__": "godmode_model_config_probe"}
    exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), ns)

    model, base_url = ns["_get_current_model"]()
    reader = ns.get("_read_model_config")
    if reader is None:  # pre-fix script: no split between "no field" and "unreadable"
        read_model, read_base_url, problem = model, base_url, "<no _read_model_config>"
    else:
        read_model, read_base_url, problem = reader()
    pipeline = ns["auto_jailbreak"](dry_run=True, verbose=False)
    print(json.dumps({
        "model": model,
        "base_url": base_url,
        "read_model": read_model,
        "read_base_url": read_base_url,
        "problem": problem,
        "pipeline_error": pipeline.get("error", ""),
    }))
    """
)


def _probe(tmp_path: Path, config: str | None) -> dict:
    """Run the probe against a scratch HERMES_HOME holding *config* (None = no file)."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and k != "HERMES_HOME"}
    env["HERMES_HOME"] = str(home)
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_default_key_is_the_current_schema(tmp_path):
    """The schema Hermes writes today must be detected, not reported as missing."""
    result = _probe(tmp_path, "model:\n  default: fixture-model\n  provider: openai-codex\n")
    assert result["model"] == "fixture-model"
    assert result["problem"] == ""
    # Detection got past the model step and stopped on the credential, i.e. the
    # reported symptom ("No model specified ...") is gone. No request is made.
    assert result["pipeline_error"] == "No API key found"


def test_legacy_name_form_still_works(tmp_path):
    result = _probe(tmp_path, "model:\n  name: legacy-fixture-model\n  provider: openrouter\n")
    assert result["model"] == "legacy-fixture-model"
    assert result["problem"] == ""


def test_string_form_still_works(tmp_path):
    result = _probe(tmp_path, "model: string-fixture-model\n")
    assert result["model"] == "string-fixture-model"
    assert result["problem"] == ""


def test_legacy_name_wins_and_empty_name_falls_back(tmp_path):
    both = _probe(tmp_path, "model:\n  name: legacy-fixture-model\n  default: current-fixture-model\n")
    assert both["model"] == "legacy-fixture-model"
    empty = _probe(tmp_path / "empty", "model:\n  name: ''\n  default: current-fixture-model\n")
    assert empty["model"] == "current-fixture-model"


def test_base_url_rides_along(tmp_path):
    result = _probe(
        tmp_path,
        "model:\n  default: fixture-model\n  base_url: https://fixture.invalid/v1\n",
    )
    assert result["read_base_url"] == "https://fixture.invalid/v1"


def test_unreadable_config_is_reported_as_such(tmp_path):
    """A corrupt file must not read as "no model": the two need different fixes."""
    broken = _probe(tmp_path / "broken", "model: [unclosed\n")
    assert broken["model"] is None
    assert "could not be read" in broken["problem"]
    assert broken["pipeline_error"].startswith("Could not read")

    missing = _probe(tmp_path / "missing", None)
    assert missing["model"] is None
    assert "does not exist" in missing["problem"]


def test_config_without_a_model_field_is_not_a_read_error(tmp_path):
    result = _probe(tmp_path, "providers: {}\n")
    assert not result["model"]
    assert result["problem"] == ""
    assert "model.default" in result["pipeline_error"]
