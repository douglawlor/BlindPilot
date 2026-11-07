"""BugReportDialog lives in bug_report_dialog, not the GUI monolith.

blindpilot_app re-exports BugReportDialog, bug_report_body and
_copy_to_clipboard, so existing call sites and tests keep working.
The contract is unchanged by the move: the report body keeps its
section order, and focus starts in the Summary field.
"""

import wx

import blindpilot_app as app
import bug_report_dialog


def test_bug_report_dialog_owns_the_class_and_helpers():
    assert issubclass(bug_report_dialog.BugReportDialog, wx.Dialog)
    assert bug_report_dialog.PAD_DIALOG == 12
    assert bug_report_dialog.PAD == 8


def test_blindpilot_app_reexports_the_same_objects():
    assert app.BugReportDialog is bug_report_dialog.BugReportDialog
    assert app.bug_report_body is bug_report_dialog.bug_report_body
    assert app._copy_to_clipboard is bug_report_dialog._copy_to_clipboard


def test_report_body_keeps_its_section_order():
    body = bug_report_dialog.bug_report_body(
        "Crash", "It crashed", "", "1. Open it", "BlindPilot: 1"
    )
    assert (
        body.index("## What happened") < body.index("## What I expected") < body.index("## Steps")
    )
    assert "Not given." in body
    assert "BlindPilot: 1" in body


def test_dialog_opens_with_facts_shown(wx_app):
    frame = wx.Frame(None)
    dialog = bug_report_dialog.BugReportDialog(frame, "BlindPilot: 1")
    try:
        assert dialog.summary.GetValue() == ""
        assert dialog._facts == "BlindPilot: 1"
    finally:
        dialog.Destroy()
        frame.Destroy()
