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
import threading
from dataclasses import dataclass

from agent_backends import backend_label
from dialog_tasks import _call_after_alive
from history_status import history_diagnostics
from markdown_rows import _MD_CONVERSATION
from screen_speech import announce
from session_history import HistoryEntry, HistoryTurn, load_turns
from pathlib import Path
from typing import Any
from collections.abc import Callable

import wx

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so
# this module does not import the application module).
PAD_DIALOG = 12


def _focus_is_within(focus, control) -> bool:
    """Whether focus is on a control or inside one of its children.

    The same walk MainFrame does for its own controls, kept here so
    this module does not import the application module.
    """
    current = focus
    while current is not None:
        if current is control:
            return True
        current = current.GetParent()
    return False


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
        can_focus: Callable[[], bool] | None = None,
        on_command: Callable[[str], None] | None = None,
    ):
        self._on_close = on_close
        self._closed = False
        self._can_focus = can_focus or (lambda: True)
        self._on_command = on_command
        self._loaded = False
        self._focus_requested = False
        self._view: Any = None
        self.view: Any = None
        self.fallback: wx.TextCtrl | None = None
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


@dataclass
class Turn:
    prompt: str
    response: str = ""


class ConversationWebViewPanel(wx.Panel):
    """A read-only conversation in a tab of the main window.

    Uses a modern native browser to render the conversation
    as HTML so it reads correctly in NVDA and JAWS. The conversation is
    loaded from the backend's transcript and rendered with Markdown.

    Where the machine has no modern web view backend, the conversation is
    shown as plain text in a read-only box
    rather than not being shown at all.
    """

    def __init__(
        self,
        parent: wx.Window,
        entry: HistoryEntry,
        title: str,
        missing: str = "No messages found in this conversation.",
        *,
        on_close: Callable[[ConversationWebViewPanel], None],
        on_command: Callable[[str], None] | None = None,
    ):
        super().__init__(parent, name=f"Read: {title}")

        self._entry = entry
        self._title = title
        self._missing = missing
        self._on_close = on_close
        self._on_command = on_command
        self.return_to: wx.Window | None = None
        self.last_status = "Loading conversation"
        # The browser adapter is imported only when a conversation is opened.
        self._webview: Any | None = None
        self._text: wx.TextCtrl | None = None
        # Set when the window is closed, so a load that was already on its way
        # does not speak for a window that is no longer there.
        self._closed = False

        view = self._build_view()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(view, 1, wx.EXPAND | wx.ALL, self.FromDIP(PAD_DIALOG))

        self.source_status = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, name="History source status"
        )
        self.source_status.SetMinSize(self.FromDIP(wx.Size(560, 85)))
        self.source_status.Hide()
        sizer.Add(self.source_status, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_DIALOG))

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        close_button = wx.Button(self, wx.ID_CANCEL, "Close")
        buttons.Add(close_button, 0, wx.LEFT, self.FromDIP(8))
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, self.FromDIP(PAD_DIALOG))

        self.SetSizer(sizer)

        self.Bind(wx.EVT_BUTTON, lambda _e: self._close(), id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        _call_after_alive(self._load_conversation)

    def _build_view(self) -> wx.Window:
        """Build a modern browser or its readable native text fallback."""
        from conversation_webview import ConversationWebView

        self._webview = ConversationWebView(
            self,
            title=self._title,
            on_close=self._close,
            can_focus=self._can_focus,
            on_command=self._on_command,
        )
        control = self._webview.control
        # The tab already says which conversation this is. The
        # control inside it is named for what it is, so the title is not said
        # three times over.
        control.SetName("Conversation")
        return control

    def _is_active(self) -> bool:
        if self._closed or not self:
            return False
        book = self.GetParent()
        return not isinstance(book, wx.BookCtrlBase) or (
            book.IsShown() and book.GetCurrentPage() is self
        )

    def _can_focus(self) -> bool:
        if not self._is_active():
            return False
        frame = wx.GetTopLevelParent(self)
        strip = getattr(frame, "tab_switcher", None)
        return not (
            getattr(frame, "_strip_keeps_focus", False)
            or (strip is not None and _focus_is_within(wx.Window.FindFocus(), strip))
        )

    def focus_content(self, *, from_navigation: bool = False) -> None:
        if from_navigation and self._is_active() and self._webview is not None:
            self._webview.control.SetFocus()
        if self._can_focus() and self._webview is not None:
            self._webview.focus()

    def _say_status(self, text: str) -> None:
        self.last_status = text
        if self._is_active():
            frame = wx.GetTopLevelParent(self)
            status = getattr(frame, "_set_status_text", None)
            if status is not None:
                status(text)
            announce(text)

    def _announce_selected(self, index: int, count: int) -> None:
        if self._is_active():
            announce(f"Reading: {self._title}, tab {index + 1} of {count}. {self.last_status}")

    def _load_conversation(self) -> None:
        """Read the conversation off the GUI thread, then show it.

        A transcript can be tens of megabytes, and reading one on the GUI
        thread freezes the modal loop, and the screen reader with it.
        """
        if self._closed or not self:
            # The window was closed before the load began. Saying "Loading
            # conversation" now would be the last thing a window nobody can
            # reach has to say.
            return
        self._say_status("Loading conversation")

        def work() -> None:
            try:
                with history_diagnostics() as issues:
                    turns = load_turns(self._entry)
                error = "\n".join(issue.message for issue in issues)
            except Exception:  # noqa: BLE001 - a store that cannot be read
                turns, error = [], "This conversation could not be read."
                logging.getLogger("blindpilot").warning(
                    "could not read a conversation", exc_info=True
                )
            _call_after_alive(self._show_conversation, turns, error)

        threading.Thread(target=work, daemon=True).start()

    def _show_conversation(self, turns: list[HistoryTurn], error: str) -> None:
        if self._closed or not self:
            return
        self.source_status.ChangeValue(error)
        self.source_status.Show(bool(error))
        self.Layout()
        if error:
            if not turns:
                self._show_text(error)
                self._say_status(f"Error: {error}")
                return
        if not turns:
            self._show_text(self._missing)
            self._say_status(self._missing)
            return
        messages = sum(1 for turn in turns for part in (turn.prompt, turn.response) if part.strip())
        view = self._webview if self._webview is not None and self._webview.using_webview else None
        if view is not None:
            view.set_content(self._conversation_html(turns))
        else:
            self._show_text(self._conversation_text(turns))
        self._say_status(
            f"Conversation loaded: {messages} messages." + (f" {error}" if error else "")
        )
        if view is not None:
            # The focus call is what puts the reading cursor in the
            # conversation. The native control taking focus is not enough:
            # without it the reader is left with a dialog and nothing in it.
            self.focus_content()

    def _conversation_html(self, turns: list[HistoryTurn]) -> str:
        """The conversation as a document fragment.

        The title is the one heading of level one, so the reader can get to
        the conversation by heading. Each exchange is a level two heading
        under it, numbered, so two exchanges that began alike can be told
        apart in a list of headings.
        """
        parts = [f"<h1>{html.escape(self._title)}</h1>"]
        speaker = html.escape(backend_label(self._entry.backend))
        for number, turn in enumerate(turns, start=1):
            if turn.prompt.strip():
                parts.append(
                    f"<article><h2>You, exchange {number}</h2>"
                    f"{_MD_CONVERSATION.render(turn.prompt)}</article>"
                )
            if turn.response.strip():
                parts.append(
                    f"<article><h2>{speaker}, exchange {number}</h2>"
                    f"{_MD_CONVERSATION.render(turn.response)}</article>"
                )
        return "\n".join(parts)

    def _conversation_text(self, turns: list[HistoryTurn]) -> str:
        """The conversation as plain text, for a machine with no web view."""
        parts: list[str] = []
        speaker = backend_label(self._entry.backend)
        for number, turn in enumerate(turns, start=1):
            if turn.prompt.strip():
                parts.append(f"You, exchange {number}\n{turn.prompt.strip()}")
            if turn.response.strip():
                parts.append(f"{speaker}, exchange {number}\n{turn.response.strip()}")
        return "\n\n".join(parts)

    def _show_text(self, text: str) -> None:
        """Show the conversation as plain text, where there is no web view."""
        control = self._plain_control()
        if control is None:
            if self._webview is not None:
                self._webview.set_content(f"<p>{html.escape(text)}</p>")
                self.focus_content()
            return
        control.SetValue(text)
        if self._can_focus():
            control.SetFocus()

    def _plain_control(self) -> wx.TextCtrl | None:
        """The box the text is shown in, when there is no web view."""
        if self._text is not None:
            return self._text
        if self._webview is not None and not self._webview.using_webview:
            return self._webview.fallback
        return None

    def _close(self) -> None:
        """Ask the notebook owner to remove this page, by identity."""
        if not self._closed:
            self._on_close(self)

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._close()
            return
        event.Skip()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        # DeletePage destroys from C++; it need not call Python's Destroy.
        if event.GetEventObject() is self:
            self.mark_closed()
        event.Skip()

    def mark_closed(self) -> None:
        self._closed = True
        invalidate = getattr(self._webview, "invalidate", None)
        if invalidate is not None:
            invalidate()

    def Destroy(self) -> bool:  # type: ignore[override]
        self.mark_closed()
        return super().Destroy()
