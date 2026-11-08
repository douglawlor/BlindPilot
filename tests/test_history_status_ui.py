"""History failures remain available to read without hiding healthy results."""

from contextlib import contextmanager
import time
from types import SimpleNamespace

import wx

import blindpilot_app as app
import conversation_webview  # noqa: E402
import history_dialog  # noqa: E402
from doubles import KeyEvent
from session_history import HistoryEntry, HistoryTurn


def _reading_panel(parent, entry, title, **kwargs):
    return app.ConversationWebViewPanel(
        parent, entry, title, on_close=lambda page: page.Destroy(), **kwargs
    )


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        wx.GetApp().Yield()
        time.sleep(0.01)
    assert predicate()


def test_partial_history_is_openable_and_status_survives_filtering(frame, monkeypatch):
    warning = "Codex history database is locked. Close other apps and select Reload."

    @contextmanager
    def diagnostics():
        yield [SimpleNamespace(message=warning)]

    entry = HistoryEntry("claude", "healthy", "Healthy conversation", "", 1)
    monkeypatch.setattr(history_dialog, "history_diagnostics", diagnostics)
    monkeypatch.setattr(history_dialog, "list_history", lambda *_a: [entry])
    spoken = []
    monkeypatch.setattr(history_dialog, "announce", lambda text, **_k: spoken.append(text))
    dialog = app.HistoryDialog(frame, "claude", ".")
    try:
        _wait(lambda: not dialog._loading)
        assert dialog._shown == [entry]
        assert dialog.FindWindow(wx.ID_OK).IsEnabled()
        assert dialog.source_status.IsShown()
        assert dialog.source_status.GetName() == "History source status"
        assert dialog.source_status.GetValue() == warning
        assert any(warning in text for text in spoken)
        dialog.filter_box.ChangeValue("no match")
        dialog._refresh()
        assert dialog.source_status.GetValue() == warning
        # A late callback for a different scope cannot replace the warning.
        dialog._history_loaded(dialog._load_serial - 1, [], "", "Old warning")
        assert dialog.source_status.GetValue() == warning
        serial = dialog._load_serial
        dialog._history_loaded(serial, [entry], "")
        assert not dialog.source_status.IsShown()
        assert dialog.source_status.GetValue() == ""
    finally:
        dialog.Destroy()


def test_empty_readable_history_has_no_failure_status(frame, monkeypatch):
    monkeypatch.setattr(history_dialog, "list_history", lambda *_a: [])
    monkeypatch.setattr(history_dialog, "announce", lambda *_a, **_k: None)
    dialog = app.HistoryDialog(frame, "claude", ".")
    try:
        _wait(lambda: not dialog._loading)
        assert dialog.summary.GetLabel() == "No past conversations found here"
        assert not dialog.source_status.IsShown()
        dialog._history_loaded(dialog._load_serial, [], "", "Claude history is unreadable.")
        assert "No past conversations" not in dialog.summary.GetLabel()
        assert dialog.source_status.IsShown()
    finally:
        dialog.Destroy()


def test_enter_in_source_status_does_not_open_a_conversation(monkeypatch):
    class Button:
        pass

    monkeypatch.setattr(app.wx, "Button", Button)
    status = object()
    stub = SimpleNamespace(_shown=[object()], source_status=status, FindFocus=lambda: status)
    for control_down in (False, True):
        event = KeyEvent(app.wx.WXK_RETURN, control_down=control_down)
        app.HistoryDialog._on_key(stub, event)
        assert event.skipped


def test_partial_conversation_keeps_text_and_announces_warning(frame, monkeypatch):
    monkeypatch.setattr(app.ConversationWebViewPanel, "_load_conversation", lambda _s: None)
    spoken = []
    monkeypatch.setattr(conversation_webview, "announce", lambda text, **_k: spoken.append(text))
    entry = HistoryEntry("claude", "partial", "Partial conversation", "", 1)
    dialog = _reading_panel(frame, entry, entry.title)
    displayed = []
    try:
        dialog._webview = None
        monkeypatch.setattr(dialog, "_show_text", displayed.append)
        warning = "Claude history has damaged records. Some messages may be missing."
        dialog._show_conversation(
            [HistoryTurn(prompt="Healthy prompt", response="Answer")], warning
        )
        assert "Healthy prompt" in displayed[-1]
        assert "Answer" in displayed[-1]
        assert dialog.source_status.GetValue() == warning
        assert dialog.source_status.IsShown()
        assert warning in spoken[-1]
    finally:
        dialog.Destroy()
