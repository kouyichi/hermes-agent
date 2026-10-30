"""A fork's recency is its own creation, not the day its parent last spoke.

A branch child copies its parent's visible transcript WITH the parent's timestamps (deliberate: the
copy is history). Session recency is the freshest of ``last_activity_at``, the latest message and
``started_at``, so with the child's ``last_activity_at`` empty and every copied row dated, a fork of a
long-idle chat listed itself among the sessions of the day its parent last spoke — burying the new
fork far down the sidebar (#146712).
"""

from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path


def _store(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return SessionDB(home / "state.db")


def _branch_and_list(tmp_path, monkeypatch):
    from tui_gateway import server

    now = time.time()
    old = now - 30 * 86400
    history = [{"role": "user", "content": "long ago turn", "timestamp": old},
               {"role": "assistant", "content": "long ago reply", "timestamp": old + 45}]
    with closing(_store(tmp_path, monkeypatch)) as db:
        db.create_session("parent", source="desktop", model="m")
        db._conn.execute("UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
                         (old - 600, old + 45, "parent"))
        db._conn.commit()
        db.append_messages_batch("parent", [dict(message) for message in history])

        # A session that is genuinely ten days old: the fork has to sort above THIS one.
        db.create_session("other", source="desktop", model="m")
        db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 10 * 86400, "other"))
        db._conn.commit()
        db.append_message("other", "user", "ten days ago", timestamp=now - 10 * 86400 + 60)

        # The real writer every branch lane funnels through: session.branch, session.branch_stored
        # and the seeded desktop child (_seed_branch_row).
        server._persist_branch(db, "child", "parent", "Branch", [dict(message) for message in history],
                               source="desktop", cwd=str(tmp_path), profile_name="default",
                               model="m", copy_fields=("timestamp",), title_source="derived")

        child = db.get_session("child")
        listing = db.list_sessions_rich(limit=10, order_by_last_active=True, include_children=True)
    return now, child, {row["id"]: row["last_active"] for row in listing}, [row["id"] for row in listing]


def test_a_branch_child_carries_its_own_recency_not_the_copied_one(tmp_path, monkeypatch):
    now, child, last_active, _ = _branch_and_list(tmp_path, monkeypatch)

    assert child["parent_session_id"] == "parent", "lineage is preserved"
    assert child["started_at"] >= now - 60, "the fork is a new row"
    assert last_active["child"] >= child["started_at"] - 1, "the fork's recency is its own creation"


def test_the_fork_sorts_above_the_session_it_copied_from(tmp_path, monkeypatch):
    _, _, _, order = _branch_and_list(tmp_path, monkeypatch)

    assert order[0] == "child", "a fresh fork must not sort below a session that is genuinely older"


def test_the_copied_parent_keeps_its_own_recency(tmp_path, monkeypatch):
    """Control: the stamp lands on the child only — the parent's history is still its own."""
    _, _, last_active, _ = _branch_and_list(tmp_path, monkeypatch)

    assert last_active["parent"] < time.time() - 25 * 86400, "the parent still reads as month-old"


def _publish_child(tmp_path, monkeypatch, model_config):
    """The canonical local branch/reset lane (``gateway`` RPCs publish through this helper)."""
    with closing(_store(tmp_path, monkeypatch)) as db:
        now = time.time()
        old = now - 30 * 86400
        db.create_session("parent", source="desktop", model="m")
        db._conn.execute("UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
                         (old - 600, old + 45, "parent"))
        db._conn.commit()
        parent = dict(db._conn.execute("SELECT * FROM sessions WHERE id = ?", ("parent",)).fetchone())
        db._execute_write(lambda conn: db._publish_child_session_row(
            conn, parent, parent_session_id="parent", child_session_id="child", source="desktop",
            model="m", model_config=model_config, system_prompt=None, cwd=None, profile_name="default"))
        return now, db.get_session("child")


def test_a_published_branch_child_starts_its_own_activity(tmp_path, monkeypatch):
    now, child = _publish_child(tmp_path, monkeypatch, {"_branched_from": "parent"})

    assert child["started_at"] >= now - 60
    assert child["last_activity_at"] >= child["started_at"] - 1


def test_a_published_compression_child_keeps_the_lineage_recency(tmp_path, monkeypatch):
    """Control: a compression continuation IS the same conversation — it must not look freshly used."""
    _, child = _publish_child(tmp_path, monkeypatch, {})

    assert child["last_activity_at"] is None, "a continuation keeps deriving recency from its history"


def test_a_published_reset_child_keeps_the_never_active_shape(tmp_path, monkeypatch):
    """Control: a reset child starts empty, so it must stay eligible for the never-active sweep."""
    _, child = _publish_child(tmp_path, monkeypatch, {"_reset_from": "parent"})

    assert child["last_activity_at"] is None, "reset is not a copied transcript"
