"""The Add dialogs live in add_dialogs, not the GUI monolith.

blindpilot_app re-exports AddMemoryDialog, AddAgentDialog,
AddNoteDialog and MEMORY_TYPES, so existing call sites keep working.
The contract is unchanged by the move: every field is named for the
screen reader from its label, a new memory defaults to the project
type, and focus starts in the first field.
"""

import wx

import add_dialogs
import blindpilot_app as app


def test_add_dialogs_owns_the_classes():
    for cls in (add_dialogs.AddMemoryDialog, add_dialogs.AddAgentDialog, add_dialogs.AddNoteDialog):
        assert issubclass(cls, wx.Dialog)
    assert add_dialogs.PAD_DIALOG == 12
    assert add_dialogs.MEMORY_TYPES == ("user", "feedback", "project", "reference")


def test_blindpilot_app_reexports_the_same_objects():
    assert app.AddMemoryDialog is add_dialogs.AddMemoryDialog
    assert app.AddAgentDialog is add_dialogs.AddAgentDialog
    assert app.AddNoteDialog is add_dialogs.AddNoteDialog
    assert app.MEMORY_TYPES is add_dialogs.MEMORY_TYPES


def test_memory_dialog_defaults_to_project_type(wx_app, tmp_path):
    frame = wx.Frame(None)
    dialog = add_dialogs.AddMemoryDialog(frame, [tmp_path])
    try:
        folder, name, description, kind, body = dialog.values()
        assert kind == "project"
        assert folder == tmp_path
        assert dialog.name.GetName() == "Name"
    finally:
        dialog.Destroy()
        frame.Destroy()
