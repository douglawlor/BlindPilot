"""SubagentDialog and SubagentEntry live in subagent_dialog.

blindpilot_app re-exports both names, so existing call sites and
tests that reach them as ``app.SubagentEntry`` keep working. The
contract is unchanged by the move: the log follows only when the
caret is at the end, Escape is Close, and focus starts in the log.
"""

import wx

import blindpilot_app as app
import subagent_dialog


def test_subagent_dialog_owns_the_classes():
    assert issubclass(subagent_dialog.SubagentDialog, wx.Dialog)
    assert subagent_dialog.PAD == 8


def test_blindpilot_app_reexports_the_same_objects():
    assert app.SubagentDialog is subagent_dialog.SubagentDialog
    assert app.SubagentEntry is subagent_dialog.SubagentEntry


def test_entry_label_reads_name_status_and_last_line():
    entry = subagent_dialog.SubagentEntry(
        name="scout", status="running", lines=["looking", "found it"]
    )
    assert entry.label() == "scout, running: found it"
    assert subagent_dialog.SubagentEntry(name="scout", status="done").label() == "scout, done"


def test_dialog_opens_on_the_log_with_escape_as_close(wx_app):
    frame = wx.Frame(None)
    entry = subagent_dialog.SubagentEntry(name="scout", status="running", lines=["one"])
    dialog = subagent_dialog.SubagentDialog(frame, "a1", entry)
    try:
        assert dialog.GetEscapeId() == wx.ID_CLOSE
        assert dialog.log.GetValue() == "one"
        assert dialog.GetTitle() == "Subagent: scout, running"
    finally:
        dialog.Destroy()
        frame.Destroy()
