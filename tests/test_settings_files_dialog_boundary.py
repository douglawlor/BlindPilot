"""SettingsFilesDialog lives in settings_files_dialog, not the monolith.

blindpilot_app re-exports SettingsFilesDialog, _settings_label and
_reach_settings_file, so existing call sites and tests keep working.
Names the dialog calls are patched on settings_files_dialog, where
the calls are made from. The contract is unchanged by the move:
every file is listed whether or not it exists, BlindPilot opens
files but never creates or writes them, and focus starts in the list.
"""

import wx

import blindpilot_app as app
import settings_files_dialog
from agent_backends import SettingsFile


def test_settings_files_dialog_owns_the_class_and_helpers():
    assert issubclass(settings_files_dialog.SettingsFilesDialog, wx.Dialog)
    assert settings_files_dialog.PAD_DIALOG == 12
    assert settings_files_dialog.PAD == 8


def test_blindpilot_app_reexports_the_same_objects():
    assert app.SettingsFilesDialog is settings_files_dialog.SettingsFilesDialog
    assert app._settings_label is settings_files_dialog._settings_label
    assert app._reach_settings_file is settings_files_dialog._reach_settings_file


def test_label_says_backend_scope_note_and_state(tmp_path):
    path = tmp_path / "settings.json"
    entry = SettingsFile("claude", "global", path, "Every project.")
    assert (
        settings_files_dialog._settings_label(entry)
        == "Claude Code, global. Every project. (not created yet)"
    )
    path.write_text("{}", encoding="utf-8")
    entry = SettingsFile("claude", "global", path, "Every project.")
    assert settings_files_dialog._settings_label(entry).endswith("(present)")
