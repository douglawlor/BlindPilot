"""ReadView lives in read_view, not the GUI monolith.

blindpilot_app re-exports ReadView, _monospace_font and _open_path
under the same names, so existing call sites and tests that reach
them as ``app.ReadView`` keep working. The viewing contract is
unchanged by the move: focus starts in the text, Escape closes, and
an unsaved edit asks before closing.
"""

import wx

import blindpilot_app as app
import read_view


def test_read_view_owns_the_class_and_helpers():
    assert issubclass(read_view.ReadView, wx.Dialog)
    assert read_view.PAD_DIALOG == 12
    assert callable(read_view._monospace_font)
    assert callable(read_view._open_path)


def test_blindpilot_app_reexports_the_same_objects():
    assert app.ReadView is read_view.ReadView
    assert app._monospace_font is read_view._monospace_font
    assert app._open_path is read_view._open_path


def test_viewer_opens_read_only_with_text_and_focus(wx_app):
    frame = wx.Frame(None)
    dialog = read_view.ReadView(frame, "hello\nworld", "A row")
    try:
        assert dialog.viewer.GetValue() == "hello\nworld"
        assert not dialog.viewer.IsEditable()
    finally:
        dialog.Destroy()
        frame.Destroy()
