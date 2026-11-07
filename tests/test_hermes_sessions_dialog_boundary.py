"""HermesSessionsDialog lives in hermes_sessions_dialog.

blindpilot_app re-exports HermesSessionsDialog and
hermes_session_label, so existing call sites and tests keep
working. The shared wx.CallAfter guard, _call_after_alive, lives in
dialog_tasks with TaskSlot and is re-exported by blindpilot_app.
Names the dialog calls are patched on hermes_sessions_dialog, where
the calls are made from.

The contract is unchanged by the move: a live conversation's label
says "Running now" first, focus starts in the filter, and the list
is filled in when the catalog arrives.
"""

import wx

import blindpilot_app as app
import dialog_tasks
import hermes_sessions_dialog


def test_hermes_sessions_dialog_owns_the_class_and_label():
    assert issubclass(hermes_sessions_dialog.HermesSessionsDialog, wx.Dialog)
    assert hermes_sessions_dialog.PAD_DIALOG == 12
    label = hermes_sessions_dialog.hermes_session_label(
        {"title": "Fix it", "message_count": 1, "source": "tui"}, live=True
    )
    assert label.startswith("Running now")
    assert "1 message" in label


def test_blindpilot_app_reexports_the_same_objects():
    assert app.HermesSessionsDialog is hermes_sessions_dialog.HermesSessionsDialog
    assert app.hermes_session_label is hermes_sessions_dialog.hermes_session_label
    assert app._call_after_alive is dialog_tasks._call_after_alive
