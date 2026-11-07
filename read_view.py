"""A modal read-only viewer for a single row's payload.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the viewer's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this viewer is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Focus moves into the text area so the user can review line by line,
spell words, and select and copy normally. Escape closes, asking
first when an edit has not been saved. ``_open_path`` came with it:
it is what the viewer's Edit in Your Editor button uses, and its
other caller reaches it through blindpilot_app's re-export.
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
from typing import Callable, Optional

import wx

from screen_speech import announce

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so this
# module does not import the application module).
PAD_DIALOG = 12


def _open_path(path) -> bool:
    """Hand a file or folder to whatever this platform opens it with.

    False means nothing happened, for any reason. Every caller says the path
    out loud in that case, so there is still a way through by hand.
    """
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(path))  # noqa: S606 - a path this application chose
            return True
        opener = "open" if system == "Darwin" else "xdg-open"
        proc = subprocess.Popen([opener, str(path)])
        # Reaped off this thread: a dropped Popen is a zombie until GC, and
        # `Popen.__del__` on one raises a ResourceWarning.
        threading.Thread(target=proc.wait, daemon=True).start()
        return True
    except (OSError, ValueError):
        return False


def _monospace_font(window: wx.Window) -> wx.Font:
    """The platform's fixed-width face at the window's own point size."""
    return wx.Font(wx.FontInfo(window.GetFont().GetPointSize()).Family(wx.FONTFAMILY_TELETYPE))


class ReadView(wx.Dialog):
    """Modal read-only viewer for a single row's payload. Esc closes.

    Focus moves into the text area so the user can review line by line, spell
    words, and select / copy normally with Ctrl+A / Ctrl+C.
    """

    def __init__(
        self,
        parent: wx.Window,
        text: str,
        title: str,
        monospace: bool = False,
        edit_path: Optional[str] = None,
        on_save: Optional[Callable[[str], None]] = None,
        start_editing: bool = False,
    ):
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        viewer = wx.TextCtrl(
            self,
            value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.TE_RICH2,
        )
        viewer.SetName(title)
        if monospace:
            # Code keeps its columns only in a fixed-width face. Same point
            # size as the system font, so nothing else about it changes.
            viewer.SetFont(_monospace_font(viewer))
        viewer.SetMinSize(self.FromDIP(wx.Size(660, 440)))
        viewer.SetInsertionPoint(0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(viewer, 1, wx.EXPAND | wx.ALL, self.FromDIP(PAD_DIALOG))
        if edit_path:
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            if on_save:
                # Read only until Edit; on_save writes the text back.
                self.edit_button = wx.Button(self, label="&Edit")
                self.edit_button.Bind(wx.EVT_BUTTON, lambda _event: self._toggle_edit())
                buttons.Add(self.edit_button, 0, wx.RIGHT, self.FromDIP(8))
            outside = wx.Button(self, label="Edit in &Your Editor")
            outside.Bind(wx.EVT_BUTTON, lambda _event: self._edit(edit_path))
            buttons.Add(outside)
            close = wx.Button(self, wx.ID_CANCEL, "Close")
            close.Bind(wx.EVT_BUTTON, lambda _event: self._close())
            buttons.Add(close, 0, wx.LEFT, self.FromDIP(8))
            sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, self.FromDIP(PAD_DIALOG))
        self.SetSizerAndFit(sizer)

        self.viewer = viewer
        self._on_save = on_save
        self._saved = text
        self._editing = False
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, lambda _event: self._close())
        viewer.SetFocus()
        self.CentreOnParent()
        if on_save and start_editing:
            self._toggle_edit()

    def _edit(self, path: str) -> None:
        if not _open_path(path):
            announce(f"Could not open {path}")

    def _toggle_edit(self) -> None:
        """Edit turns the text editable and becomes Save; Save writes it."""
        if self._editing:
            self._save()
            return
        self._editing = True
        self.viewer.SetEditable(True)
        self.edit_button.SetLabel("&Save")
        self.viewer.SetFocus()
        announce("Editing. Ctrl+S saves.")

    def _save(self) -> bool:
        assert self._on_save is not None
        text = self.viewer.GetValue()
        try:
            self._on_save(text)
        except (OSError, IndexError, ValueError) as exc:
            wx.MessageBox(f"Could not save: {exc}", "Save", wx.OK | wx.ICON_ERROR, self)
            return False
        self._saved = text
        announce("Saved")
        return True

    def _close(self) -> None:
        """Close, asking first when an edit has not been saved."""
        if self._editing and self.viewer.GetValue() != self._saved:
            answer = wx.MessageBox(
                "Save your changes?", "Save", wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, self
            )
            if answer == wx.CANCEL or (answer == wx.YES and not self._save()):
                return
        self.EndModal(wx.ID_CANCEL)

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._close()
            return
        if self._editing and event.ControlDown() and event.GetKeyCode() == ord("S"):
            self._save()
            return
        event.Skip()
