"""NewSessionDialog lives in new_session_dialog, not the monolith.

blindpilot_app re-exports NewSessionDialog, _recent_folders and
create_worktree, and _git from process_utils, so existing call
sites and tests keep working. Names the dialog calls are patched
on new_session_dialog, where the calls are made from.

The contract is unchanged by the move: the folder box starts on
the last folder opened, Up and Down walk the recent folders, and
_git runs git in a folder with no window.
"""

import wx

import blindpilot_app as app
import new_session_dialog
import process_utils


def test_new_session_dialog_owns_the_class_and_helpers():
    assert issubclass(new_session_dialog.NewSessionDialog, wx.Dialog)
    assert new_session_dialog.PAD_DIALOG == 12
    assert callable(new_session_dialog._recent_folders)
    assert callable(new_session_dialog.create_worktree)


def test_blindpilot_app_reexports_the_same_objects():
    assert app.NewSessionDialog is new_session_dialog.NewSessionDialog
    assert app._recent_folders is new_session_dialog._recent_folders
    assert app.create_worktree is new_session_dialog.create_worktree
    assert app._git is process_utils._git


def test_recent_folders_come_from_config(monkeypatch):
    monkeypatch.setattr(
        new_session_dialog, "_load_config", lambda: {"recent_folders": ["a", "", 3, "b"]}
    )
    assert new_session_dialog._recent_folders() == ["a", "b"]
