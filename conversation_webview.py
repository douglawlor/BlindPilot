"""A conversation in a modern native browser, with a readable text fallback.

The view is wx-accessible-webview's AccessibleWebView: a semantic HTML
document in a native web view, which a screen reader reads as a web page.
The conversation title is the one heading of level one and each exchange is a
heading under it, so the conversation can be reached by heading.

Nothing a conversation names is fetched. A picture in an answer is rendered
as a link, and a link is fetched only when the user asks for it: the view
opens it in the system browser rather than loading it here.
"""

from __future__ import annotations

import html
import logging
import platform
import re
from typing import Any, Callable, Optional

import wx

_TAGS = re.compile(r"<[^>]+>")


def _text_of(markup: str) -> str:
    """The words in a fragment, for a box that cannot render markup."""
    return html.unescape(_TAGS.sub("", markup or ""))


def _modern_browser_available() -> bool:
    """Whether this machine has a browser engine worth reading in.

    Windows still carries the engine from before, which cannot render the
    document BlindPilot writes and is not what the reader is used to here.
    Where only that engine is available, the conversation is shown as native
    text instead, which every reader can read.
    """
    try:
        import wx.html2 as html2
    except Exception:  # noqa: BLE001 - a build with no web view at all
        return False
    if platform.system() != "Windows":
        # macOS and Linux ship a modern engine with the platform.
        return True
    edge = html2.WebViewBackendEdge
    backend = edge.decode() if isinstance(edge, bytes) else edge
    try:
        return bool(html2.WebView.IsBackendAvailable(backend))
    except Exception:  # noqa: BLE001 - a backend that cannot be asked
        return False


class ConversationWebView:
    """The window a conversation is shown in.

    The browser is wx-accessible-webview's AccessibleWebView. Where there is
    no browser worth reading in, the conversation is shown as plain text in a
    read-only box rather than not being shown at all.
    """

    def __init__(self, parent, *, title: str, on_close: Callable[[], None]):
        self._on_close = on_close
        self._view: Any = None
        self.view: Any = None
        self.fallback: Optional[wx.TextCtrl] = None

        if not _modern_browser_available():
            self.fallback = self._text_box(parent)
            return

        try:
            from wx_accessible_webview import AccessibleWebView
        except Exception:  # noqa: BLE001 - a build without the package
            logging.getLogger("blindpilot").info(
                "the accessible web view package is not installed", exc_info=True
            )
            self.fallback = self._text_box(parent)
            return

        import wx.html2 as html2

        self._view = AccessibleWebView(
            parent,
            title=title,
            lang="en",
            live_region=False,
            handler_name="conversation",
            escape_to_close=True,
            on_close=self._close_from_page,
            on_message=self._on_message,
            open_links_externally=False,
        )
        if not self._view.using_webview:
            # The package fell back to a text box of its own.
            self.fallback = self._view.fallback
            return
        self.view = self._view.view
        self.view.Bind(html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)

    def _text_box(self, parent) -> wx.TextCtrl:
        """A read-only box the conversation can be read in as plain text."""
        return wx.TextCtrl(parent, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)

    @property
    def using_webview(self) -> bool:
        return self.view is not None

    @property
    def control(self) -> wx.Window:
        """The window the conversation is shown in."""
        if self.view is not None:
            return self.view
        if self.fallback is not None:
            return self.fallback
        raise RuntimeError("The conversation view was not built")

    def set_content(self, fragment: str) -> None:
        """Show the conversation, as a fragment of the document."""
        if self.view is not None:
            self._view.set_content(fragment)
        elif self.fallback is not None:
            self.fallback.SetValue(_text_of(fragment))

    def focus(self) -> None:
        """Move the reading cursor into the conversation."""
        if self.view is not None:
            self._view.focus()
        elif self.fallback is not None:
            self.fallback.SetFocus()

    def _close_from_page(self) -> None:
        """The page asked to close: Escape was pressed inside it."""
        self._on_close()

    def _on_message(self, message: dict) -> None:
        """A message from the page. The page asks to close; nothing else."""
        if isinstance(message, dict) and message.get("type") in ("__close", "close"):
            self._close_from_page()

    def _on_navigating(self, event) -> None:
        """Nothing in a conversation may navigate the view away from it.

        Web and mail links open in the system browser. Every other navigation
        is refused, because the conversation viewer has no back button.
        """
        url = event.GetURL() or ""
        if url in ("", "about:blank"):
            # Both the browser's own first page and the one set here.
            return
        if url.startswith(("http://", "https://", "mailto:")):
            wx.LaunchDefaultBrowser(url)
        event.Veto()
