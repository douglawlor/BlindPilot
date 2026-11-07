"""SendToTabDialog lives in send_to_tab_dialog, not the GUI monolith.

blindpilot_app re-exports SendToTabDialog and RELAY_KINDS, so
existing call sites and tests keep working. The contract is
unchanged by the move: the first tab is selected, the message is
editable, choosing a kind fills both roles and enables the relay,
Escape cancels, and focus starts in the target list.
"""

import wx

import blindpilot_app as app
import send_to_tab_dialog


def test_send_to_tab_dialog_owns_the_class():
    assert issubclass(send_to_tab_dialog.SendToTabDialog, wx.Dialog)
    assert send_to_tab_dialog.PAD_DIALOG == 12
    assert send_to_tab_dialog.RELAY_KINDS[0][0] == "Plain hand-off"


def test_blindpilot_app_reexports_the_same_objects():
    assert app.SendToTabDialog is send_to_tab_dialog.SendToTabDialog
    assert app.RELAY_KINDS is send_to_tab_dialog.RELAY_KINDS


def test_dialog_opens_with_first_tab_and_message(wx_app):
    frame = wx.Frame(None)
    dialog = send_to_tab_dialog.SendToTabDialog(frame, ["Codex", "Claude"], "hello")
    try:
        assert dialog.target.GetSelection() == 0
        assert dialog.message.GetValue() == "hello"
        assert not dialog.relay.IsChecked()
        assert dialog.GetEscapeId() != wx.ID_OK
    finally:
        dialog.Destroy()
        frame.Destroy()
