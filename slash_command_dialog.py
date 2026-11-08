"""Pick a slash command from a labeled list.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

The keyboard contract: Enter on a button is the button's, Enter
elsewhere accepts the highlighted row, Escape cancels, and focus
starts in the list.
"""

from __future__ import annotations


import wx

from conversation_list import make_conversation_list

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so this
# module does not import the application module).
PAD_DIALOG = 12


class SlashCommandDialog(wx.Dialog):
    """Pick a slash command from a labeled list, in place of a stock choice dialog."""

    def __init__(self, parent: wx.Window, message: str, labels: list[str]):
        super().__init__(
            parent,
            title="Slash Commands",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        message_text = wx.StaticText(self, label=message)

        self.list_box = make_conversation_list(self, name="Slash commands")
        self.list_box.Set(labels)
        if labels:
            self.list_box.SetSelection(0)
        self.list_box.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._accept())

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        pad = self.FromDIP(PAD_DIALOG)
        self.list_box.SetMinSize(self.FromDIP(wx.Size(480, 220)))
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(message_text, 0, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.list_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)

        self.Bind(wx.EVT_BUTTON, lambda _e: self._accept(), id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.list_box.SetFocus()
        self.CentreOnParent()

    def GetSelection(self) -> int:
        return self.list_box.GetSelection()

    def SetLabels(self, labels: list[str]) -> None:
        """Replace the list while it is open, keeping the same row selected."""
        rows = self.list_box.GetRows()
        index = self.list_box.GetSelection()
        selected = rows[index].label if 0 <= index < len(rows) else ""
        self.list_box.Set(labels)
        rows = self.list_box.GetRows()
        if not rows:
            return
        current = [row.label for row in rows]
        if selected in current:
            index = current.index(selected)
        else:
            index = self.list_box.GetSelection()
        self.list_box.SetSelection(min(max(index, 0), len(rows) - 1))

    def _accept(self) -> None:
        self.EndModal(wx.ID_OK)

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            # CHAR_HOOK sees Enter before the focused button does. Enter on
            # Cancel has to cancel, not choose the highlighted row.
            if isinstance(self.FindFocus(), wx.Button):
                event.Skip()
                return
            self._accept()
            return
        event.Skip()
