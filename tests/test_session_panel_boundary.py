"""SessionPanel lives in session_panel, not the GUI monolith.

blindpilot_app re-exports SessionPanel and the helpers that came
with it, so MainFrame and the tests keep working. Names the panel
calls are patched on session_panel, where the calls are made from.

The contract is unchanged by the move: a trailing question the
agent asked properly is not repeated, and a tab's label is its
backend until it has a title of its own.
"""

import wx

import blindpilot_app as app
import session_panel

NAMES = (
    "SessionPanel",
    "_flatten",
    "_tab_label",
    "_gui_work",
    "_show_conversation_items",
    "question_left_in_the_answer",
    "changed_files",
    "cached_changed_files",
    "_slash_commands_for_backend",
)


def test_session_panel_owns_the_class_and_helpers():
    assert issubclass(session_panel.SessionPanel, wx.Panel)
    for name in NAMES[1:]:
        assert callable(getattr(session_panel, name)), name


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(session_panel, name), name


def test_helpers_behave_as_before():
    assert session_panel._flatten("Fix the `build`!") == "fixthebuild"
    left = session_panel.question_left_in_the_answer(
        "Done. Which branch should I use?", asked_properly=False, replaying=False
    )
    assert left != ""
    assert (
        session_panel.question_left_in_the_answer("Done.", asked_properly=False, replaying=False)
        == ""
    )
    assert (
        session_panel.question_left_in_the_answer(
            "Done. Which branch should I use?", asked_properly=True, replaying=False
        )
        == ""
    )
