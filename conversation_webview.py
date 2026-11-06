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
from pathlib import Path
from typing import Any, Callable, Optional

import wx

_TAGS = re.compile(r"<[^>]+>")
_webview_loader: Any = None

# Native browser children may keep application shortcuts from wx's accelerator
# table. Forward only the tab/pane commands; ordinary document keys stay here.
_TAB_KEYS_JS = """
document.addEventListener('keydown', function(e) {
    var command = null;
    var modified = e.ctrlKey || e.metaKey;
    if (modified && !e.altKey && e.key === 'Tab') {
        command = e.shiftKey ? 'previous-tab' : 'next-tab';
    } else if (modified && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'w') {
        command = 'close';
    } else if (!modified && !e.altKey && e.key === 'F6') {
        command = e.shiftKey ? 'previous-pane' : 'next-pane';
    } else if (modified && !e.altKey && !e.shiftKey && /^[1-9]$/.test(e.key)) {
        command = 'tab-' + e.key;
    } else if (modified && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'l') {
        command = 'focus-content';
    } else if (modified && !e.altKey && e.shiftKey && (e.key === ']' || e.key === '[')) {
        command = e.key === ']' ? 'next-tab' : 'previous-tab';
    }
    if (command && window.conversation && window.conversation.postMessage) {
        e.preventDefault();
        window.conversation.postMessage(JSON.stringify({type:'tab-command', command:command}));
    }
});
"""


def _load_webview2_loader() -> None:
    """Keep wxPython's bundled loader available to wxWidgets on Windows.

    wxWidgets looks up the DLL by name. Python can import wx from its package
    directory without adding that directory to the Windows DLL search path.
    Load the known bundled DLL by absolute path, without changing PATH or
    searching the working directory.
    """
    global _webview_loader
    if _webview_loader is not None:
        return
    import ctypes

    try:
        _webview_loader = ctypes.WinDLL(
            str(Path(wx.__file__).resolve().parent / "WebView2Loader.dll")
        )
    except OSError:
        logging.getLogger("blindpilot").info(
            "could not load wxPython's WebView2 loader", exc_info=True
        )


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
        if not html2.WebView.IsBackendAvailable(backend):
            _load_webview2_loader()
        return bool(html2.WebView.IsBackendAvailable(backend))
    except Exception:  # noqa: BLE001 - a backend that cannot be asked
        return False


class ConversationWebView:
    """The window a conversation is shown in.

    The browser is wx-accessible-webview's AccessibleWebView. Where there is
    no browser worth reading in, the conversation is shown as plain text in a
    read-only box rather than not being shown at all.
    """

    def __init__(
        self,
        parent,
        *,
        title: str,
        on_close: Callable[[], None],
        can_focus: Optional[Callable[[], bool]] = None,
        on_command: Optional[Callable[[str], None]] = None,
    ):
        self._on_close = on_close
        self._closed = False
        self._can_focus = can_focus or (lambda: True)
        self._on_command = on_command
        self._loaded = False
        self._focus_requested = False
        self._view: Any = None
        self.view: Any = None
        self.fallback: Optional[wx.TextCtrl] = None
        self._initial_document_pending = True

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

        owner = self

        class ConversationBrowser(AccessibleWebView):
            def _run(self, script: str) -> None:
                # The package's synchronous RunScript spins a nested event
                # loop on Windows, allowing focus/load callbacks to reenter
                # it and stall the GUI. Content and focus need no return value.
                if not owner._closed and self.view is not None and self.view:
                    self.view.RunScriptAsync(script)

        self._view = ConversationBrowser(
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
        self.view.Bind(html2.EVT_WEBVIEW_LOADED, self._on_loaded)

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
        if self._closed:
            return
        if self.view is not None:
            self._view.set_content(fragment)
        elif self.fallback is not None:
            self.fallback.SetValue(_text_of(fragment))

    def focus(self) -> None:
        """Move the reading cursor into the conversation."""
        if self._closed or not self._can_focus():
            return
        if self.view is not None:
            if self._loaded:
                self._view.focus()
            else:
                # The package's own deferred focus is unconditional. Keep the
                # request here so a later load cannot focus an inactive tab.
                self._focus_requested = True
        elif self.fallback is not None:
            self.fallback.SetFocus()

    def _on_loaded(self, event) -> None:
        self._loaded = True
        event.Skip()  # Let AccessibleWebView flush its queued content first.
        wx.CallAfter(self._finish_load)

    def _finish_load(self) -> None:
        if self._closed or self.view is None or not self.view:
            return
        if self._on_command is not None:
            self.view.RunScriptAsync(_TAB_KEYS_JS)
        if self._focus_requested:
            self._focus_requested = False
            self.focus()

    def _close_from_page(self) -> None:
        """The page asked to close: Escape was pressed inside it."""
        if not self._closed:
            self._on_close()

    def invalidate(self) -> None:
        """Stop queued browser work before the page or frame is destroyed."""
        self._closed = True
        self._focus_requested = False
        self._on_command = None

    def _on_message(self, message: dict) -> None:
        """A message from the page. The page asks to close; nothing else."""
        if self._closed:
            return
        if isinstance(message, dict) and message.get("type") in ("__close", "close"):
            self._close_from_page()
        elif (
            isinstance(message, dict)
            and message.get("type") == "tab-command"
            and isinstance(message.get("command"), str)
            and self._on_command is not None
        ):
            self._on_command(message["command"])

    def _on_navigating(self, event) -> None:
        """Nothing in a conversation may navigate the view away from it.

        Web and mail links open in the system browser. Every other navigation
        is refused, because the conversation viewer has no back button.
        """
        url = event.GetURL() or ""
        if self._initial_document_pending and url.startswith(
            "data:text/html;charset=utf-8;base64,"
        ):
            # wxWidgets 3.3 loads SetPage's initial document as a data URL.
            # Only that first document may navigate here; links in it cannot.
            self._initial_document_pending = False
            return
        if url in ("", "about:blank"):
            # Both the browser's own first page and the one set here.
            return
        if url.startswith(("http://", "https://", "mailto:")):
            wx.LaunchDefaultBrowser(url)
        event.Veto()
