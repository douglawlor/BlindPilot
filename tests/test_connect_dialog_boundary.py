"""ConnectDialog lives in connect_dialog, not the GUI monolith.

blindpilot_app re-exports ConnectDialog and _open_web_page, so
existing call sites and tests keep working. Names the dialog calls
are patched on connect_dialog, where the calls are made from.

The contract is unchanged by the move: only http and https
addresses are handed to the platform opener, and a worker result
that lands after the app has gone is dropped.
"""

import wx

import blindpilot_app as app
import connect_dialog


def test_connect_dialog_owns_the_class_and_helper():
    assert issubclass(connect_dialog.ConnectDialog, wx.Dialog)
    assert connect_dialog.PAD_DIALOG == 12
    assert connect_dialog.PAD == 8


def test_blindpilot_app_reexports_the_same_objects():
    assert app.ConnectDialog is connect_dialog.ConnectDialog
    assert app._open_web_page is connect_dialog._open_web_page


def test_only_web_addresses_are_opened(monkeypatch):
    opened = []
    monkeypatch.setattr(connect_dialog.webbrowser, "open", lambda url: opened.append(url) or True)
    assert connect_dialog._open_web_page("https://example.com/signin") is True
    assert connect_dialog._open_web_page("file:///etc/passwd") is False
    assert connect_dialog._open_web_page("ms-msdt:/id") is False
    assert opened == ["https://example.com/signin"]
