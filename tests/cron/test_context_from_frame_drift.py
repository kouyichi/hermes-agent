"""A prompt stamp that drifted from the stored prompt must not lose the answer (#146017).

``_archive_answer`` finds the writer's response frame by adding the ``**Prompt Characters:**``
stamp to the end of the prompt frame. When the stored prompt is shorter than its stamp (measured
drift: +9 characters on two skill-bearing jobs), that offset overshoots the frame, ``match()``
misses it, and ``context_from`` silently injects nothing even though the run produced an answer.

Drift tolerance must not cost the property the length frames exist for: a truncated write stays
unusable, and a frame quoted inside the prompt or the answer is never promoted to a boundary.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _archive(prompt: str, response: str, *, drift: int = 0) -> str:
    """A run document as ``cron.scheduler.run_job`` writes it, with an optional stamp drift."""
    return (
        "# Cron Job: probe\n\n"
        "**Job ID:** abcdef\n"
        "**Run Time:** 2026-10-29 08:00:00\n"
        f"**Prompt Characters:** {len(prompt) + drift}\n"
        f"## Prompt\n\n{prompt}\n\n"
        f"**Response Characters:** {len(response)}\n"
        f"## Response\n\n{response}\n"
    )


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron output tree (this module's own copy: a fixture cannot be imported)."""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


def _write_archive(cron_env, job_id: str, filename: str, body: str) -> None:
    from cron.jobs import OUTPUT_DIR

    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(body, encoding="utf-8")


class TestDriftRecovery:
    def test_drifted_prompt_stamp_still_finds_the_response(self, caplog):
        from cron.scheduler_prompt import _archive_answer

        with caplog.at_level("WARNING", logger="cron.scheduler"):
            answer = _archive_answer(_archive("Report the weather", "ANSWER-MARKER-42", drift=9))

        assert answer == "ANSWER-MARKER-42"
        assert "drift" in caplog.text.lower()

    def test_drift_is_recovered_through_the_injection_lane(self, cron_env):
        from cron.jobs import create_job
        from cron.scheduler_prompt import _inject_context_from

        job = create_job(prompt="Report", schedule="0 8 * * *", context_from="self")
        _write_archive(cron_env, job["id"], "2026-10-29_08-00-00.md",
                       _archive("Summarize yesterday's incidents", "PREVIOUS-ANSWER-77", drift=9))

        prompt, injected = _inject_context_from(job, "Report")

        assert injected is True
        assert "PREVIOUS-ANSWER-77" in prompt
        assert "Summarize yesterday's incidents" not in prompt

    def test_drift_in_the_other_direction_is_recovered(self):
        from cron.scheduler_prompt import _archive_answer

        assert _archive_answer(_archive("Report", "ANSWER-MARKER-43", drift=-6)) == "ANSWER-MARKER-43"


class TestDriftToleranceKeepsTheFramesAuthority:
    def test_a_truncated_response_stays_unusable_under_drift(self):
        from cron.scheduler_prompt import _archive_answer

        complete = _archive("Report the weather", "ANSWER-MARKER-42 keeps going", drift=9)

        assert _archive_answer(complete[: complete.rindex("keeps going")]) is None

    def test_a_frame_quoted_in_the_answer_is_not_promoted_under_drift(self):
        from cron.scheduler_prompt import _archive_answer

        quoted = "QUOTED INNER ANSWER"
        response = (
            "Outer response introduction\n"
            f"**Response Characters:** {len(quoted)}\n## Response\n\n{quoted}"
        )

        assert _archive_answer(_archive("Report", response, drift=9)) == response

    def test_a_frame_quoted_in_the_prompt_is_not_promoted_under_drift(self):
        from cron.scheduler_prompt import _archive_answer

        quoted = "PROMPT-QUOTED BODY"
        prompt = f"Use this format:\n**Response Characters:** {len(quoted)}\n## Response\n\n{quoted}"

        assert _archive_answer(_archive(prompt, "REAL-ANSWER-91", drift=9)) == "REAL-ANSWER-91"


class TestUnframedArchivesAreUnchanged:
    def test_no_response_frame_at_all_is_still_unusable(self):
        from cron.scheduler_prompt import _archive_answer

        archive = _archive("Report", "irrelevant", drift=9)
        truncated = archive.split("**Response Characters:**", 1)[0]

        assert _archive_answer(truncated) is None
