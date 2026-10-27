"""A stale reap counts toward the unified breaker only when the run never heartbeated.

``detect_stale_running`` reclaims a ``running`` card whose worker stopped reporting: the worker is
killed, the run closes ``outcome='stale'``, the card returns to its source phase. For a long-running
worker that heartbeated and then wedged, that is a benign requeue and must stay counter-neutral —
counting it would let legitimate multi-hour tasks trip the breaker.

A worker that dies BEFORE its first heartbeat is the other shape. Its card makes no progress
whatsoever, the symptom repeats deterministically (an endpoint that is down, a poisoned prompt, a
dead credential), and without accounting the dispatcher loops spawn -> stale reap -> requeue forever,
unbounded by both ``kanban.failure_limit`` and a task's ``max_retries``. ``_touch_activity`` bridges
the agent's chunk-level liveness into ``last_heartbeat_at``, so a NULL there means the run produced
no progress at all, not merely "a quiet worker".

So: tick the counter for exactly that shape, through the same ``_record_task_failure`` every other
non-success outcome uses, and let the existing threshold (``max_retries`` > ``failure_limit`` >
``DEFAULT_FAILURE_LIMIT``) park the card. The stored reason starts with ``stale-loop`` so an operator
can tell a spawn-loop block from a worker fault.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


def count_stale_loop(
    conn: sqlite3.Connection,
    task_id: str,
    elapsed_seconds: float,
    *,
    failure_limit: Optional[int] = None,
) -> bool:
    """Tick the breaker for one never-heartbeated stale reap; True when the card was parked.

    Runs OUTSIDE the reap's write txn: ``_record_task_failure`` opens its own. The caller has
    already closed the run and restored the source phase, so this moves the counter only (no
    claim release, no second run close) and appends ``gave_up`` when the threshold is reached.
    """
    from hermes_cli.kanban_db_dispatch import _record_task_failure

    return _record_task_failure(
        conn,
        task_id,
        f"stale-loop: no heartbeat ever after {int(elapsed_seconds)}s running",
        outcome="stale",
        failure_limit=failure_limit,
    )
