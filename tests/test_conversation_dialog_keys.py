"""Enter, Escape and F5 in the two dialogs that list past conversations.

Both bind `EVT_CHAR_HOOK`, which fires before the focused control sees the
key. Enter on a list item opens the conversation in a new tab, the way the
Open button does. Ctrl+Enter (or Shift+Enter) reads it in an accessible web
view instead. Enter on a button is handed back so Cancel still cancels.

Enter works from the filter box too, where the conversation is the first
match: narrowing the list and opening what is left is one gesture, and a
key that did nothing there was a key that used to do something.

`HistoryDialog._on_key` was fixed first and `HermesSessionsDialog` repeated the
defect, so both dialogs are driven through the same tests here rather than
through two files that had to be kept in step by hand.
"""

from __future__ import annotations

import pytest

import blindpilot_app as app
from doubles import Button, KeyEvent


def _stub(monkeypatch):
    """The state `_on_key` reads, on a dialog neither of them has to build.

    The handler asks whether the focused window is a button; the stand-in has
    to answer that question the same way.
    """
    monkeypatch.setattr(app.wx, "Button", Button)

    stub = type("DialogStub", (), {})()
    stub._shown = ["a conversation"]
    stub.accepted = 0
    stub.webviewed = 0
    stub.ended: list[int] = []
    stub.focus = None
    stub._accept = lambda: setattr(stub, "accepted", stub.accepted + 1)
    stub._open_in_webview = lambda: setattr(stub, "webviewed", stub.webviewed + 1)
    stub.EndModal = stub.ended.append
    stub.FindFocus = lambda: stub.focus
    stub.filter_box = type("Box", (), {"HasFocus": lambda self: False})()
    stub.list_box = type(
        "List",
        (),
        {
            "SetFocus": lambda self: None,
            "SetSelection": lambda self, i: None,
            "HasFocus": lambda self: True,  # list has focus in these tests
        },
    )()
    return stub


def _history_stub(monkeypatch):
    stub = _stub(monkeypatch)
    stub.dialog = app.HistoryDialog
    return stub


def _hermes_stub(monkeypatch):
    """The Hermes dialog also reloads its list, and says so out loud."""
    stub = _stub(monkeypatch)
    stub.dialog = app.HermesSessionsDialog
    stub.said: list[str] = []
    monkeypatch.setattr(app, "announce", lambda text, urgent=False: stub.said.append(text))
    stub.reload_result = True
    stub._reload = lambda: stub.reload_result
    return stub


@pytest.fixture(params=[_history_stub, _hermes_stub], ids=["history", "hermes"])
def dialog(request, monkeypatch):
    return request.param(monkeypatch)


@pytest.fixture
def hermes(monkeypatch):
    return _hermes_stub(monkeypatch)


def _press(dialog, key, control_down=False, shift_down=False):
    event = KeyEvent(key, control_down=control_down, shift_down=shift_down)
    dialog.dialog._on_key(dialog, event)
    return event


def test_enter_on_a_button_does_not_open_a_conversation(dialog):
    """Enter on a button hands the key back so the button gets it."""
    dialog.focus = Button()

    event = _press(dialog, app.wx.WXK_RETURN)

    assert dialog.accepted == 0, "Enter on a button opened a conversation"
    assert dialog.webviewed == 0, "Enter on a button opened web view"
    assert event.skipped, "the button never got the key it was focused for"


def test_enter_in_the_list_opens_the_conversation(dialog):
    """Enter on a list item does what the Open button does.

    Opening the conversation in a new tab is what the list is for; the web
    view is a way of reading it, and it is on another key.
    """
    dialog.focus = object()

    _press(dialog, app.wx.WXK_RETURN)

    assert dialog.accepted == 1, "Enter in the list should open the conversation"
    assert dialog.webviewed == 0, "Enter in the list should not open web view"


def test_ctrl_enter_in_the_list_opens_the_web_view(dialog):
    """Ctrl+Enter on a list item reads the conversation in the web view."""
    dialog.focus = object()

    _press(dialog, app.wx.WXK_RETURN, control_down=True)

    assert dialog.webviewed == 1, "Ctrl+Enter in the list should open web view"
    assert dialog.accepted == 0, "Ctrl+Enter in the list should not open the conversation"


def test_shift_enter_in_the_list_opens_the_web_view(dialog):
    """Shift+Enter reads it as a page, the way it opens an answer as a page."""
    dialog.focus = object()

    _press(dialog, app.wx.WXK_RETURN, shift_down=True)

    assert dialog.webviewed == 1, "Shift+Enter in the list should open web view"
    assert dialog.accepted == 0, "Shift+Enter in the list should not open the conversation"


def test_enter_in_the_filter_box_opens_the_conversation(dialog):
    """Type a filter, press Enter, get the first match opened."""
    dialog.focus = dialog.filter_box

    _press(dialog, app.wx.WXK_NUMPAD_ENTER)

    assert dialog.accepted == 1, "Enter in the filter box should open the conversation"
    assert dialog.webviewed == 0, "Enter in the filter box should not open web view"


def test_ctrl_enter_in_the_filter_box_opens_the_web_view(dialog):
    """The filter box is where a conversation is picked from as well."""
    dialog.focus = dialog.filter_box

    _press(dialog, app.wx.WXK_RETURN, control_down=True)

    assert dialog.webviewed == 1, "Ctrl+Enter in the filter box should open web view"
    assert dialog.accepted == 0, "Ctrl+Enter in the filter box should not open the conversation"


def test_escape_still_cancels_from_anywhere(dialog):
    dialog.focus = Button()

    _press(dialog, app.wx.WXK_ESCAPE)

    assert dialog.ended == [app.wx.ID_CANCEL]


def test_f5_says_refreshed_only_when_the_reload_worked(hermes):
    """`_reload` speaks the error itself; "Refreshed" on top of it is a lie.

    Only the Hermes dialog reloads: the past-conversations list is read from
    disk when it opens and has nothing to go back to.
    """
    _press(hermes, app.wx.WXK_F5)
    assert hermes.said == ["Refreshed"]

    hermes.said.clear()
    hermes.reload_result = False
    _press(hermes, app.wx.WXK_F5)
    assert hermes.said == []
