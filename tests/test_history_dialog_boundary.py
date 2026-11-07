"""HistoryDialog lives in history_dialog, not the GUI monolith.

blindpilot_app re-exports HistoryDialog and its helpers, so existing
call sites and tests keep working. Names the dialog calls are
patched on history_dialog, where the calls are made from.

The contract is unchanged by the move: the scope and sort orders
keep their labels, a conversation's label parts join in the same
order, and the last exchange preview is cut at a word boundary.
"""

import wx

import blindpilot_app as app
import history_dialog
from session_history import HistoryEntry, HistoryTurn

NAMES = (
    "HistoryDialog",
    "_history_key",
    "sort_history",
    "conversation_groups",
    "group_names",
    "_last_exchange",
    "_save_or_announce",
    "_HISTORY_SORTS",
    "_HISTORY_SCOPES",
)


def test_history_dialog_owns_the_class_and_helpers():
    assert issubclass(history_dialog.HistoryDialog, wx.Dialog)
    assert history_dialog.PAD_DIALOG == 12
    assert history_dialog._HISTORY_SORTS[0] == "Newest first"


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(history_dialog, name), name


def test_helpers_behave_as_before():
    entry = HistoryEntry(
        backend="claude", session_id="s1", title="T", path="", modified=1.0, cwd="/x"
    )
    assert history_dialog._history_key(entry) == "claude:s1"
    turns = [HistoryTurn(prompt="hi", response="hello there")]
    assert history_dialog._last_exchange(turns) == "You: hi\n\nAnswer: hello there"
    groups = history_dialog.conversation_groups(
        {"conversation_groups": {"claude:s1": ["Work", ""]}}
    )
    assert history_dialog.group_names(groups) == ["Work"]
