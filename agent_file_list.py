"""One kind of file one backend keeps, for What Agents Know.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Ends with wx.ID_OPEN to read the selected entry and wx.ID_EDIT to
edit it; where entries can be added and removed, wx.ID_ADD and
wx.ID_DELETE (the Delete key too). Escape goes back, Read is the
default button, and focus starts in the list.
"""

from __future__ import annotations

import wx

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so
# this module does not import the application module).
PAD_DIALOG = 12


class AgentFileList(wx.Dialog):
    """One kind of file one backend keeps, for What Agents Know.

    Ends with wx.ID_OPEN to read the selected entry, wx.ID_EDIT to edit it,
    and where entries can be added and removed with wx.ID_ADD or wx.ID_DELETE
    (the Delete key too).
    Escape goes back.
    """

    def __init__(
        self, parent: wx.Window, kind: str, labels: list[str], selected: int, add_label: str = ""
    ):
        super().__init__(parent, title=kind, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        memories = bool(add_label)
        hint = f"{kind}. Enter reads one."
        if memories:
            hint += " Delete removes one."
        prompt = wx.StaticText(self, label=hint)
        self.list = wx.ListBox(self, choices=labels, style=wx.LB_SINGLE)
        self.list.SetName(kind)
        self.list.SetMinSize(self.FromDIP(wx.Size(560, 320)))
        if labels:
            self.list.SetSelection(min(selected, len(labels) - 1))
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._end(wx.ID_OPEN))
        self.list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        actions = [(wx.ID_OPEN, "&Read"), (wx.ID_EDIT, "&Edit")]
        if memories:
            actions += [(wx.ID_ADD, add_label), (wx.ID_DELETE, "&Delete")]
        for id_, label in actions:
            button = wx.Button(self, id_, label)

            def on_button(_event: wx.CommandEvent, selected: int = id_) -> None:
                self._end(selected)

            button.Bind(wx.EVT_BUTTON, on_button)
            buttons.Add(button, 0, wx.RIGHT, self.FromDIP(8))
            if id_ == wx.ID_OPEN:
                button.SetDefault()
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "Back"))

        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(prompt, 0, wx.ALL, pad)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        sizer.Add(buttons, 0, wx.ALL, pad)
        self.SetSizerAndFit(sizer)
        (self.list if labels else self.FindWindow(wx.ID_ADD) or self.list).SetFocus()
        self.CentreOnParent()

    def _end(self, id_: int) -> None:
        if id_ != wx.ID_ADD and self.list.GetSelection() == wx.NOT_FOUND:
            return
        self.EndModal(id_)

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._end(wx.ID_OPEN)
        elif key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE) and self.FindWindow(wx.ID_DELETE):
            self._end(wx.ID_DELETE)
        else:
            event.Skip()
