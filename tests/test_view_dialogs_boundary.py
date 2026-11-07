"""FormattedView and WrappedText live in view_dialogs, not the monolith.

blindpilot_app re-exports them under the same names, so existing call
sites and tests that reach them as ``app.FormattedView`` keep working.
"""

import wx

import blindpilot_app as app
import view_dialogs


def test_view_dialogs_owns_the_classes():
    assert issubclass(view_dialogs.FormattedView, wx.Dialog)
    assert issubclass(view_dialogs.WrappedText, wx.StaticText)


def test_blindpilot_app_reexports_the_same_objects():
    assert app.FormattedView is view_dialogs.FormattedView
    assert app.WrappedText is view_dialogs.WrappedText
