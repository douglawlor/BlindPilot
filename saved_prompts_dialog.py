"""Saved Prompts: messages sent often, kept by name.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Every change is saved at once, through app_config. Names the dialog
calls are patched here, where the calls are made from -- for example
``saved_prompts_dialog._load_config``.
"""

from __future__ import annotations

from collections.abc import Callable

import wx

from app_config import _load_config, _save_config
from screen_speech import announce

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so
# this module does not import the application module).
PAD_DIALOG = 12


def _saved_prompts() -> list[dict]:
    """The saved prompts, in the order the user put them: name and text each."""
    found = _load_config().get("saved_prompts")
    if not isinstance(found, list):
        return []
    return [
        {"name": str(p["name"]), "text": str(p["text"])}
        for p in found
        if isinstance(p, dict) and p.get("name") and isinstance(p.get("text"), str)
    ]


def _store_saved_prompts(prompts: list[dict]) -> bool:
    cfg = _load_config()
    cfg["saved_prompts"] = prompts
    return _save_config(cfg)


class SavedPromptsDialog(wx.Dialog):
    """Messages sent often, kept by name (Conversation, Saved Prompts, Ctrl+Shift+P).

    Use (or Enter on a prompt) closes the dialog with `chosen` set to its
    text. New starts from `draft`, what the prompt box holds, so saving what
    was just typed is New then OK. Every change is saved at once.
    """

    def __init__(self, parent: wx.Window, draft: str = ""):
        super().__init__(
            parent, title="Saved Prompts", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.prompts = _saved_prompts()
        self.draft = draft
        self.chosen = ""
        # Each label before its control: wxMSW names a control after the
        # static text created just before it.
        list_label = wx.StaticText(self, label="&Prompts:")
        self.list_box = wx.ListBox(self, name="Prompts")
        text_label = wx.StaticText(self, label="Te&xt:")
        self.text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2, name="Text"
        )
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.use_button = wx.Button(self, wx.ID_OK, "&Use")
        buttons.Add(self.use_button, 0, wx.RIGHT, self.FromDIP(4))
        for label, action in (
            ("&New…", self._new),
            ("&Edit…", self._edit),
            ("&Delete", self._delete),
            ("Mo&ve Up", lambda: self._move(-1)),
            ("Move Do&wn", lambda: self._move(1)),
        ):
            button = wx.Button(self, label=label)

            def on_button(_event: wx.CommandEvent, selected: Callable[[], None] = action) -> None:
                selected()

            button.Bind(wx.EVT_BUTTON, on_button)
            buttons.Add(button, 0, wx.RIGHT, self.FromDIP(4))
        close = wx.Button(self, wx.ID_CANCEL, "&Close")
        buttons.Add(close)

        pad = self.FromDIP(PAD_DIALOG)
        self.list_box.SetMinSize(self.FromDIP(wx.Size(480, 160)))
        self.text.SetMinSize(self.FromDIP(wx.Size(480, 120)))
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.list_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        sizer.Add(text_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        sizer.Add(buttons, 0, wx.ALL, pad)
        self.SetSizerAndFit(sizer)

        self.list_box.Bind(wx.EVT_LISTBOX, lambda _e: self._show_text())
        self.list_box.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._use())
        self.Bind(wx.EVT_BUTTON, lambda _e: self._use(), id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self._refresh(0)
        self.list_box.SetFocus()
        self.CentreOnParent()

    def _refresh(self, selection: int) -> None:
        self.list_box.Set([p["name"] for p in self.prompts])
        if self.prompts:
            self.list_box.SetSelection(max(0, min(selection, len(self.prompts) - 1)))
        self.use_button.Enable(bool(self.prompts))
        self._show_text()

    def _current(self) -> int:
        index = self.list_box.GetSelection()
        return index if 0 <= index < len(self.prompts) else -1

    def _show_text(self) -> None:
        index = self._current()
        self.text.ChangeValue(self.prompts[index]["text"] if index >= 0 else "")

    def _save(self, said: str, selection: int) -> None:
        if _store_saved_prompts(self.prompts):
            announce(said)
        else:
            announce(
                "Saved prompts could not be written. Check that the settings folder is writable."
            )
        self._refresh(selection)
        self.list_box.SetFocus()

    def _ask(self, name: str, text: str) -> dict | None:
        """Name, then text. None if either was cancelled or left blank."""
        with wx.TextEntryDialog(self, "Prompt name:", "Saved Prompt", name) as dlg:
            if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue().strip():
                return None
            name = dlg.GetValue().strip()
        with wx.TextEntryDialog(
            self,
            "Prompt text. Enter starts a new line; Tab to OK to save.",
            "Saved Prompt",
            text,
            style=wx.TextEntryDialogStyle | wx.TE_MULTILINE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue().strip():
                return None
            return {"name": name, "text": dlg.GetValue()}

    def _new(self) -> None:
        draft = self.draft.strip()
        prompt = self._ask(" ".join(draft.split()[:5]), self.draft)
        if prompt:
            self.prompts.append(prompt)
            self._save(f"Prompt saved: {prompt['name']}", len(self.prompts) - 1)

    def _edit(self) -> None:
        index = self._current()
        if index < 0:
            return
        prompt = self._ask(self.prompts[index]["name"], self.prompts[index]["text"])
        if prompt:
            self.prompts[index] = prompt
            self._save(f"Prompt saved: {prompt['name']}", index)

    def _delete(self) -> None:
        index = self._current()
        if index < 0:
            return
        name = self.prompts[index]["name"]
        if (
            wx.MessageBox(
                f"Delete the prompt {name}?",
                "Delete prompt",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                self,
            )
            != wx.YES
        ):
            return
        del self.prompts[index]
        self._save(f"Prompt deleted: {name}", index)

    def _move(self, step: int) -> None:
        index = self._current()
        target = index + step
        if index < 0 or not 0 <= target < len(self.prompts):
            return
        self.prompts[index], self.prompts[target] = self.prompts[target], self.prompts[index]
        self._save(f"Moved to {target + 1} of {len(self.prompts)}", target)

    def _use(self) -> None:
        index = self._current()
        if index < 0:
            return
        self.chosen = self.prompts[index]["text"]
        self.EndModal(wx.ID_OK)

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if self.FindFocus() is self.list_box:
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                self._use()
                return
            if key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
                self._delete()
                return
        event.Skip()
