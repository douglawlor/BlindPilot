"""Read-only views: a wrapped label and a rendered page in a dialog.

Self-contained pieces of the window with no BlindPilot state of their
own. blindpilot_app re-exports both names, so call sites and tests that
reach them as ``blindpilot_app.FormattedView`` keep working.
"""

from __future__ import annotations

import logging
import platform

import wx


class WrappedText(wx.StaticText):
    """A StaticText that can be wrapped again after its label or width changes.

    Wrap writes newlines into the label itself, so wrapping a second time at a
    wider width would keep the old breaks. This keeps the text as it was set
    and wraps from that copy every time, so a resizable dialog can reflow its
    paragraphs on every size event.
    """

    def __init__(self, parent: wx.Window, label: str = ""):
        super().__init__(parent, label=label)
        self._unwrapped = label
        self._width = 0
        self._wrapping = False

    def SetLabel(self, label: str) -> None:
        if self._wrapping:
            # wxWidgets sets the broken-up text through this same method.
            super().SetLabel(label)
            return
        self._unwrapped = label
        super().SetLabel(label)
        if self._width > 0:
            self.Wrap(self._width)

    def Wrap(self, width: int) -> None:
        self._width = width
        self._wrapping = True
        try:
            super().SetLabel(self._unwrapped)
            super().Wrap(width)
        finally:
            self._wrapping = False


class FormattedView(wx.Dialog):
    """A rendered page in a web view; Escape closes, links open the browser.

    Nothing is fetched: the page is set from a string, and any navigation away
    from it is refused and handed to the default browser instead.
    """

    @classmethod
    def open(cls, parent: wx.Window, page: str, title: str) -> FormattedView | None:
        """The view, or None when this machine has no usable web view."""
        try:
            import wx.html2 as html2

            edge = html2.WebViewBackendEdge
            backend = (
                (edge.decode() if isinstance(edge, bytes) else edge)
                if platform.system() == "Windows"
                else ""
            )
            if backend and not html2.WebView.IsBackendAvailable(backend):
                return None
            return cls(parent, page, title, html2, backend)
        except Exception:
            logging.getLogger("blindpilot").info("formatted view unavailable", exc_info=True)
            return None

    def __init__(self, parent: wx.Window, page: str, title: str, html2, backend: str):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        kwargs = {"backend": backend} if backend else {}
        self.view = html2.WebView.New(self, wx.ID_ANY, **kwargs)
        self.view.SetMinSize(self.FromDIP(wx.Size(720, 520)))
        self.view.Bind(html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)
        self.view.Bind(html2.EVT_WEBVIEW_LOADED, lambda _e: self.view.SetFocus())
        self._loaded = False
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.view, 1, wx.EXPAND)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.view.SetPage(page, "")

    def _on_navigating(self, event) -> None:
        url = event.GetURL()
        if not self._loaded:
            # The first navigation is the page being set.
            self._loaded = True
            return
        if url.startswith(("http://", "https://", "mailto:")):
            wx.LaunchDefaultBrowser(url)
        event.Veto()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()
