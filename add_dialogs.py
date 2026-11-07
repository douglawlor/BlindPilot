"""Add one memory, agent, or note: the three small entry dialogs.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
these dialogs' UI behaviour can be changed and reviewed
independently. Assistive-technology verification of these dialogs
is covered by the automated behaviour tests; per issue #136's
agreed exception, no real screen-reader verification is represented
as completed for this extraction.

Every field is named for the screen reader from its label, a new
memory defaults to the project type, and focus starts in the first
field of each dialog.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import wx

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so
# this module does not import the application module).
PAD_DIALOG = 12

MEMORY_TYPES = ("user", "feedback", "project", "reference")


class AddMemoryDialog(wx.Dialog):
    """Name, description, type, folder and text of a new Claude Code memory."""

    def __init__(self, parent: wx.Window, folders: list[Path]):
        super().__init__(
            parent, title="Add Memory", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        grid = wx.FlexGridSizer(cols=2, vgap=self.FromDIP(6), hgap=self.FromDIP(8))
        grid.AddGrowableCol(1)

        def field(label: str, control: wx.Window) -> wx.Window:
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control.SetName(label.replace("&", "").rstrip(":"))
            grid.Add(control, 1, wx.EXPAND)
            return control

        self.name = cast(wx.TextCtrl, field("&Name:", wx.TextCtrl(self)))
        self.description = cast(wx.TextCtrl, field("&Description, one line:", wx.TextCtrl(self)))
        self.kind = cast(wx.Choice, field("&Type:", wx.Choice(self, choices=list(MEMORY_TYPES))))
        self.kind.SetSelection(MEMORY_TYPES.index("project"))
        self.folders = folders
        self.folder = cast(
            wx.Choice, field("&Folder:", wx.Choice(self, choices=[str(f) for f in folders]))
        )
        self.folder.SetSelection(0)
        self.body = cast(wx.TextCtrl, field("&Memory:", wx.TextCtrl(self, style=wx.TE_MULTILINE)))
        self.body.SetMinSize(self.FromDIP(wx.Size(480, 160)))
        grid.AddGrowableRow(4)

        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)
        self.name.SetFocus()
        self.CentreOnParent()

    def values(self) -> tuple[Path, str, str, str, str]:
        return (
            self.folders[self.folder.GetSelection()],
            self.name.GetValue().strip(),
            self.description.GetValue().strip(),
            MEMORY_TYPES[self.kind.GetSelection()],
            self.body.GetValue(),
        )


class AddAgentDialog(wx.Dialog):
    """Name, description and instructions for one new backend agent."""

    def __init__(self, parent: wx.Window, backend: str):
        super().__init__(
            parent, title=f"Add {backend} Agent", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        grid = wx.FlexGridSizer(cols=2, vgap=self.FromDIP(6), hgap=self.FromDIP(8))
        grid.AddGrowableCol(1)

        def field(label: str, control: wx.TextCtrl) -> wx.TextCtrl:
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control.SetName(label.replace("&", "").rstrip(":"))
            grid.Add(control, 1, wx.EXPAND)
            return control

        self.name = field("&Name (letters, numbers, hyphens, underscores):", wx.TextCtrl(self))
        self.description = field("&Description, one line:", wx.TextCtrl(self))
        self.instructions = field("&Instructions:", wx.TextCtrl(self, style=wx.TE_MULTILINE))
        self.instructions.SetMinSize(self.FromDIP(wx.Size(480, 160)))
        grid.AddGrowableRow(2)

        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)
        self.name.SetFocus()
        self.CentreOnParent()


class AddNoteDialog(wx.Dialog):
    """A Command Code taste or a Hermes memory entry: where it goes, and its text.

    `editable` lets a new category be typed into the place box.
    """

    def __init__(
        self, parent: wx.Window, title: str, place: str, places: list[str], editable: bool
    ):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        style = wx.CB_DROPDOWN if editable else wx.CB_READONLY
        self.place = wx.ComboBox(self, choices=places, style=style)
        self.place.SetName(place.replace("&", "").rstrip(":"))
        if places:
            self.place.SetSelection(0)
        self.text = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        self.text.SetName("Text")
        self.text.SetMinSize(self.FromDIP(wx.Size(480, 140)))
        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=place), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.place, 0, wx.EXPAND | wx.ALL, pad)
        sizer.Add(wx.StaticText(self, label="&Text:"), 0, wx.LEFT | wx.RIGHT, pad)
        sizer.Add(self.text, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)
        self.text.SetFocus()
        self.CentreOnParent()
