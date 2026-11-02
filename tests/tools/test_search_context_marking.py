"""#147722: ``context=N`` rows are marked, and ``total_count`` counts matching lines.

``context=N`` used to append rg's neighbouring rows to ``matches`` as if they were
matches, so one hit with ``context=1`` answered ``total_count: 3`` and nothing in
the payload said which row actually matched. A page is now ``limit`` MATCHES, each
carrying up to ``context`` rows on either side, and every surrounding row is
flagged ``is_context``.
"""

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ExecuteResult, ShellFileOperations
from tools.file_operations_common import SearchMatch, SearchResult
from tools.file_operations_search import _parse_search_output

pytestmark = pytest.mark.platforms("posix")  # the end-to-end cases drive the native rg lane


@pytest.fixture(scope="module")
def _env(tmp_path_factory):
    return LocalEnvironment(cwd=str(tmp_path_factory.mktemp("context-marking")))


def _ops(env, cwd):
    env.cwd = str(cwd)
    return ShellFileOperations(env, cwd=str(cwd))


def _rows(result) -> list[tuple[int, bool]]:
    """``(line, is_context)`` per returned row, read defensively so the control run
    on the pre-fix code fails on the assertion instead of at construction."""
    return [(m.line_number, bool(getattr(m, "is_context", False))) for m in result.matches]


def test_total_count_counts_matching_lines_not_context_rows(tmp_path, _env):
    (tmp_path / "note.md").write_text("line one\nline two\nCodeword: HERON-41.\nline four\nline five\n")

    result = _ops(_env, tmp_path).search("HERON", path=str(tmp_path), target="content", context=1)

    assert result.error is None
    assert result.total_count == 1
    assert _rows(result) == [(2, True), (3, False), (4, True)]
    payload = result.to_dict()
    assert payload["total_count"] == 1
    assert [row.get("is_context") for row in payload["matches"]] == [True, None, True]


def test_pages_are_made_of_matches_and_carry_their_own_context(tmp_path, _env):
    (tmp_path / "hits.md").write_text("hit one\nb\nc\nhit two\ne\n")

    ops = _ops(_env, tmp_path)
    first = ops.search("hit", path=str(tmp_path), target="content", context=1, limit=1)
    second = ops.search("hit", path=str(tmp_path), target="content", context=1, limit=1, offset=1)
    beyond = ops.search("hit", path=str(tmp_path), target="content", context=1, limit=1, offset=2)

    # Two matching lines, so `total_count` is 2 on every page of the same search.
    assert (first.total_count, second.total_count, beyond.total_count) == (2, 2, 2)
    assert first.truncated is True and second.truncated is False
    # One match per page, each with the rows around it — context never eats the budget.
    assert _rows(first) == [(1, False), (2, True)]
    assert _rows(second) == [(3, True), (4, False), (5, True)]
    assert beyond.matches == [] and beyond.truncated is False


def test_densified_text_block_marks_context_rows():
    def row(line, content, context=False):
        match = SearchMatch(path="a.py", line_number=line, content=content)
        match.is_context = context  # set after construction: the field does not exist pre-fix
        return match

    result = SearchResult(
        total_count=2,
        matches=[row(10, "ctx before", True), row(11, "needle one"), row(12, "ctx after", True),
                 row(30, "needle two"), row(31, "ctx after two", True)],
    )

    payload = result.to_dict(densify=True)

    assert "  11: needle one" in payload["matches_text"]
    assert "  12- ctx after" in payload["matches_text"]
    assert "context" in payload["matches_format"]


def test_shell_transport_splits_hunks_and_keeps_the_match_count():
    output = "\n".join([
        "note.md-2-line two",
        "note.md:3:Codeword: HERON-41.",
        "note.md-4-line four",
        "--",
        "other.md:9:Codeword: HERON-99.",
    ])

    result = _parse_search_output(ExecuteResult(stdout=output, exit_code=0), "content", 10, 0, 1)

    assert result.total_count == 2
    assert _rows(result) == [(2, True), (3, False), (4, True), (9, False)]
