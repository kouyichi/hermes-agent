"""``session_count``/``session_count_by_source`` must mirror ``list_sessions_rich``'s visibility.

Hidden rows (Bot Mode marks its own sessions hidden, and a pin clears the flag) are excluded from
every default listing, so a count served beside that listing has to exclude them too — otherwise a
paginated page reports a total its rows can never reach (#142313). The archived-only recovery view
is the one surface that must keep hidden rows: it exists so a hidden+archived row stays reachable
(#90946).
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    for session_id in ("visible", "hidden", "archived-hidden"):
        store.create_session(session_id, source="gui")
        store.set_session_title(session_id, session_id)
    store.set_session_hidden("hidden", True)
    store.set_session_hidden("archived-hidden", True)
    store.set_session_archived("archived-hidden", True)
    try:
        yield store
    finally:
        store.close()


def _ids(rows):
    return sorted(row["id"] for row in rows)


def test_count_matches_the_listing_it_is_paired_with(db):
    listed = db.list_sessions_rich(limit=50)

    assert _ids(listed) == ["visible"]
    assert db.session_count(exclude_children=True) == len(listed)


def test_count_hides_hidden_rows_with_the_listing_defaults(db):
    """Same defaults, same answer: a hidden row is not a row the page can show."""
    assert db.session_count() == len(db.list_sessions_rich(limit=50))


def test_count_by_source_hides_hidden_rows(db):
    assert db.session_count_by_source(exclude_children=True) == {"gui": 1}


def test_hidden_rows_stay_countable_on_request(db):
    """Storage/status callers opt back in; only the default follows the listing."""
    assert db.session_count(include_hidden=True) == 2  # visible + hidden; archived stays out
    assert db.session_count_by_source(include_hidden=True) == {"gui": 2}


def test_archived_only_recovery_view_keeps_hidden_rows(db):
    """#90946: a hidden+archived row must stay reachable through the recovery view."""
    assert _ids(db.list_sessions_rich(archived_only=True, limit=50)) == ["archived-hidden"]
    assert db.session_count(archived_only=True) == 1
