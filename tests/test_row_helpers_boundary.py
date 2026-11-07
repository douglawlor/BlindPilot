"""The row-offset helpers live in markdown_rows, not the GUI monolith.

blindpilot_app re-exports them under the same names, so existing call
sites and tests that reach them as ``app._row_at`` keep working, while
the logic itself sits with the Row type it operates on.
"""

import blindpilot_app as app
import markdown_rows


def test_markdown_rows_owns_the_row_helpers():
    for name in ("_one_line", "_row_at", "_starts_of", "_last_block_start", "_search_key"):
        assert callable(getattr(markdown_rows, name)), name


def test_blindpilot_app_reexports_the_same_objects():
    for name in ("_one_line", "_row_at", "_starts_of", "_last_block_start", "_search_key"):
        assert getattr(app, name) is getattr(markdown_rows, name), name


def test_row_offsets_round_trip():
    lines = ["alpha", "beta", "gamma"]
    starts = markdown_rows._starts_of(lines)
    assert starts == [0, 6, 11]
    assert markdown_rows._row_at(starts, 0) == 0
    assert markdown_rows._row_at(starts, 6) == 1
    assert markdown_rows._row_at([], 0) == -1
    assert markdown_rows._one_line("a\n  b\tc") == "a b c"
