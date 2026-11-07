"""AgentFileList lives in agent_file_list, not the GUI monolith.

blindpilot_app re-exports it under the same name, so existing call
sites keep working. The contract is unchanged by the move: Read is
the default button, Enter reads, Delete deletes only where entries
can be removed, Escape goes back, and focus starts in the list.
"""

import wx

import agent_file_list
import blindpilot_app as app


def test_agent_file_list_owns_the_class():
    assert issubclass(agent_file_list.AgentFileList, wx.Dialog)
    assert agent_file_list.PAD_DIALOG == 12


def test_blindpilot_app_reexports_the_same_object():
    assert app.AgentFileList is agent_file_list.AgentFileList


def test_list_opens_with_selection_and_default_read(wx_app):
    frame = wx.Frame(None)
    dialog = agent_file_list.AgentFileList(frame, "Memories", ["one", "two"], 1)
    try:
        assert dialog.list.GetSelection() == 1
        read = dialog.FindWindow(wx.ID_OPEN)
        assert read is not None
        # No Add button was asked for, so no Delete either.
        assert dialog.FindWindow(wx.ID_DELETE) is None
    finally:
        dialog.Destroy()
        frame.Destroy()


def test_memories_offer_add_and_delete(wx_app):
    frame = wx.Frame(None)
    dialog = agent_file_list.AgentFileList(frame, "Memories", ["one"], 0, add_label="&Add")
    try:
        assert dialog.FindWindow(wx.ID_ADD) is not None
        assert dialog.FindWindow(wx.ID_DELETE) is not None
    finally:
        dialog.Destroy()
        frame.Destroy()
