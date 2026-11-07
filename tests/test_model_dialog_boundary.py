"""ModelDialog lives in model_dialog, not the GUI monolith.

blindpilot_app re-exports ModelDialog, ModelOptions, AUTO_MODEL,
AUTO_MODEL_CHOICE, DEFAULT_CHOICE, _keep_choice, _model_choice and
_plain, so existing call sites and tests keep working. The contract
is unchanged by the move: the first entry in each box is "leave it
alone" and says what that means, an alias shows the version it
resolves to, and selection() returns "" for anything left alone.
"""

import wx

import blindpilot_app as app
import model_dialog

NAMES = (
    "ModelDialog",
    "ModelOptions",
    "AUTO_MODEL",
    "AUTO_MODEL_CHOICE",
    "DEFAULT_CHOICE",
    "_keep_choice",
    "_model_choice",
    "_plain",
)


def test_model_dialog_owns_the_class_and_helpers():
    assert issubclass(model_dialog.ModelDialog, wx.Dialog)
    assert model_dialog.PAD_DIALOG == 12
    assert model_dialog.PAD == 8


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(model_dialog, name), name


def test_choices_read_as_before():
    assert model_dialog._plain("`opus`") == "opus"
    assert model_dialog._keep_choice("") == "(CLI default)"
    assert model_dialog._keep_choice("Opus 5") == "(CLI default), currently Opus 5"
    assert model_dialog._model_choice("opus", {"opus": "Opus 5.5"}) == "opus: Opus 5.5"
    assert model_dialog._model_choice("opus", {}) == "opus"


def test_dialog_selection_defaults_to_leave_it_alone(wx_app):
    frame = wx.Frame(None)
    options = model_dialog.ModelOptions(["opus"], ["high"], current_model="Opus 5")
    dialog = model_dialog.ModelDialog(frame, options, "", "", "Claude Code")
    try:
        assert dialog.selection() == ("", "")
    finally:
        dialog.Destroy()
        frame.Destroy()
