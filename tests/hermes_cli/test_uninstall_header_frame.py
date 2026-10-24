"""The uninstall heading frame's three rows share one width (#143925).

The heading rows were hand-padded literals, and the two titles carrying ``☤`` were one
cell short: their trailing ``│`` sat a column left of the rules above and below them.
Building every row from one interior width keeps the frame square for any title,
including ones whose glyphs are wider than one cell.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.utils import get_cwidth

from hermes_cli import uninstall

# The four headings the uninstall screens print, as the module spells them.
TITLES = (
    "☤ Hermes Chat GUI Uninstaller",
    "✓ Chat GUI Uninstalled!",
    "☤ Hermes Agent Uninstaller",
    "✓ Uninstall Complete!",
)


def _rows(capsys, title: str) -> list[str]:
    uninstall._print_box(title, uninstall.Colors.MAGENTA)
    return capsys.readouterr().out.splitlines()


def _assert_square(rows: list[str]) -> None:
    assert len(rows) == 3
    widths = {get_cwidth(row) for row in rows}
    assert len(widths) == 1, f"frame rows differ in width: {[get_cwidth(r) for r in rows]}"
    # The rules sit under the corners, so the two ends of every row line up.
    assert rows[0].startswith("┌") and rows[0].endswith("┐")
    assert rows[1].startswith("│") and rows[1].endswith("│")
    assert rows[2].startswith("└") and rows[2].endswith("┘")


@pytest.mark.parametrize("title", TITLES)
def test_heading_rows_line_up(capsys, title):
    rows = _rows(capsys, title)
    _assert_square(rows)
    assert title in rows[1]


def test_wide_glyph_title_keeps_the_frame_square(capsys):
    """A title whose characters take two cells must not widen the frame either."""
    _assert_square(_rows(capsys, "☤ 卸载 Hermes Agent"))


def test_overlong_title_is_clipped_instead_of_widening_the_frame(capsys):
    rows = _rows(capsys, "☤ " + "Hermes " * 20)
    _assert_square(rows)
