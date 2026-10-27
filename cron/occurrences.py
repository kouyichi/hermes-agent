"""Exact scheduled identities, independent of mutable jobs.json dispatch stamps, plus the
profile-local stale-schedule catch-up counter marker."""
from datetime import datetime, timedelta, timezone, UTC
import logging

logger = logging.getLogger(__name__)


def scheduled_instant(value):
    """Canonicalize aware instants; legacy/ambiguous values carry no exact identity."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            return None
        return instant.astimezone(UTC).isoformat()
    except ValueError:
        return None


def completed_occurrence(job, instant):
    """Unknown/failed/pruned attempts cannot prove completion: keep them eligible."""
    from cron.constants import FIRE_CLAIM_SKEW_SECONDS
    from cron.executions import _transaction

    instant = scheduled_instant(instant)
    if instant is None:
        return False
    # A skewed early fire (see claim_job_for_fire) legitimately completes just before its slot.
    earliest_real = datetime.fromisoformat(instant) - timedelta(seconds=FIRE_CLAIM_SKEW_SECONDS)
    try:
        with _transaction() as conn:
            rows = conn.execute(
                "SELECT id, finished_at, claimed_at FROM executions "
                "WHERE job_id=? AND scheduled_instant=? "
                "AND status='completed'", (str(job['id']), instant)
            ).fetchall()
        for row in rows:
            completed_at = scheduled_instant(row["finished_at"] or row["claimed_at"])
            # Legacy or malformed timestamps remain proof; only positively identified poison
            # rows — completions recorded before their claimed occurrence — are ignored.
            if completed_at is None or datetime.fromisoformat(completed_at) >= earliest_real:
                # Both dedup gates (due scan and fire claim) consume the slot on True without a
                # run or a ledger row, so this line is the only trace the skip leaves (#111414).
                logger.warning(
                    "Job '%s' (%s): scheduled occurrence %s was already completed by execution "
                    "%s (finished %s); skipping the due slot without a new run",
                    job.get("name", job.get("id")), job.get("id"), instant, row["id"],
                    row["finished_at"] or row["claimed_at"])
                return True
        return False
    except Exception:
        logger.warning("Cannot check completed occurrence for job %s", job['id'], exc_info=True)
        return False


# --- Pending slot: the occurrence a tick took off the schedule but has not yet claimed ---
#
# The tick advances a recurring job's ``next_run_at`` BEFORE dispatch (at-most-once: a crash
# mid-run must not re-fire on every restart). That leaves a window — advance done, fire claim
# not yet taken (interpreter finalizing, executor refusing work, process killed) — in which the
# process exiting loses the occurrence silently: the restarted scan sees a future ``next_run_at``
# and nothing ever ran (#107485). ``pending_slot`` is the durable record of that window: the due
# scan stamps it with the exact stored instant plus the stamping owner, ``claim_job_for_fire``
# (the point after which side effects may exist) clears it, and any explicit rewrite of the
# schedule drops it. A slot still pending once its owner is provably gone (or its lease has
# expired) was never claimed, so it is restored ONCE as the due instant and then flows through
# the ordinary late / fast-forward / ``cron.catch_up_missed`` policy — never N replays.

def pending_slot_stamp(next_run, now):
    """Store value for a recurring occurrence about to be handed to the dispatcher."""
    from cron.jobs import _machine_id

    return {"scheduled_at": next_run, "at": now.isoformat(), "by": _machine_id()}


def unclaimed_pending_slot(job, now):
    """Stored instant of a slot the dispatcher never claimed, or None.

    None for non-recurring jobs and malformed stamps (never a fire) and for a job still in this
    process's running set (its queued worker will claim and clear the slot itself). A stamp by
    THIS process on a job not running here is orphaned (dispatch refused). A stamp by another
    process is honoured while that owner may still be alive within the fire-claim lease — a
    second live gateway on the same store is mid-dispatch, not dead."""
    from cron.constants import FIRE_CLAIM_TTL_SECONDS
    from cron.jobs import _claim_is_live, _job_running_in_this_process, _machine_id

    pending = job.get("pending_slot")
    if not isinstance(pending, dict):
        return None
    slot = pending.get("scheduled_at")
    if job.get("schedule", {}).get("kind") not in {"cron", "interval"} or not isinstance(slot, str):
        return None
    try:
        datetime.fromisoformat(slot)
    except ValueError:
        return None
    if _job_running_in_this_process(str(job.get("id", ""))):
        return None
    if pending.get("by") != _machine_id() and _claim_is_live(pending, now, FIRE_CLAIM_TTL_SECONDS):
        return None
    return slot


# --- Bounded restore: the crash window is not a retry budget ---
#
# The tick that restores a pending slot re-stamps it in the SAME pass, before dispatch — that
# second stamp is the crash window's protection, not a retry. So a dispatch that is refused or
# failing rather than dead (executor error, unwritable executions store, a pool held by a wedged
# run) leaves the stamp behind with nothing to claim it, and the next tick restores the same
# instant again: one warning per tick, the job pinned to that instant for as long as the failure
# lasts (161 restores across 160 minutes in #145564). The restore is therefore counted per stored
# instant; past the budget the schedule moves past the occurrence instead of replaying it.
MAX_SLOT_RESTORES = 3


def restore_unclaimed_slot(job, scan, slot):
    """Put *slot* back as the due instant, at most ``MAX_SLOT_RESTORES`` times per instant.

    Returns the instant the caller must run, or None when the occurrence has spent its attempts:
    then ``next_run_at`` is advanced past it and both stamps are dropped, so a dispatch that can
    never claim the slot stops re-looping the schedule. The count is keyed by the stored instant, so
    a fresh occurrence starts with the full budget, and a single crash-restart pair still restores
    (#107485).
    """
    seen = job.get("restored_slot")
    attempts = (int(seen.get("count") or 0)
                if isinstance(seen, dict) and seen.get("scheduled_at") == slot else 0)
    if attempts >= MAX_SLOT_RESTORES:
        return _give_up_restored_slot(job, scan, slot, attempts)
    logger.warning(
        "Job '%s' (%s): occurrence %s was taken off the schedule but never claimed "
        "(scheduler stopped before dispatch); restoring it as the due instant (was %s, attempt %d/%d).",
        job.get("name", job.get("id")), job.get("id"), slot, job.get("next_run_at"),
        attempts + 1, MAX_SLOT_RESTORES)
    job["next_run_at"] = slot
    job["restored_slot"] = {"scheduled_at": slot, "count": attempts + 1}
    _drop_pending_stamp(job, scan)
    scan.persist(job["id"], next_run_at=slot, restored_slot=job["restored_slot"])
    return slot


def _give_up_restored_slot(job, scan, slot, attempts):
    """Advance past an occurrence that has been restored ``attempts`` times and never claimed."""
    from cron.jobs import compute_next_run

    nxt = compute_next_run(job.get("schedule") or {}, scan.now.isoformat()) or job.get("next_run_at")
    logger.warning(
        "Job '%s' (%s): occurrence %s was restored %d times but still was not claimed "
        "(dispatch refused or failing); giving it up and moving to %s instead of re-pinning it.",
        job.get("name", job.get("id")), job.get("id"), slot, attempts, nxt)
    job.pop("restored_slot", None)
    job["next_run_at"] = nxt
    raw = scan.find(job.get("id"))
    if raw is not None:
        raw.pop("restored_slot", None)
    _drop_pending_stamp(job, scan)
    scan.persist(job["id"], next_run_at=nxt)


def _drop_pending_stamp(job, scan):
    """Clear the window stamp on the scan's copy and on the raw record it saves."""
    job.pop("pending_slot", None)
    raw = scan.find(job.get("id"))
    if raw is not None:
        raw.pop("pending_slot", None)


def get_catch_up_occurrence_count() -> int:
    """Return the profile-local stale-schedule catch-up count."""
    from cron.jobs import _current_cron_store

    path = _current_cron_store().cron_dir / "catch_up_occurrences"
    try:
        return max(0, int(path.read_text(encoding="utf-8-sig").strip()))
    except (OSError, ValueError):
        return 0


def record_catch_up_occurrence() -> None:
    """Increment the profile-local stale-schedule catch-up counter, best effort."""
    from cron.jobs import _write_marker

    _write_marker("catch_up_occurrences", str(get_catch_up_occurrence_count() + 1), ".count_")
