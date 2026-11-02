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
    view.focus()

    assert view._view.focused


def test_windows_without_webview2_uses_text_and_never_opens_ie(frame, monkeypatch):
    """Windows still carries the engine from before, and it is not worth
    reading in: the conversation is shown as text instead."""

    monkeypatch.setattr(viewer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(html2.WebView, "IsBackendAvailable", lambda backend: False)

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
