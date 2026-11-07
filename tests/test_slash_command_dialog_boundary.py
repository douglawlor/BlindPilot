"""SlashCommandDialog lives in slash_command_dialog, not the monolith.

blindpilot_app re-exports it under the same name, so existing call
sites and tests that reach it as ``app.SlashCommandDialog`` keep
working. The keyboard contract is unchanged by the move: Enter on a
button is the button's, Enter elsewhere accepts, Escape cancels,
and focus starts in the list.
"""

import wx

import blindpilot_app as app
import slash_command_dialog


def test_slash_command_dialog_owns_the_class():
    assert issubclass(slash_command_dialog.SlashCommandDialog, wx.Dialog)
    assert slash_command_dialog.PAD_DIALOG == 12


def test_blindpilot_app_reexports_the_same_object():
    assert app.SlashCommandDialog is slash_command_dialog.SlashCommandDialog


def test_dialog_opens_with_first_row_selected_and_focus_in_list(wx_app):
    frame = wx.Frame(None)
    dialog = slash_command_dialog.SlashCommandDialog(frame, "Pick one", ["/help", "/model"])
    try:
        assert dialog.GetSelection() == 0
        assert dialog.list_box.GetRows()[1].label == "/model"
    finally:
        dialog.Destroy()
        frame.Destroy()
