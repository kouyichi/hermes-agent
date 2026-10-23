"""A messages row without an integer id must not abort the compaction commit (#143402).

``messages.id`` is ``INTEGER PRIMARY KEY AUTOINCREMENT`` in ``SCHEMA_SQL``, so on a store this code
created every row carries an id. A table from a pre-PK layout keeps ``id INTEGER`` instead, and the
INSERT path (``_INSERT_MESSAGE_SQL`` omits ``id`` on purpose) then leaves every appended row NULL —
808k such rows on the reporting install. The in-place compaction commit names rows by id
(``_matching_active_ids``, ``_matching_retired_ids``, ``_merged_user_run``, ``_resolve_carried_row_ids``,
``_archive_named_rows``), and one ``int(None)`` rolled the whole transaction back: the summary the
provider already paid for was discarded, the session stayed oversized, and the next turn re-ran the
same failing commit (measured: 14 occurrences over 3 days).

A NULL-id row can never be addressed by ``UPDATE ... WHERE id IN (...)`` anyway, so the commit must
skip it — the store already degrades one unusable cell rather than killing the operation
(``test_corrupt_row_robustness``). The session-wide ``_ARCHIVE_ACTIVE_SQL`` still archives it, so the
compaction lands and the compacted transcript becomes the active set.
"""

import sqlite3

import pytest

from hermes_state import SessionDB

LEGACY_ID_LINE = "id INTEGER PRIMARY KEY AUTOINCREMENT"
MESSAGES = {
    0: ("user", "msg 0"),
    1: ("assistant", "msg 1"),
    2: ("user", "msg 2"),
    3: ("assistant", "msg 3"),
}


def _legacy_messages_db(tmp_path) -> SessionDB:
    """A ``state.db`` whose messages table stores ``id INTEGER`` with no PRIMARY KEY.

    Built by rewriting the shipped table's id line, so the fixture keeps every column and trigger the
    real store has: this is the shape the reporter's upgraded install carried (all ids NULL), only
    reached without hand-maintaining a copy of the DDL.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", "cli", model="test/model")
    for role, content in MESSAGES.values():
        db.append_message("s1", role, content)
    ddl = db._conn.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
    stored = [tuple(row) for row in db._conn.execute(
        "SELECT session_id, role, content, timestamp FROM messages WHERE session_id='s1'").fetchall()]
    db.close()

    legacy = ddl.replace(LEGACY_ID_LINE, "id INTEGER")
    assert legacy != ddl, "messages DDL changed; re-anchor this fixture's id line"
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        conn.execute("DROP TABLE messages")
        conn.execute(legacy)
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)", stored)
        conn.commit()
    finally:
        conn.close()
    return SessionDB(db_path=tmp_path / "state.db")


def _null_id_rows(db) -> int:
    return db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='s1' AND id IS NULL").fetchone()[0]


def _active_contents(db) -> list[str]:
    return [row["content"] for row in db._conn.execute(
        "SELECT content FROM messages WHERE session_id='s1' AND active=1 ORDER BY id")]


class TestUnnameableRowIds:
    def test_the_fixture_really_has_null_ids(self, tmp_path):
        """Guard the fixture itself: the shape under test only exists while ids stay NULL."""
        db = _legacy_messages_db(tmp_path)
        try:
            assert _null_id_rows(db) == len(MESSAGES)
        finally:
            db.close()

    def test_covered_commit_skips_a_persisted_row_it_cannot_name(self, tmp_path):
        """The reported frame: a loaded held dict the coverage matcher cannot name must not abort."""
        from agent.context_compressor import _DB_PERSISTED_MARKER

        db = _legacy_messages_db(tmp_path)
        try:
            inserted = db.archive_and_compact(
                "s1", [{"role": "user", "content": "summary of prior turns"}],
                covered_ids=[],
                unresolved_held=[{"role": "user", "content": "msg 2", _DB_PERSISTED_MARKER: True}])
            assert _active_contents(db) == ["summary of prior turns"]
            assert inserted == 1
        finally:
            db.close()

    def test_carried_tail_commit_skips_rows_it_cannot_name(self, tmp_path):
        """The carried-tail lane resolves originals by id too; an unnameable row must be skipped."""
        db = _legacy_messages_db(tmp_path)
        try:
            inserted = db.archive_and_compact(
                "s1", [{"role": "user", "content": "summary of prior turns"}],
                carried_messages=[{"role": "assistant", "content": "msg 3"}])
            assert _active_contents(db) == ["summary of prior turns"]
            assert inserted == 1
        finally:
            db.close()

    def test_named_rows_still_commit_when_some_rows_are_unnameable(self, tmp_path):
        """A store carrying a few real ids (an import) plus NULL-id appends: the named lane runs."""
        db = _legacy_messages_db(tmp_path)
        try:
            def _stamp(conn):
                conn.execute("UPDATE messages SET id = 1 WHERE session_id='s1' AND content='msg 0'")
                conn.execute("UPDATE messages SET id = 2 WHERE session_id='s1' AND content='msg 1'")

            db._execute_write(_stamp)
            assert _null_id_rows(db) == len(MESSAGES) - 2
            db.archive_and_compact(
                "s1", [{"role": "user", "content": "summary of prior turns"}], covered_ids=[1, 2])
            assert _active_contents(db) == ["summary of prior turns"]
        finally:
            db.close()

    def test_coverage_helpers_return_no_unnameable_id(self, tmp_path):
        """Every id projection on the commit path answers "cannot name it", not an exception."""
        from agent.conversation_compression_archive import RETIRED_ROW

        db = _legacy_messages_db(tmp_path)
        try:
            conn = db._conn
            assert db._matching_active_ids(conn, "s1", {"role": "user", "content": "msg 2"}) == []
            assert db._matching_retired_ids(
                conn, "s1", {"role": "user", "content": "msg 2", RETIRED_ROW: True}) == []
            assert db._resolve_carried_row_ids(
                conn, "s1", [{"role": "assistant", "content": "msg 3"}]) == []
            assert db._merged_user_run(
                conn, "s1",
                {"role": "user", "content": "msg 0\nmsg 2", "merged_durable_rows": 2}) == []
        finally:
            db.close()
