"""One subagent's activity, in a read-only log that keeps up with it.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

The caret stays where the reader put it; one left at the end follows
the log. Escape is Close, and focus starts in the log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import wx

# Dialog margin, matching blindpilot_app.PAD (kept local so this
# module does not import the application module).
PAD = 8


@dataclass
class SubagentEntry:
    name: str
    status: str
    lines: list[str] = field(default_factory=list)

    def label(self) -> str:
        """The list row: name, state, and the last thing it did."""
        last = self.lines[-1] if self.lines else ""
        if len(last) > 120:
            last = last[:119].rstrip() + "…"
        return f"{self.name}, {self.status}: {last}" if last else f"{self.name}, {self.status}"


class SubagentDialog(wx.Dialog):
    """One subagent's activity in a read-only edit field that keeps up with it.

    New lines are added while the dialog is open. The caret stays where the
    reader put it; one left at the end follows the log, which is how a person
    tracks what the agent is doing without being pulled away from a line they
    went back to read.
    """

    def __init__(self, parent: wx.Window, agent_id: str, entry: SubagentEntry):
        super().__init__(
            parent,
            title=self._title_for(entry),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.agent_id = agent_id
        self.status_label = wx.StaticText(self, label=f"Status: {entry.status}")
        activity_label = wx.StaticText(self, label="Activity:")
        self.log = wx.TextCtrl(
            self,
            value="\n".join(entry.lines),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.log.SetName("Subagent activity")
        close = wx.Button(self, wx.ID_CLOSE, "Close")
        close.Bind(wx.EVT_BUTTON, lambda _e: self.EndModal(wx.ID_CLOSE))
        self.SetEscapeId(wx.ID_CLOSE)
        pad = self.FromDIP(PAD)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.status_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(activity_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.log, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, pad)
        self.SetSizer(sizer)
        self.SetSize(self.FromDIP(wx.Size(640, 480)))
        # The top: the log reads from the start, and End or Ctrl+End reaches
        # the newest line, which from then on follows the agent.
        self.log.SetInsertionPoint(0)
        self.log.SetFocus()

    @staticmethod
    def _title_for(entry: SubagentEntry) -> str:
        return f"Subagent: {entry.name}, {entry.status}"

    def update(self, entry: SubagentEntry, line: str) -> None:
        self.SetTitle(self._title_for(entry))
        self.status_label.SetLabel(f"Status: {entry.status}")
        if not line:
            return
        caret = self.log.GetInsertionPoint()
        following = caret >= self.log.GetLastPosition()
        self.log.AppendText(("\n" if self.log.GetLastPosition() else "") + line)
        if not following:
            self.log.SetInsertionPoint(caret)
