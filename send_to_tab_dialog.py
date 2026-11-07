"""Send a response to another tab, once or as an agent conversation.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

The message is editable before it goes. Choosing a kind of
conversation fills in both roles and enables the relay; both roles
stay editable. Escape cancels, and focus starts in the target list.
"""

from __future__ import annotations

from typing import Optional, Sequence

import wx

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so this
# module does not import the application module).
PAD_DIALOG = 12

# Kinds of agent conversation: (name, role of the session sending first, role
# of the session it goes to). Choosing one fills in the roles; both stay
# editable. Modelled on The Chat Place's kinds of conversation.
RELAY_KINDS = (
    ("Plain hand-off", "", ""),
    (
        "Builder and reviewer",
        "Build what the person asked for: make the changes, then say what you changed.",
        "Review each change the builder reports. Don't edit files. Say what is wrong, "
        "or DONE when it is right.",
    ),
    (
        "Second opinion",
        "Answer the person's question.",
        "Give an independent second opinion on the other agent's answer. Don't edit files.",
    ),
    (
        "Debate",
        "Argue for the approach in the opening message.",
        "Argue against it, and look for its weaknesses.",
    ),
    (
        "Plan and do",
        "Plan the work as numbered steps. Don't edit files.",
        "Carry out the plan the planner sends, step by step, and report back.",
    ),
)


class SendToTabDialog(wx.Dialog):
    """Send a response to another tab, once or as an agent conversation.

    The message is editable before it goes. With the relay box ticked, each
    tab's next answer is handed to the other, up to the hand-off limit.
    """

    def __init__(self, parent: wx.Window, tabs: Sequence[str], message: str):
        super().__init__(
            parent, title="Send Response to Another Tab",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )  # fmt: skip
        pad = self.FromDIP(PAD_DIALOG)
        rows = wx.BoxSizer(wx.VERTICAL)
        rows.Add(wx.StaticText(self, label="Send &to:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.target = wx.ListBox(self, choices=list(tabs), name="Send to")
        self.target.SetSelection(0)
        rows.Add(self.target, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
        rows.Add(wx.StaticText(self, label="&Message:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.message = wx.TextCtrl(self, value=message, style=wx.TE_MULTILINE, name="Message")
        self.message.SetMinSize(self.FromDIP(wx.Size(520, 180)))
        rows.Add(self.message, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.relay = wx.CheckBox(self, label="&Keep passing replies between the two tabs")
        rows.Add(self.relay, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        rows.Add(
            wx.StaticText(self, label="&Hand-offs before stopping:"),
            0, wx.LEFT | wx.RIGHT | wx.TOP, pad,
        )  # fmt: skip
        self.turns = wx.SpinCtrl(self, min=1, max=50, initial=6, name="Hand-offs before stopping")
        rows.Add(self.turns, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.manual = wx.CheckBox(self, label="&Ask me before each hand-off")
        self.manual.Enable(False)
        rows.Add(self.manual, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.relay.Bind(wx.EVT_CHECKBOX, self._on_relay_toggle)

        def field(label: str, name: str) -> wx.TextCtrl:
            rows.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            box = wx.TextCtrl(self, name=name)
            rows.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
            return box

        rows.Add(
            wx.StaticText(self, label="Kin&d of conversation:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad
        )
        self.kind = wx.Choice(
            self, choices=[kind[0] for kind in RELAY_KINDS], name="Kind of conversation"
        )
        self.kind.SetSelection(0)
        self.kind.Bind(wx.EVT_CHOICE, self._on_kind)
        rows.Add(self.kind, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.first_role = field("&Role of this session:", "Role of this session")
        self.second_role = field("Role of the recei&ving session:", "Role of the receiving session")
        rows.Add(wx.StaticText(self, label="Third a&gent:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.third = wx.Choice(self, choices=["None", *tabs], name="Third agent")
        self.third.SetSelection(0)
        rows.Add(self.third, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self.third_role = field("Rol&e of the third agent:", "Role of the third agent")
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        if buttons is not None:
            ok = self.FindWindowById(wx.ID_OK, self)
            if ok is not None:
                ok.SetLabel("&Send")
            rows.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(rows)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.target.SetFocus()
        self.CentreOnParent()

    def _on_relay_toggle(self, _event: Optional[wx.CommandEvent] = None) -> None:
        self.manual.Enable(self.relay.IsChecked())

    def _on_kind(self, _event: Optional[wx.CommandEvent] = None) -> None:
        _name, first, second = RELAY_KINDS[max(0, self.kind.GetSelection())]
        self.first_role.SetValue(first)
        self.second_role.SetValue(second)
        if first:
            self.relay.SetValue(True)
            self._on_relay_toggle()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()
