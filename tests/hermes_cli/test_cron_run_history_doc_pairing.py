"""Regression: one execution must not render as two rows when the run outlives its session.

A cron run's session can end mid-run (context compression closes it) while the run keeps
working, and its output doc is only written at the true end of the run. Reconciliation
paired that doc with a legacy session (one that names no execution) by the DOC's timestamp
inside ``[session.started_at - grace, session.ended_at + grace]``, so a tail longer than
``_SESSION_DOC_MATCH_SECONDS`` left the doc unpaired and the Desktop run history rendered one
execution as two rows (#144338). Pairing also accepts a legacy session that BEGAN inside the
writing attempt's run window, which is exact where the doc's timestamp is not: a run's
session is opened at the fire, so its start sits inside ``[claimed_at, finished_at + grace]``
whatever the run's tail.

The cases below also pin that the wider pairing never folds a doc into a session from a
DIFFERENT run: a script-only fire keeps its own row, and two consecutive agent runs each
keep one.
"""

import hermes_cli.web_routers.cron as cron_routes

T = 1_000_000.0


def _attempt(attempt_id: str, claimed: float, finished: float) -> dict:
    return {"id": attempt_id, "status": "completed", "claimed_at": claimed,
            "finished_at": finished, "error": None}


def _legacy_session(session_id: str, started: float, ended: float) -> dict:
    """A run session from an install that predates execution ids: it names no execution."""
    return {"id": session_id, "cron_execution_id": None, "started_at": started,
            "ended_at": ended, "last_active": ended, "title": "job run"}


def _doc(doc_id: str, started: float) -> dict:
    return {"id": f"cron_output:job1:{doc_id}", "started_at": started, "title": "job run output"}


def _ids(sessions, docs, attempts) -> list[str]:
    rows = cron_routes._reconcile_cron_runs(sessions, docs, attempts, "job1", 50)
    return [row["id"] for row in rows]


def test_a_run_that_outlives_its_session_keeps_one_row():
    """The reported case: the doc lands past ended_at + 300s, and must still fold in."""
    for tail in (0, 200, 500, 900, 1057, 1200, 1500):
        session = [_legacy_session("s1", T, T + 100)]
        attempts = [_attempt("e1", T, T + tail)]
        docs = [_doc("doc1", T + tail)]

        assert _ids(session, docs, attempts) == ["s1"], f"tail={tail}"
        # The tail is what decides it on main: past session_span + grace the doc used to
        # survive as its own row, which is the doubled execution the report describes.
        assert cron_routes._doc_matches_session(
            float(docs[0]["started_at"]), session[0], cron_routes._SESSION_DOC_MATCH_SECONDS
        ) == (tail <= 400)


def test_a_script_only_fire_still_keeps_its_own_row():
    """An attempt that wrote a doc and started no session is its own execution, not a tail."""
    session = [_legacy_session("s1", T, T + 100)]
    attempts = [_attempt("e1", T, T + 150), _attempt("e2", T + 5000, T + 5010)]
    docs = [_doc("doc2", T + 5010), _doc("doc1", T + 150)]

    assert _ids(session, docs, attempts) == ["cron_output:job1:doc2", "s1"]


def test_consecutive_runs_each_fold_their_own_doc():
    """Two runs with a long tail each: neither doc may surface as a second row."""
    sessions = [_legacy_session("s2", T + 700, T + 800), _legacy_session("s1", T, T + 100)]
    attempts = [_attempt("e2", T + 700, T + 1350), _attempt("e1", T, T + 150)]
    docs = [_doc("doc2", T + 1350), _doc("doc1", T + 150)]

    assert _ids(sessions, docs, attempts) == ["s2", "s1"]
