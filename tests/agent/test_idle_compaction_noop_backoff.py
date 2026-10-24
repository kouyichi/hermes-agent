"""Idle-triggered compaction: a pass that reclaims nothing must not repeat.

``_should_idle_compact`` keeps a no-growth floor from
``ContextCompressor.last_compression_rough_tokens`` — what the previous pass actually
produced (#97239). An engine that never reports that number (the LCM engine logs
``last compaction produced ~n/a``) leaves the floor at its theoretical value, so a pass
that returned the transcript unchanged is re-run -- full summariser call included -- on
every later idle resume, forever (#143897).

The harness (idle-eligible agent + prologue driver) is shared with
``test_idle_compaction_lock_and_guards``; these helpers are plain functions, not
fixtures.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hermes_state import SessionDB

from tests.agent.test_idle_compaction_lock_and_guards import (
    _history,
    _pin_compress_seam,
    _prep_idle_agent,
    _run_prologue,
)

_ESTIMATE = 127_000


def _prep_silent_engine_agent(db: SessionDB, sid: str) -> Any:
    """Idle-eligible agent whose compressor reports no produced size.

    Threshold 127,510 x ratio 0.20 gives the theoretical 25,502 floor the reported
    session kept clearing; ``last_compression_rough_tokens = 0`` is the shape an engine
    that cannot report its own output leaves behind, so the #97239 floor cannot apply.
    """
    agent = _prep_idle_agent(db, sid, idle_after=1, idle_gap=3660.0)
    agent.context_compressor.threshold_tokens = 127_510
    agent.context_compressor.summary_target_ratio = 0.20
    agent.context_compressor.last_compression_rough_tokens = 0
    agent.context_compressor.awaiting_real_usage_after_compression = False
    return agent


def _idle_resume(agent, rough_tokens: int = _ESTIMATE):
    """One idle resume of an unchanged transcript.

    ``_last_activity_ts`` is re-aged per call so the wall-clock gap (not a fresh
    activity stamp) is what decides: every rescue below is the idle gate's own.
    """
    agent._last_activity_ts = time.time() - 3660.0
    agent.context_compressor.awaiting_real_usage_after_compression = False
    return _run_prologue(agent, _history(), rough_tokens=rough_tokens)


def test_idle_resume_does_not_repeat_a_pass_that_reclaimed_nothing(tmp_path: Path) -> None:
    """The reported loop: 62 no-op passes on one session, one every cron tick."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_NOOP_ENGINE"
    db.create_session(sid, source="cli")
    agent = _prep_silent_engine_agent(db, sid)
    seam = _pin_compress_seam(agent)

    _idle_resume(agent)
    assert seam.call_count == 1, "control: the first idle resume must reach compression"

    # The seam returns the input list: the pass ran and reclaimed nothing, and the
    # transcript has not grown. A second full summarisation buys the same nothing.
    _idle_resume(agent)
    assert seam.call_count == 1, (
        "a no-op idle pass repeated on the next idle resume: the floor still came from "
        "the theoretical target because the engine reported no produced size"
    )


def test_idle_resume_fires_again_once_the_transcript_grows(tmp_path: Path) -> None:
    """The backoff is a floor, not a latch: a floor's worth of new content re-arms it."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_NOOP_GROWTH"
    db.create_session(sid, source="cli")
    agent = _prep_silent_engine_agent(db, sid)
    seam = _pin_compress_seam(agent)

    _idle_resume(agent)
    _idle_resume(agent)
    assert seam.call_count == 1

    _idle_resume(agent, rough_tokens=_ESTIMATE + 30_000)
    assert seam.call_count == 2, "a transcript that grew past the floor must be compacted again"
