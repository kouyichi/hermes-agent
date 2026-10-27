"""A restored occurrence whose dispatch never claims the slot must not re-loop every tick (#145564).

The tick restores a pending slot as the due instant and then, in the same pass, stamps
``pending_slot`` again for that instant before dispatch. That second stamp is the crash window's
protection, not a retry budget: when the dispatch is REFUSED or FAILS instead of the process dying
(an executor error, an unwritable executions store, a pool held by a wedged run), nothing claims
the slot, so the next tick finds the same instant pending and restores it again — one warning per
tick, with the job pinned to that instant for as long as the failure lasts (161 restores in 160
minutes in the report). The restore is therefore bounded per instant: after
``MAX_SLOT_RESTORES`` attempts the schedule moves on to the next occurrence.

Drives the REAL ``tick()`` against a throwaway HERMES_HOME with a ``no_agent`` script job that
appends one line per fire.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

try:  # the per-instant budget this test asserts; absent on the base tree
    from cron.occurrences import MAX_SLOT_RESTORES
except ImportError:  # pragma: no cover - base tree
    MAX_SLOT_RESTORES = 3


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "cron" / "output").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)

    import cron.executions as E
    import cron.jobs as J
    import cron.occurrences as O
    import cron.scheduler as S

    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(E, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    monkeypatch.setattr(S, "_hermes_home", home)
    S._running_job_ids.clear()
    S._running_since.clear()
    S._running_futures.clear()

    counter = home / "fires.txt"
    scratch = home / "scripts" / "fire.sh"
    scratch.write_text(f"#!/bin/sh\necho fired >> {counter}\necho fired\n", encoding="utf-8")
    scratch.chmod(0o755)
    job = J.create_job(prompt=None, schedule="every 1h", name="slot", script="fire.sh",
                       no_agent=True, deliver="local")
    slot = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    stored = J.load_jobs()
    record = next(r for r in stored if r["id"] == job["id"])
    # Exactly what a tick leaves behind when its dispatch never claims the slot: the schedule holds
    # the stored instant and the stamp records THIS owner (so no lease guesswork is involved).
    record["next_run_at"] = slot
    record["pending_slot"] = {"scheduled_at": slot, "at": slot, "by": J._machine_id()}
    J.save_jobs(stored)

    def fires() -> int:
        return counter.read_text(encoding="utf-8").count("\n") if counter.exists() else 0

    def restores(monkeypatch) -> list[str]:
        """Record every restore warning as the scheduler emits it, independent of log config.

        Wraps both module loggers: the restore policy may sit in either module, and a missing
        capture must not read as "no restore happened"."""
        seen: list[str] = []

        def collect(*a, **k):
            seen.append(a[0] % a[1:])

        monkeypatch.setattr(J.logger, "warning", collect)
        monkeypatch.setattr(O.logger, "warning", collect)
        return seen

    yield {"job_id": job["id"], "slot": slot, "fires": fires, "restores": restores,
           "S": S, "J": J, "E": E}
    S._shutdown_parallel_pool()


class TestRestoredSlotBudget:
    def test_a_slot_dispatch_never_claims_is_given_up_after_the_budget(self, cron_env, monkeypatch):
        S, J = cron_env["S"], cron_env["J"]
        seen = cron_env["restores"](monkeypatch)

        def refuse(*a, **k):
            raise RuntimeError("executions store is read-only")

        # A persistent dispatch failure: every attempt raises before the slot can be claimed.
        monkeypatch.setattr(S, "create_execution", refuse)
        for _ in range(MAX_SLOT_RESTORES + 3):
            S.tick(verbose=False, sync=True)

        restores = [line for line in seen if "restoring it as the due instant" in line]
        assert len(restores) == MAX_SLOT_RESTORES, (
            f"the restore must be bounded per instant, saw {len(restores)}: {restores}")
        rec = J.get_job(cron_env["job_id"])
        assert "pending_slot" not in rec, f"the given-up slot is still stamped pending: {rec}"
        assert J._ensure_aware(J.datetime.fromisoformat(rec["next_run_at"])) > J._hermes_now(), (
            f"the job is still pinned to the stale instant: next_run_at={rec['next_run_at']}")
        assert cron_env["fires"]() == 0

    def test_the_job_fires_its_next_occurrence_once_dispatch_works(self, cron_env, monkeypatch):
        S, J = cron_env["S"], cron_env["J"]
        cron_env["restores"](monkeypatch)

        def refuse(*a, **k):
            raise RuntimeError("executions store is read-only")

        real_create = S.create_execution
        monkeypatch.setattr(S, "create_execution", refuse)
        for _ in range(MAX_SLOT_RESTORES + 3):
            S.tick(verbose=False, sync=True)
        # Only the dispatch failure clears; the fixture's store redirection must stay in place
        # (monkeypatch.undo() would restore the operator's real HERMES_HOME paths too).
        monkeypatch.setattr(S, "create_execution", real_create)

        # The failure clears and the schedule reaches its next occurrence.
        next_slot = (J._hermes_now() - timedelta(seconds=5)).replace(microsecond=0).isoformat()
        stored = J.load_jobs()
        next(r for r in stored if r["id"] == cron_env["job_id"])["next_run_at"] = next_slot
        J.save_jobs(stored)

        S.tick(verbose=False, sync=True)
        assert cron_env["fires"]() == 1, "a job given up on one occurrence must still fire the next"
        rec = J.get_job(cron_env["job_id"])
        assert "pending_slot" not in rec
        from cron.occurrences import scheduled_instant
        assert cron_env["E"].latest_execution(cron_env["job_id"])["scheduled_instant"] == scheduled_instant(next_slot)

    def test_a_single_dead_owner_still_restores_and_fires_once(self, cron_env, monkeypatch):
        """#107485's contract: one crash-restart pair restores the occurrence instead of losing it."""
        S, J = cron_env["S"], cron_env["J"]
        cron_env["restores"](monkeypatch)

        S.tick(verbose=False, sync=True)
        assert cron_env["fires"]() == 1, "the restored occurrence must run once"
        from cron.occurrences import scheduled_instant
        row = cron_env["E"].latest_execution(cron_env["job_id"])
        assert row["scheduled_instant"] == scheduled_instant(cron_env["slot"]), "restored slot keeps its identity"
        rec = J.get_job(cron_env["job_id"])
        assert "pending_slot" not in rec
        assert J._ensure_aware(J.datetime.fromisoformat(rec["next_run_at"])) > J._hermes_now()
        S.tick(verbose=False, sync=True)
        assert cron_env["fires"]() == 1, "a restored slot never replays"
