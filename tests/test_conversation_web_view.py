# SPDX-License-Identifier: MIT
"""The conversation web view, and the list it is opened from.

Three things are checked here, each of which was broken at once:

* the dependency the view is built on was never declared, so a source
  checkout that followed the documented `pip install -r requirements.txt` had
  no package to import and the view never opened;
* the Recent Conversations list was scoped to one folder by default, and
  which folder that was depended on the directory the application happened to
  be started from, so conversations the user knew they had were not listed;
* Enter in the filter box did nothing, because the key handling had been
  narrowed to the list box alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import blindpilot_app as app
from session_history import HistoryEntry, HistoryTurn


def _reading_panel(parent, entry, title, **kwargs):
    return app.ConversationWebViewPanel(
        parent, entry, title, on_close=lambda page: page.Destroy(), **kwargs
    )


def _entry(title: str = "A conversation") -> HistoryEntry:
    return HistoryEntry(
        backend="claude",
        session_id="session-1",
        title=title,
        path="",
        modified=0.0,
    )


def _turns() -> list[HistoryTurn]:
    return [
        HistoryTurn(prompt="What is 2+2?", response="Four."),
        HistoryTurn(prompt="And 3+3?", response="Six."),
    ]


# ----- the dependency -----


def test_the_web_view_package_is_declared():
    """The package the view is built on has to be installed to be used.

    It was left out of requirements.txt while the view was written, so the
    instructions for running from source installed everything else and not
    this, and the import failed at the moment the view was opened.
    """
    requirements = (Path(app.__file__).parent / "requirements.txt").read_text(encoding="utf-8")
    assert "wx-accessible-webview" in requirements, (
        "requirements.txt does not install wx-accessible-webview, so the "
        "conversation web view cannot be built from a source checkout"
    )


def test_the_package_is_importable():
    """And the installed one is the one the code asks for."""
    importlib = pytest.importorskip("wx_accessible_webview")
    assert hasattr(importlib, "AccessibleWebView")


def test_the_view_is_built_on_the_declared_package():
    """A dependency nothing imports is a promise the code does not keep.

    The package was declared because the conversation view is built on it. A
    view that quietly went back to the raw wx control would leave the
    declaration describing a program that is not this one.
    """
    pytest.importorskip("wx_accessible_webview")
    import conversation_webview

    source = Path(conversation_webview.__file__).read_text(encoding="utf-8")
    assert "from wx_accessible_webview import AccessibleWebView" in source


# ----- the document it renders -----


def test_the_conversation_has_a_heading_for_its_title(frame):
    """One heading of level one names the conversation.

    The reader navigates by heading, and a document that starts at level two
    gives them no way to tell where the conversation begins.
    """
    dialog = _reading_panel(frame, _entry("Fix the login redirect"), "Fix the login redirect")
    try:
        fragment = dialog._conversation_html(_turns())
    finally:
        dialog.Destroy()

    assert "<h1>Fix the login redirect</h1>" in fragment
    assert "<h2>You, exchange 1</h2>" in fragment
    assert f"<h2>{app.backend_label('claude')}, exchange 2</h2>" in fragment
    # No heading level is skipped on the way down.
    assert fragment.index("<h1>") < fragment.index("<h2>")


def test_the_title_in_the_document_is_escaped(frame):
    """A title is text, never markup."""
    dialog = _reading_panel(frame, _entry("<script>x</script>"), "<script>x</script>")
    try:
        fragment = dialog._conversation_html(_turns())
    finally:
        dialog.Destroy()

    assert "<script>" not in fragment
    assert "&lt;script&gt;" in fragment


def test_nothing_a_conversation_names_is_fetched(frame):
    """An image in a conversation is not loaded by the web view showing it.

    The picture becomes a link instead, and a link is only fetched when the
    user asks for it: the web view opens it in the browser.
    """
    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        fragment = dialog._conversation_html(
            [HistoryTurn(prompt="![a picture](https://example.com/p.png)", response="")]
        )
    finally:
        dialog.Destroy()

    assert "<img" not in fragment
    assert "src=" not in fragment, "the web view would fetch this when the page opens"
    assert 'href="https://example.com/p.png"' in fragment


def test_the_plain_text_form_keeps_the_turns_apart(frame):
    """Where there is no web view, the conversation still reads as a conversation."""
    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        text = dialog._conversation_text(_turns())
    finally:
        dialog.Destroy()

    assert "You, exchange 1\nWhat is 2+2?" in text
    assert f"\n\n{app.backend_label('claude')}, exchange 1\nFour." in text


# ----- the dialog itself -----


def test_the_reader_is_a_child_panel_instead_of_a_top_level_dialog(frame):
    """The notebook owns the reading area and controls its size."""
    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        assert isinstance(dialog, app.wx.Panel)
        assert not dialog.IsTopLevel()
        assert dialog.GetParent() is frame
    finally:
        dialog.Destroy()


def test_the_dialog_names_the_area_it_shows(frame):
    """The caption says which conversation; the control says what it is."""
    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        assert dialog._webview.control.GetName() == "Conversation"
    finally:
        dialog.Destroy()


def test_escape_from_inside_the_conversation_closes_the_dialog(frame):
    """The native web control keeps Escape, so the page has to bridge it out.

    Without the bridge the only way out of the dialog is the title bar, which
    is the one exit a screen-reader user is least likely to reach for.
    """
    closed = []

    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        dialog._webview._on_close = lambda: closed.append(True)
        dialog._webview._on_message({"type": "__close"})
        assert closed == [True]
    finally:
        dialog.Destroy()


def test_focus_moves_into_the_conversation(frame):
    """The reading cursor has to go where the conversation is."""
    dialog = _reading_panel(frame, _entry(), "A conversation")
    focused: list[bool] = []
    from types import SimpleNamespace

    dialog._webview = SimpleNamespace(
        using_webview=True,
        set_content=lambda _html: None,
        focus=lambda: focused.append(True),
    )
    try:
        dialog._show_conversation(_turns(), "")
    finally:
        dialog.Destroy()

    assert focused, "the conversation was shown without moving focus into it"


def test_a_conversation_that_cannot_be_read_says_so(frame, monkeypatch):
    """A store that cannot be read is an error said out loud, not a blank page."""
    said: list[str] = []
    monkeypatch.setattr(app, "announce", lambda text, urgent=False: said.append(text))
    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        dialog._show_conversation([], "This conversation could not be read.")
    finally:
        dialog.Destroy()

    assert said == ["Error: This conversation could not be read."]


def test_a_conversation_closed_before_it_loads_says_nothing(frame, monkeypatch):
    """A window nobody can reach must not speak.

    The load is queued when the window opens and delivered on the next pass
    of the event loop. A window closed in between must not announce itself
    after the fact, which is how a "Loading conversation" lands in the middle
    of whatever the person is doing next.
    """
    said: list[str] = []
    monkeypatch.setattr(app, "announce", lambda text, urgent=False: said.append(text))
    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        dialog.Destroy()
        dialog._load_conversation()
        assert said == []
    finally:
        if dialog:
            dialog.Destroy()


def test_a_conversation_with_no_messages_says_which_one_is_missing(frame, monkeypatch):
    """A Hermes conversation on another machine cannot be shown, and says so."""
    said: list[str] = []
    monkeypatch.setattr(app, "announce", lambda text, urgent=False: said.append(text))
    missing = "This conversation is not stored on this computer, so it cannot be shown here."
    dialog = _reading_panel(frame, _entry(), "A conversation", missing=missing)
    try:
        dialog._show_conversation([], "")
    finally:
        dialog.Destroy()

    assert said == [missing]


def test_without_a_browser_the_conversation_is_still_readable(frame, monkeypatch):
    """A missing modern browser leaves a readable native text control."""
    import wx.html2 as html2

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("No browser backend")

    monkeypatch.setattr(html2.WebView, "New", unavailable)
    said: list[str] = []
    monkeypatch.setattr(app, "announce", lambda text, urgent=False: said.append(text))
    dialog = _reading_panel(frame, _entry(), "A conversation")
    try:
        assert not dialog._webview.using_webview
        dialog._show_conversation(_turns(), "")
        assert "What is 2+2?" in dialog._webview.fallback.GetValue()
    finally:
        dialog.Destroy()


# ----- the list the view is opened from -----


def test_the_list_shows_every_folder_by_default(frame, monkeypatch):
    """A conversation is filed under the directory it ran in.

    Which directory that is depends on where the application was started
    from: the source tree when it is run from source, the install folder
    otherwise. Scoping the list to one of them hid conversations the user
    knew they had.
    """
    monkeypatch.setattr(app, "_load_config", dict)
    monkeypatch.setattr(app, "list_history", lambda *_a, **_k: [])
    dialog = app.HistoryDialog(frame, backend="claude", cwd=r"C:\somewhere")
    try:
        assert dialog.scope_picker.GetStringSelection() == app._HISTORY_SCOPE_LABELS[1]
        assert dialog._selected_cwd() is None
    finally:
        dialog.Destroy()


def test_the_folder_the_user_asked_for_is_remembered(frame, monkeypatch):
    """A scope the user chose is kept, the way the sort is kept."""
    saved: dict = {}

    def save(cfg: dict) -> bool:
        saved.update(cfg)
        return True

    monkeypatch.setattr(app, "_load_config", lambda: {"history_scope": "folder"})
    monkeypatch.setattr(app, "_save_or_announce", save)
    monkeypatch.setattr(app, "list_history", lambda *_a, **_k: [])
    dialog = app.HistoryDialog(frame, backend="claude", cwd=r"C:\somewhere")
    try:
        assert dialog.scope_picker.GetStringSelection() == app._HISTORY_SCOPE_LABELS[0]
        assert dialog._selected_cwd() == r"C:\somewhere"
        dialog.scope_picker.SetSelection(1)
        dialog._on_scope()
    finally:
        dialog.Destroy()

    assert saved.get("history_scope") == "all"


def test_the_list_says_what_enter_does(frame, monkeypatch):
    """Enter changed what it does in this list, so the list says so."""
    monkeypatch.setattr(app, "_load_config", dict)
    monkeypatch.setattr(app, "list_history", lambda *_a, **_k: [])
    dialog = app.HistoryDialog(frame, backend="claude", cwd=r"C:\somewhere")
    try:
        dialog._entries = [_entry()]
        dialog._shown = list(dialog._entries)
        dialog._loading = False
        dialog._load_error = ""
        dialog._refresh()
        summary = dialog.summary.GetLabel()
    finally:
        dialog.Destroy()

    assert "Enter opens it in a new tab" in summary
    assert "Ctrl+Enter reads it in a web view" in summary
