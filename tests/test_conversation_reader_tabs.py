"""Reading a saved conversation uses the main tab strip and its lifecycle."""

from types import SimpleNamespace

import pytest
import wx

import blindpilot_app as app
import main_frame
import conversation_webview
from session_history import HistoryEntry, HistoryTurn


@pytest.fixture
def entry():
    return HistoryEntry("claude", "reader-example", "Fix the parser", "", 1)


@pytest.fixture
def window(wx_app, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_load_config", lambda: {"app_mode": "agent", "backend": "claude"})
    monkeypatch.setattr(
        main_frame, "_load_config", lambda: {"app_mode": "agent", "backend": "claude"}
    )
    monkeypatch.setattr(app, "_save_config", lambda _cfg: True)
    monkeypatch.setattr(main_frame, "_save_config", lambda _cfg: True)
    monkeypatch.setattr(app, "announce", lambda *_a, **_k: None)
    monkeypatch.setattr(main_frame, "announce", lambda *_a, **_k: None)
    monkeypatch.setattr(conversation_webview, "_modern_browser_available", lambda: False)
    monkeypatch.setattr(app.ConversationWebViewPanel, "_load_conversation", lambda _page: None)
    frame = app.MainFrame(str(tmp_path))
    wx.Yield()
    try:
        yield frame
    finally:
        frame.Destroy()
        wx.Yield()


def test_reader_uses_the_existing_tab_strip(window, entry):
    opener = window.notebook.GetCurrentPage()
    page = window._add_conversation_webview(entry)
    assert window.notebook.GetCurrentPage() is page
    assert page.return_to is opener
    assert window.notebook.GetPageText(1) == "Read: Fix the parser"
    assert window.tab_switcher.GetPageText(1) == "Read: Fix the parser"
    assert window.tab_switcher.GetSelection() == 1
    assert window._session_panels() == [opener]
    window._cycle_tab(-1)
    assert window.notebook.GetCurrentPage() is opener
    window._jump_to_tab(1)
    assert window.notebook.GetCurrentPage() is page


def test_closing_a_reader_returns_to_its_opener(window, entry):
    opener = window.notebook.GetCurrentPage()
    page = window._add_conversation_webview(entry)
    other = window._add_session(opener.cwd)
    window._jump_to_tab(1)
    window._close_current_session()
    assert page._closed
    assert window.notebook.GetCurrentPage() is opener
    assert window.notebook.GetPageCount() == window.tab_switcher.GetPageCount() == 2
    assert window.notebook.GetPage(1) is other


def test_escape_closes_its_reader_by_identity(window, entry):
    page = window._add_conversation_webview(entry)
    other = window._add_session(window._history_cwd())
    page._webview._on_message({"type": "__close"})
    assert page._closed
    assert window.notebook.GetCurrentPage() is other
    assert window.notebook.GetPageCount() == 2


def test_reader_close_focuses_a_remaining_session_when_the_opener_is_gone(
    window, entry, monkeypatch
):
    page = window._add_conversation_webview(entry)
    other = window._add_session(window._history_cwd())
    window._jump_to_tab(0)
    window._close_current_session()
    window._jump_to_tab(0)
    focused = []
    monkeypatch.setattr(window, "focus_for_mode", lambda: focused.append(True))
    window._close_conversation_webview(page)
    wx.Yield()
    assert window.notebook.GetCurrentPage() is other
    assert focused


def test_closing_the_only_remaining_reader_creates_a_session(window, entry):
    page = window._add_conversation_webview(entry)
    window._jump_to_tab(0)
    window._close_current_session()
    assert window.notebook.GetCurrentPage() is page
    window._close_current_session()
    assert window.notebook.GetPageCount() == window.tab_switcher.GetPageCount() == 1
    assert isinstance(window.notebook.GetCurrentPage(), app.SessionPanel)


def test_deleted_reader_drops_late_load_results(window, entry, monkeypatch):
    page = window._add_conversation_webview(entry)
    window.notebook.DeletePage(1)
    assert page._closed
    spoken = []
    monkeypatch.setattr(app, "announce", spoken.append)
    monkeypatch.setattr(main_frame, "announce", spoken.append)
    page._show_conversation([HistoryTurn("Late prompt", "Late answer")], "")
    assert spoken == []


def test_background_reader_load_does_not_speak_or_focus(window, entry, monkeypatch):
    page = window._add_conversation_webview(entry)
    wx.Yield()
    window._jump_to_tab(0)
    wx.Yield()
    spoken, focused, content = [], [], []
    monkeypatch.setattr(app, "announce", spoken.append)
    monkeypatch.setattr(main_frame, "announce", spoken.append)
    page._webview = SimpleNamespace(
        using_webview=True, set_content=content.append, focus=lambda: focused.append(True)
    )
    page._show_conversation([HistoryTurn("Background prompt", "Background answer")], "")
    assert "Background answer" in content[0]
    assert spoken == focused == []
    assert page.last_status == "Conversation loaded: 2 messages."


def test_selecting_a_failed_reader_preserves_and_announces_its_error(window, entry, monkeypatch):
    page = window._add_conversation_webview(entry)
    wx.Yield()
    window._jump_to_tab(0)
    wx.Yield()
    page._show_conversation([], "This conversation could not be read.")
    spoken = []
    monkeypatch.setattr(app, "announce", spoken.append)
    monkeypatch.setattr(main_frame, "announce", spoken.append)
    monkeypatch.setattr(conversation_webview, "announce", spoken.append)
    monkeypatch.setattr(type(window), "_focus_is_within", staticmethod(lambda *_a: False))
    window._jump_to_tab(1)
    wx.Yield()
    assert page.last_status == "Error: This conversation could not be read."
    assert any(page.last_status in text for text in spoken)
    assert page.source_status.IsShown()


def test_frame_shutdown_invalidates_readers_before_native_destruction(window, entry, monkeypatch):
    page = window._add_conversation_webview(entry)
    frame = SimpleNamespace(notebook=window.notebook, chat_panel=None, _session_panels=list)
    monkeypatch.setattr(app, "_destroy_other_top_level_windows", lambda _frame: None)
    monkeypatch.setattr(main_frame, "_destroy_other_top_level_windows", lambda _frame: None)
    monkeypatch.setattr(app.backend_pool, "stop_all_held_processes", lambda: None)
    with monkeypatch.context() as patches:
        patches.setattr(wx, "GetApp", lambda: None)
        app.MainFrame._on_close(frame, SimpleNamespace(Skip=lambda: None))
    assert page._closed
    assert page._webview._closed


def test_arrowing_to_a_reader_keeps_focus_on_the_tab_strip(window, entry, monkeypatch):
    page = window._add_conversation_webview(entry)
    wx.Yield()
    window._jump_to_tab(0)
    wx.Yield()
    focused = []
    monkeypatch.setattr(page, "focus_content", lambda: focused.append("document"))
    monkeypatch.setattr(window.tab_switcher, "SetFocus", lambda: focused.append("strip"))
    monkeypatch.setattr(type(window), "_focus_is_within", staticmethod(lambda *_a: True))
    window._jump_to_tab(1)
    wx.Yield()
    assert focused == ["strip"]


def test_browser_commands_switch_and_close_reader_tabs(window, entry):
    page = window._add_conversation_webview(entry)
    window._webview_command(page, "previous-tab")
    assert window.notebook.GetSelection() == 0
    # A stale message from a hidden page cannot close the selected session.
    window._webview_command(page, "close")
    assert window.notebook.GetPageCount() == 2
    window._jump_to_tab(1)
    window._webview_command(page, "close")
    assert window.notebook.GetPageCount() == 1


def test_f6_moves_between_the_reader_and_tab_strip(window, entry, monkeypatch):
    page = window._add_conversation_webview(entry)
    focused = []
    monkeypatch.setattr(window.tab_switcher, "SetFocus", lambda: focused.append("strip"))
    monkeypatch.setattr(page, "focus_content", lambda **_kw: focused.append("document"))
    on_strip = [False]
    monkeypatch.setattr(type(window), "_focus_is_within", staticmethod(lambda *_a: on_strip[0]))
    window._cycle_pane(1)
    on_strip[0] = True
    window._cycle_pane(-1)
    assert focused == ["strip", "document"]


@pytest.mark.parametrize("kind", ["history", "hermes"])
def test_picker_returns_a_webview_request_instead_of_opening_a_nested_dialog(entry, kind):
    ended = []
    if kind == "history":
        picker = SimpleNamespace(_current=lambda: entry, EndModal=ended.append)
        app.HistoryDialog._open_in_webview(picker)
    else:
        picker = SimpleNamespace(
            _selected=lambda: {"id": entry.session_id, "title": entry.title},
            EndModal=ended.append,
        )
        app.HermesSessionsDialog._open_in_webview(picker)
    assert picker.webview_entry.session_id == entry.session_id
    assert picker.webview_entry.title == entry.title
    assert ended == [wx.ID_OK]


@pytest.mark.parametrize("kind", ["history", "hermes"])
def test_main_window_destroys_the_picker_before_opening_the_reader(
    window, entry, kind, monkeypatch
):
    steps = []
    picker = SimpleNamespace(
        entry=None,
        attaching=False,
        webview_entry=entry,
        ShowModal=lambda: wx.ID_OK,
        Destroy=lambda: steps.append("picker destroyed"),
    )
    monkeypatch.setattr(
        window, "_add_conversation_webview", lambda *_a, **_k: steps.append("tab opened")
    )
    if kind == "history":
        monkeypatch.setattr(app, "HistoryDialog", lambda *_a, **_k: picker)
        monkeypatch.setattr(main_frame, "HistoryDialog", lambda *_a, **_k: picker)
        window._open_history()
    else:
        monkeypatch.setattr(app, "HermesSessionsDialog", lambda *_a, **_k: picker)
        monkeypatch.setattr(main_frame, "HermesSessionsDialog", lambda *_a, **_k: picker)
        window._open_hermes_sessions()
    assert steps == ["picker destroyed", "tab opened"]
