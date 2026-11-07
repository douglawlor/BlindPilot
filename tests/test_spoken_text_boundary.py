"""The spoken-text helpers live in spoken_text, not the GUI monolith.

blindpilot_app re-exports them under the same names, so existing call
sites and tests that reach them as ``app._sent_readback`` keep working.
"""

import blindpilot_app as app
import spoken_text

HELPERS = ("_spoken_duration", "_context_line", "_result_label", "_sent_readback", "_FENCED_BLOCKS")


def test_spoken_text_owns_the_helpers():
    for name in HELPERS:
        assert getattr(spoken_text, name) is not None, name


def test_blindpilot_app_reexports_the_same_objects():
    for name in HELPERS:
        assert getattr(app, name) is getattr(spoken_text, name), name


def test_spoken_text_formats_as_before():
    assert spoken_text._spoken_duration(125) == "2 minutes 5 seconds"
    assert spoken_text._spoken_duration(0) == "0 seconds"
    assert spoken_text._context_line(124_000, 200_000) == "Context 62% full: 124,000 of 200,000 tokens"
    assert spoken_text._context_line(0, 0) == "Context: not reported yet"
    assert spoken_text._result_label("first line\nsecond") == "Result: first line"
    assert spoken_text._sent_readback("fix the build") == "Sent: fix the build"
