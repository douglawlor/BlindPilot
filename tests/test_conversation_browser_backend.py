"""The browser behind a conversation, exercised without a browser runtime.

The view is built on wx-accessible-webview, so what is checked here is the
part BlindPilot owns: which browser is asked for, what is handed to it, and
what happens when there is none.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import conversation_webview as viewer

wx = pytest.importorskip("wx")
html2 = pytest.importorskip("wx.html2")


class FakePackageView:
    """The package's view, replaced by something that records what it was told."""

    created: list[dict] = []

    def __init__(self, parent, **kwargs):
        self.parent = parent
        self.kwargs = kwargs
        self.view = SimpleNamespace(Bind=lambda *_args: None)
        self.fallback = None
        self.using_webview = True
        self.content: list[str] = []
        self.focused = False
        FakePackageView.created.append(kwargs)

    def set_content(self, fragment: str) -> None:
        self.content.append(fragment)

    def focus(self) -> None:
        self.focused = True


def _fake_package(monkeypatch) -> type[FakePackageView]:
    FakePackageView.created.clear()
    monkeypatch.setattr(viewer, "_modern_browser_available", lambda: True)
    monkeypatch.setattr("wx_accessible_webview.AccessibleWebView", FakePackageView)
    return FakePackageView


def test_the_package_is_asked_for_the_conversation_by_its_title(monkeypatch):
    """The title names the conversation, and Escape has to get out of it.

    The native web control keeps Escape, so the package bridges it out of the
    page and back to the dialog; without that bridge the title bar is the only
    way out.
    """
    fake = _fake_package(monkeypatch)

    viewer.ConversationWebView(None, title="Fix & ship", on_close=lambda: None)

    assert fake.created[0]["title"] == "Fix & ship"
    assert fake.created[0]["escape_to_close"] is True


def test_the_conversation_is_handed_over_as_a_fragment(monkeypatch):
    """The package renders what it is given; BlindPilot decides what that is."""
    fake = _fake_package(monkeypatch)

    view = viewer.ConversationWebView(None, title="A conversation", on_close=lambda: None)
    view.set_content("<h1>A conversation</h1>")

    assert view.using_webview
    assert fake.created[0]["title"] == "A conversation"
    assert view._view.content == ["<h1>A conversation</h1>"]


def test_the_reading_cursor_goes_into_the_conversation(monkeypatch):
    """Focusing the control is not enough: the cursor has to go inside it."""
    _fake_package(monkeypatch)

    view = viewer.ConversationWebView(None, title="A conversation", on_close=lambda: None)
    view._loaded = True
    view.focus()

    assert view._view.focused


def test_a_late_browser_load_does_not_focus_a_tab_switched_away_from(monkeypatch):
    _fake_package(monkeypatch)
    active = [True]
    view = viewer.ConversationWebView(
        None, title="Conversation", on_close=lambda: None, can_focus=lambda: active[0]
    )
    view.focus()
    assert not view._view.focused
    active[0] = False
    view._loaded = True
    view._finish_load()
    assert not view._view.focused
    active[0] = True
    view.focus()
    assert view._view.focused


def test_browser_tab_commands_are_forwarded_to_the_owner(monkeypatch):
    _fake_package(monkeypatch)
    commands = []
    view = viewer.ConversationWebView(
        None, title="Conversation", on_close=lambda: None, on_command=commands.append
    )
    view._on_message({"type": "tab-command", "command": "next-tab"})
    view._on_message({"type": "tab-command", "command": None})
    assert commands == ["next-tab"]


def test_package_scripts_run_without_a_nested_synchronous_event_loop(monkeypatch):
    _fake_package(monkeypatch)
    scripts = []
    view = viewer.ConversationWebView(None, title="Conversation", on_close=lambda: None)
    view._view.view.RunScriptAsync = scripts.append
    view._view._run("window.__awv.focus();")
    assert scripts == ["window.__awv.focus();"]


def test_invalidated_browser_drops_queued_scripts_focus_and_commands(monkeypatch):
    _fake_package(monkeypatch)
    actions = []
    view = viewer.ConversationWebView(
        None,
        title="Conversation",
        on_close=lambda: actions.append("close"),
        on_command=actions.append,
    )
    view._view.view.RunScriptAsync = actions.append
    view.focus()
    view.invalidate()
    view._finish_load()
    view._view._run("late script")
    view._on_message({"type": "tab-command", "command": "next-tab"})
    view._close_from_page()
    assert actions == []
    assert not view._focus_requested


def test_windows_without_webview2_uses_text_and_never_opens_ie(frame, monkeypatch):
    """Windows still carries the engine from before, and it is not worth
    reading in: the conversation is shown as text instead."""

    monkeypatch.setattr(viewer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(html2.WebView, "IsBackendAvailable", lambda backend: False)
    monkeypatch.setattr(viewer, "_load_webview2_loader", lambda: None)

    def unexpected_browser(*_args, **_kwargs):
        raise AssertionError("an unavailable Edge backend must not fall back to IE")

    monkeypatch.setattr(html2.WebView, "New", unexpected_browser)
    view = viewer.ConversationWebView(frame, title="Conversation", on_close=lambda: None)

    assert not view.using_webview
    assert view.control is view.fallback


def test_the_modern_browser_is_the_one_asked_for_on_windows(monkeypatch):
    """The legacy engine reports itself as available, so it is not asked."""
    monkeypatch.setattr(viewer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        html2.WebView,
        "IsBackendAvailable",
        lambda backend: backend == "wxWebViewEdge",
    )

    assert viewer._modern_browser_available() is True


def test_windows_retries_edge_after_loading_the_bundled_loader(monkeypatch):
    monkeypatch.setattr(viewer.platform, "system", lambda: "Windows")
    loaded = []
    monkeypatch.setattr(viewer, "_load_webview2_loader", lambda: loaded.append(True))
    monkeypatch.setattr(html2.WebView, "IsBackendAvailable", lambda backend: bool(loaded))

    assert viewer._modern_browser_available() is True
    assert loaded == [True]


def test_loader_uses_wx_package_path_and_keeps_the_dll_loaded(monkeypatch):
    import ctypes
    from pathlib import Path

    loaded = []
    handle = object()
    monkeypatch.setattr(viewer, "_webview_loader", None)
    monkeypatch.setattr(viewer.wx, "__file__", str(Path("wx-package") / "__init__.py"))

    def load(path):
        loaded.append(path)
        return handle

    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    viewer._load_webview2_loader()
    viewer._load_webview2_loader()

    assert loaded == [str(Path("wx-package").resolve() / "WebView2Loader.dll")]
    assert viewer._webview_loader is handle


def test_a_missing_loader_keeps_the_text_fallback_available(monkeypatch):
    import ctypes

    monkeypatch.setattr(viewer, "_webview_loader", None)

    def missing(path):
        raise OSError("loader missing")

    monkeypatch.setattr(ctypes, "WinDLL", missing, raising=False)
    viewer._load_webview2_loader()

    assert viewer._webview_loader is None


def test_a_link_opens_in_the_browser_and_the_conversation_stays(monkeypatch):
    """A link is fetched only when the user asks for it, and never here."""
    opened: list[str] = []
    monkeypatch.setattr(viewer.wx, "LaunchDefaultBrowser", opened.append)
    _fake_package(monkeypatch)

    view = viewer.ConversationWebView(None, title="A conversation", on_close=lambda: None)

    class _Event:
        def __init__(self, url):
            self.url = url
            self.vetoed = False

        def GetURL(self):
            return self.url

        def Veto(self):
            self.vetoed = True

    event = _Event("https://example.com/page")
    view._on_navigating(event)

    assert opened == ["https://example.com/page"]
    assert event.vetoed, "the view was allowed to navigate away from the conversation"


def test_navigation_that_is_not_a_link_is_refused(monkeypatch):
    """The conversation viewer has no back button, so it goes nowhere."""
    opened: list[str] = []
    monkeypatch.setattr(viewer.wx, "LaunchDefaultBrowser", opened.append)
    _fake_package(monkeypatch)

    view = viewer.ConversationWebView(None, title="A conversation", on_close=lambda: None)

    class _Event:
        def __init__(self, url):
            self.url = url
            self.vetoed = False

        def GetURL(self):
            return self.url

        def Veto(self):
            self.vetoed = True

    event = _Event("file:///C:/Windows/System32/calc.exe")
    view._on_navigating(event)

    assert opened == []
    assert event.vetoed


def test_the_initial_setpage_document_can_load_but_data_links_cannot(monkeypatch):
    _fake_package(monkeypatch)
    view = viewer.ConversationWebView(None, title="Conversation", on_close=lambda: None)
    vetoed = []
    event = SimpleNamespace(
        GetURL=lambda: "data:text/html;charset=utf-8;base64,PGgxPkNvbnZlcnNhdGlvbjwvaDE+",
        Veto=lambda: vetoed.append(True),
    )

    view._on_navigating(event)
    assert vetoed == []
    view._on_navigating(event)
    assert vetoed == [True]
