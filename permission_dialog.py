"""The dialog a backend asks permission in.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Nothing is allowed by closing the dialog: Deny (or Keep Planning) is
the default button, and Escape is the same as choosing it.
"""

from __future__ import annotations

import json

import wx

from agent_backends import backend_label

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept local
# so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12


def _permission_text(tool: str, payload: dict) -> str:
    """A tool request as lines a screen reader reads top to bottom."""
    if tool == "ExitPlanMode":
        return str(payload.get("plan") or "Claude gave no plan text.")
    lines = [f"Tool: {tool}"]
    reason = payload.get("_reason")
    if reason:
        lines.append(f"Why it is asking: {reason}")
    for key, value in payload.items():
        if key == "_reason":
            continue
        shown = value if isinstance(value, str) else json.dumps(value, indent=2)
        lines.append(f"{key}:\n{shown}" if "\n" in shown else f"{key}: {shown}")
    return "\n".join(lines)


class PermissionDialog(wx.Dialog):
    """A backend asks to use a tool, or (Claude) to leave plan mode with a plan.

    Deny (or Keep Planning) is the default button, and Escape is the same as
    choosing it: nothing is allowed by closing a dialog.
    """

    ALLOW, ALLOW_SESSION, DENY = wx.ID_HIGHEST + 1, wx.ID_HIGHEST + 2, wx.ID_HIGHEST + 3
    APPROVE_EDITS, APPROVE_ASK = wx.ID_HIGHEST + 4, wx.ID_HIGHEST + 5

    def __init__(
        self,
        parent: wx.Window,
        backend: str,
        tool: str,
        payload: dict,
        offer_session: bool,
    ):
        plan = tool == "ExitPlanMode"
        label = backend_label(backend)
        title = f"Approve {label}'s Plan" if plan else f"{label} Wants to Use {tool}"
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = "&Plan:" if plan else "&Request:"
        sizer.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        self._details = wx.TextCtrl(
            self,
            value=_permission_text(tool, payload),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self._details.SetName(label.replace("&", "").rstrip(":"))
        self._details.SetMinSize(self.FromDIP(wx.Size(620, 300)))
        sizer.Add(self._details, 1, wx.EXPAND | wx.ALL, pad)
        reason_label = "What to &change (optional):" if plan else "Reason, if you &deny (optional):"
        sizer.Add(wx.StaticText(self, label=reason_label), 0, wx.LEFT | wx.RIGHT, pad)
        self._reason = wx.TextCtrl(self)
        self._reason.SetName(reason_label.replace("&", "").rstrip(":"))
        sizer.Add(self._reason, 0, wx.EXPAND | wx.ALL, pad)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        if plan:
            choices = [
                (self.APPROVE_EDITS, "&Approve, accept edits"),
                (self.APPROVE_ASK, "Approve, as&k before edits"),
                (self.DENY, "&Keep planning"),
            ]
        else:
            choices = [(self.ALLOW, "&Allow")]
            if offer_session:
                choices.append((self.ALLOW_SESSION, "Allow for this &session"))
            choices.append((self.DENY, "De&ny"))
        for button_id, text in choices:
            button = wx.Button(self, button_id, text)

            def on_button(_event: wx.CommandEvent, selected: int = button_id) -> None:
                self.EndModal(selected)

            button.Bind(wx.EVT_BUTTON, on_button)
            buttons.Add(button, 0, wx.RIGHT, self.FromDIP(PAD))
            if button_id == self.DENY:
                button.SetDefault()
        sizer.Add(buttons, 0, wx.ALL, pad)
        self.SetEscapeId(self.DENY)
        self.SetSizerAndFit(sizer)
        self._details.SetFocus()

    def reason(self) -> str:
        return self._reason.GetValue().strip()
