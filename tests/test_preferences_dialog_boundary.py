"""PreferencesDialog lives in preferences_dialog, not the monolith.

blindpilot_app re-exports it under the same name, so existing call
sites and tests keep working. The contract is unchanged by the
move: the dialog only collects choices from the live SETTINGS, it
changes nothing itself (the frame applies them through the same
switches the menu items use), and focus starts on Narration.
"""

import wx

import blindpilot_app as app
import preferences_dialog
from app_config import SETTINGS


def test_preferences_dialog_owns_the_class_and_constants():
    assert issubclass(preferences_dialog.PreferencesDialog, wx.Dialog)
    assert preferences_dialog.PAD_DIALOG == 12
    assert preferences_dialog.MODEL_REFRESH_MINUTES_MIN == 0
    assert preferences_dialog.MODEL_REFRESH_MINUTES_MAX == 240


def test_blindpilot_app_reexports_the_same_objects():
    assert app.PreferencesDialog is preferences_dialog.PreferencesDialog
    assert app.MODEL_REFRESH_MINUTES_MIN is preferences_dialog.MODEL_REFRESH_MINUTES_MIN
    assert app.MODEL_REFRESH_MINUTES_MAX is preferences_dialog.MODEL_REFRESH_MINUTES_MAX


def test_dialog_reads_live_settings_without_changing_them(wx_app):
    before = SETTINGS.live_rows
    frame = wx.Frame(None)
    dialog = preferences_dialog.PreferencesDialog(frame)
    try:
        assert dialog._live_rows.IsChecked() == before
        assert SETTINGS.live_rows == before
    finally:
        dialog.Destroy()
        frame.Destroy()
