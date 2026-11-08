"""Where each backend keeps its settings, and a way in to them.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

BlindPilot opens these files; it does not change, create, or write
them. Every file is listed whether or not it exists, because half of
what this answers is "where would that even be". Names the dialog
calls are patched here, where the calls are made from -- for example
``settings_files_dialog._open_path``.
"""

from __future__ import annotations


import wx

from agent_backends import backend_label, settings_files
from read_view import _open_path
from screen_speech import announce
from view_dialogs import WrappedText

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12


def _settings_label(entry) -> str:
    """One row of the settings list, as the screen reader will read it.

    Everything is in the label because the label is what gets read on arrow.
    The scope and the note are not decoration: the two project-level Claude
    Code files differ only in whether the repository carries them, and opening
    the wrong one is how somebody's own settings end up committed to a
    repository that is not theirs.
    """
    state = "present" if entry.exists else "not created yet"
    return f"{backend_label(entry.backend)}, {entry.scope}. {entry.note} ({state})"


def _reach_settings_file(entry) -> str:
    """Open one, and say what happened. Never writes and never creates.

    A settings file belongs to the CLI that reads it, and every one of them
    writes its own on first run. One invented here, at a path BlindPilot chose,
    would do nothing while looking like it did.
    """
    if entry.exists:
        if _open_path(entry.path):
            return f"Opened {entry.path.name}"
        return f"Could not open {entry.path}. Open it yourself to edit it."
    folder = entry.path.parent
    if folder.is_dir() and _open_path(folder):
        return (
            f"{entry.path.name} does not exist yet. "
            f"{backend_label(entry.backend)} writes it itself. "
            "Opened the folder it belongs in."
        )
    return f"{entry.path.name} does not exist yet. It would be at {entry.path}."


class SettingsFilesDialog(wx.Dialog):
    """Where each backend keeps its settings, and a way in to them.

    BlindPilot opens these; it does not change them. That is the whole feature.
    The problem was never that editing text is hard - it is that these are
    dotfiles in directories nothing announces, so reaching one meant leaving
    the application already knowing where to look. `open_log_folder` exists on
    the same reasoning: reading a path out loud and leaving somebody to
    navigate to it is not a way in.

    Editing them here was considered and rejected. Claude Code's is over three
    hundred lines of nested JSON and Codex's over two hundred lines of TOML; a
    text box holding either, navigated by ear, is worse than the editor
    somebody already has, and a stray comma written back breaks the CLI
    silently until it next refuses to start.

    Every file is listed whether or not it exists, because half of what this
    answers is "where would that even be".
    """

    def __init__(self, parent: wx.Window | None, cwd: str | None):
        super().__init__(
            parent,
            title="Backend Settings",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._entries = list(settings_files(cwd))
        pad = self.FromDIP(PAD_DIALOG)
        outer = wx.BoxSizer(wx.VERTICAL)
        self._intro = WrappedText(
            self,
            "The files each coding agent reads its settings from. "
            "BlindPilot opens them in your editor; it never changes them.",
        )
        self._intro.Wrap(self.FromDIP(720))
        outer.Add(self._intro, 0, wx.ALL, pad)
        outer.Add(wx.StaticText(self, label="Settings &files:"), 0, wx.LEFT | wx.RIGHT, pad)
        self.list_box = wx.ListBox(
            self,
            choices=[_settings_label(entry) for entry in self._entries],
            style=wx.LB_SINGLE,
        )
        self.list_box.SetMinSize(self.FromDIP(wx.Size(720, 260)))
        if self._entries:
            self.list_box.SetSelection(0)
        outer.Add(self.list_box, 1, wx.EXPAND | wx.ALL, pad)
        self.status = wx.StaticText(self, label="")
        outer.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        open_button = wx.Button(self, wx.ID_ANY, "&Open")
        buttons.Add(open_button, 0, wx.RIGHT, self.FromDIP(PAD))
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "&Close"), 0)
        outer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, pad)
        self.SetSizerAndFit(outer)

        self.Bind(wx.EVT_BUTTON, lambda _event: self._open_selected(), open_button)
        self.Bind(wx.EVT_LISTBOX_DCLICK, lambda _event: self._open_selected(), self.list_box)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.list_box.SetFocus()
        self.CentreOnParent()

    def _on_size(self, event: wx.SizeEvent) -> None:
        """The dialog can be resized, so the intro follows its width."""
        event.Skip()
        width = self.GetClientSize().width - 2 * self.FromDIP(PAD_DIALOG)
        if width > 0:
            self._intro.Wrap(width)

    def _open_selected(self) -> None:
        index = self.list_box.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._entries):
            announce("Choose a settings file first")
            return
        said = _reach_settings_file(self._entries[index])
        announce(said)
        self.status.SetLabel(said)

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            # CHAR_HOOK fires before the focused control sees the key, so Enter
            # on Close has to reach Close. The same mistake as the
            # past-conversations dialog, not repeated here.
            if isinstance(self.FindFocus(), wx.Button):
                event.Skip()
                return
            self._open_selected()
            return
        event.Skip()
