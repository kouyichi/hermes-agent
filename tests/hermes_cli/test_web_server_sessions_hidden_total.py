"""``GET /api/sessions`` must report a total its own rows can reach (#142313).

The dashboard pairs ``total`` with the paginated rows, so a hidden row counted in the total but
absent from every page leaves a "load more" cursor that can never be satisfied.
"""

import pytest


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    TestClient = pytest.importorskip("starlette.testclient").TestClient

    import hermes_state
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from hermes_constants import get_hermes_home

    db_path = get_hermes_home() / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    seed = hermes_state.SessionDB(db_path=db_path)
    try:
        for session_id in ("visible", "hidden"):
            seed.create_session(session_id, source="cli")
        seed.set_session_hidden("hidden", True)
    finally:
        seed.close()

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def test_total_excludes_the_hidden_rows_the_page_omits(client):
    response = client.get("/api/sessions?limit=50&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert [row["id"] for row in payload["sessions"]] == ["visible"]
    assert payload["total"] == 1


def test_stats_endpoint_still_counts_the_whole_store(client):
    """The stats widget is a STORE statistic, not a page total: hidden rows are still stored."""
    response = client.get("/api/sessions/stats")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["active_store"] == 2
    assert payload["by_source"] == {"cli": 2}
    assert payload["archived"] == 0
