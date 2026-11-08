"""MuseAiDialog lives in museai_dialog, not the GUI monolith.

blindpilot_app re-exports it under the same name, so existing call
sites and tests keep working. Names the dialog calls are patched
on museai_dialog, where the calls are made from.

The contract is unchanged by the move: Read is the default button,
every view gets Refresh and Close, and action buttons exist only
for the views they belong to.
"""

import wx

import blindpilot_app as app
import museai_dialog


def test_museai_dialog_owns_the_class():
    assert issubclass(museai_dialog.MuseAiDialog, wx.Dialog)
    assert museai_dialog.PAD_DIALOG == 12


def test_blindpilot_app_reexports_the_same_object():
    assert app.MuseAiDialog is museai_dialog.MuseAiDialog


def test_feed_view_has_run_and_link_buttons(wx_app):
    frame = wx.Frame(None)
    dialog = museai_dialog.MuseAiDialog(frame, "feed")
    try:
        assert dialog.read is not None
        assert dialog.run is not None and dialog.run.GetLabel() == "&Run now…"
        assert dialog.links is not None
        assert dialog.decide is None
    finally:
        dialog.Destroy()
        frame.Destroy()


def test_approvals_view_has_decide_and_no_discuss(wx_app):
    frame = wx.Frame(None)
    dialog = museai_dialog.MuseAiDialog(frame, "approvals")
    try:
        assert dialog.decide is not None
        assert dialog.discuss is None
    finally:
        dialog.Destroy()
        frame.Destroy()
