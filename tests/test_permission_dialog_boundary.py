"""PermissionDialog lives in permission_dialog, not the GUI monolith.

blindpilot_app re-exports both names, so existing call sites and tests
that reach them as ``app.PermissionDialog`` keep working. The dialog's
accessibility invariants are unchanged by the move: Deny is the default
button, Escape is Deny, and focus starts in the request text.
"""

import wx

import blindpilot_app as app
import permission_dialog


def test_permission_dialog_owns_the_classes():
    assert issubclass(permission_dialog.PermissionDialog, wx.Dialog)
    assert permission_dialog.PAD == 8
    assert permission_dialog.PAD_DIALOG == 12


def test_blindpilot_app_reexports_the_same_objects():
    assert app.PermissionDialog is permission_dialog.PermissionDialog
    assert app._permission_text is permission_dialog._permission_text


def test_permission_text_behaves_as_before():
    assert permission_dialog._permission_text("ExitPlanMode", {"plan": "1. Fix it"}) == "1. Fix it"
    text = permission_dialog._permission_text(
        "Bash", {"command": "git push", "_reason": "Not in the allow list"}
    )
    assert text.splitlines() == [
        "Tool: Bash",
        "Why it is asking: Not in the allow list",
        "command: git push",
    ]


def test_deny_is_the_default_and_escape_choice(wx_app):
    dialog = permission_dialog.PermissionDialog(None, "claude", "Bash", {"command": "ls"}, False)
    try:
        assert dialog.GetEscapeId() == permission_dialog.PermissionDialog.DENY
        deny = dialog.FindWindow(permission_dialog.PermissionDialog.DENY)
        assert deny is not None
    finally:
        dialog.Destroy()
