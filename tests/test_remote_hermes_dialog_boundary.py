"""RemoteHermesDialog lives in remote_hermes_dialog, not the monolith.

blindpilot_app re-exports it under the same name, so existing call
sites and tests that reach it as ``app.RemoteHermesDialog`` keep
working. The contract is unchanged by the move: fields start from
the saved Remote Hermes settings, every field is labelled, and the
test result is reported in the dialog's status line.
"""

import wx

import blindpilot_app as app
import remote_hermes_dialog
from app_config import REMOTE_HERMES


def test_remote_hermes_dialog_owns_the_class():
    assert issubclass(remote_hermes_dialog.RemoteHermesDialog, wx.Dialog)
    assert remote_hermes_dialog.PAD_DIALOG == 12
    assert remote_hermes_dialog.PAD == 8


def test_blindpilot_app_reexports_the_same_object():
    assert app.RemoteHermesDialog is remote_hermes_dialog.RemoteHermesDialog


def test_dialog_fields_start_from_saved_settings(wx_app):
    frame = wx.Frame(None)
    dialog = remote_hermes_dialog.RemoteHermesDialog(frame)
    try:
        assert dialog._host.GetValue() == REMOTE_HERMES.host
        assert dialog._port.GetValue() == REMOTE_HERMES.port
        assert dialog._enabled.IsChecked() == REMOTE_HERMES.enabled
    finally:
        dialog.Destroy()
        frame.Destroy()
