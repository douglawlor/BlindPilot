"""Report a Bug: describe it, see exactly what else goes, file it.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Open on GitHub copies the report and opens the new-issue page with
it filled in; Copy Report only copies it. Nothing from a conversation
is ever included, and the Also included field shows exactly what
else goes before anything is sent.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable

import wx

from app_updater import GITHUB_REPOSITORY
from screen_speech import announce

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12


def _copy_to_clipboard(text: str) -> bool:
    if wx.TheClipboard.Open():
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            return True
        finally:
            wx.TheClipboard.Close()
    return False


def bug_report_body(summary: str, happened: str, expected: str, steps: str, facts: str) -> str:
    """The issue text, in the order a maintainer reads it."""
    return "\n\n".join(
        [
            f"## What happened\n\n{happened or summary}",
            f"## What I expected\n\n{expected or 'Not given.'}",
            f"## Steps\n\n{steps or 'Not given.'}",
            f"## Versions and settings\n\n```\n{facts}\n```",
        ]
    )


class BugReportDialog(wx.Dialog):
    """Help, Report a Bug: describe it, see exactly what else goes, file it.

    Open on GitHub copies the report and opens GitHub's new-issue page with it
    filled in; Copy Report only copies it, for anyone without a GitHub account
    to paste into the Telegram group or an email instead.
    """

    def __init__(self, parent: wx.Window, facts: str):
        super().__init__(
            parent, title="Report a Bug", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self._facts = facts
        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def field(label: str, lines: int, extra: int = 0) -> wx.TextCtrl:
            sizer.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            # Windows cannot make a text box read-only after it is made.
            style = (wx.TE_MULTILINE if lines > 1 else 0) | extra
            box = wx.TextCtrl(self, style=style)
            box.SetName(label.replace("&", "").rstrip(":"))
            box.SetMinSize(self.FromDIP(wx.Size(560, 22 * lines)))
            sizer.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
            return box

        self.summary = field("&Summary:", 1)
        self.happened = field("&What happened:", 4)
        self.expected = field("What you e&xpected:", 3)
        self.steps = field("S&teps to make it happen:", 4)
        included = field("&Also included (nothing from your conversations):", 6, wx.TE_READONLY)
        included.SetValue(facts)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for label, action in (
            ("&Open on GitHub", self._open_github),
            ("&Copy Report", self._copy),
        ):
            button = wx.Button(self, label=label)

            def on_button(_event: wx.CommandEvent, selected: Callable[[], object] = action) -> None:
                selected()

            button.Bind(wx.EVT_BUTTON, on_button)
            buttons.Add(button, 0, wx.RIGHT, self.FromDIP(PAD))
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "Close"))
        sizer.Add(buttons, 0, wx.ALL, pad)
        self.SetSizerAndFit(sizer)
        self.summary.SetFocus()

    def _body(self) -> str:
        return bug_report_body(
            self.summary.GetValue().strip(),
            self.happened.GetValue().strip(),
            self.expected.GetValue().strip(),
            self.steps.GetValue().strip(),
            self._facts,
        )

    def _copy(self) -> bool:
        text = f"{self.summary.GetValue().strip()}\n\n{self._body()}"
        if not _copy_to_clipboard(text):
            announce("Error: Could not access clipboard")
            return False
        announce("Report copied")
        return True

    def _open_github(self) -> None:
        title = self.summary.GetValue().strip()
        if not title:
            announce("Type a summary first")
            self.summary.SetFocus()
            return
        self._copy()
        query = urllib.parse.urlencode({"title": title, "body": self._body()[:6000]})
        wx.LaunchDefaultBrowser(f"https://github.com/{GITHUB_REPOSITORY}/issues/new?{query}")
        self.EndModal(wx.ID_OK)
