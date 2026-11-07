"""The model picker for /model: one box for the model, one for effort.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the picker's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Both lists come from the probe that ran just before this opened. The
first entry in each box is "leave it alone" and says what that
currently means; picking it passes nothing at all. An alias is shown
with the version it resolves to and mapped back to the bare alias
when picked. Esc cancels, and focus starts in the model box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import wx

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12

DEFAULT_CHOICE = "(CLI default)"


AUTO_MODEL = "<BlindPilot auto>"


AUTO_MODEL_CHOICE = "Auto (choose for each task)"


def _plain(text: str) -> str:
    """Text for a label or a list entry, with markdown backticks taken out."""
    return text.replace("`", "")


@dataclass
class ModelOptions:
    """What a backend reports it can be asked for, plus what it is using now."""

    models: List[str]
    efforts: List[str]
    current_model: str = ""  # display name, e.g. "Opus 5"
    current_effort: str = ""  # e.g. "medium"
    error: str = ""  # non-empty when the probe fell back to defaults
    from_cache: bool = False  # served from a recent probe, not a fresh one
    # alias -> the model it currently resolves to, e.g. "opus" -> "Opus 5.5".
    labels: dict[str, str] = field(default_factory=dict)


def _keep_choice(current: str) -> str:
    """First combo-box entry: pass no flag, and say what that currently means."""
    current = _plain(current)
    return f"{DEFAULT_CHOICE}, currently {current}" if current else DEFAULT_CHOICE


def _model_choice(model: str, labels: dict[str, str]) -> str:
    """A model-box entry: the alias, plus the version it means when known."""
    label = labels.get(model, "")
    return f"{model}: {label}" if label and label.lower() != model.lower() else model


class ModelDialog(wx.Dialog):
    """Model picker for /model: one combo box for the model, one for effort.

    Both lists come from the probe that ran just before this opened, so the
    choices are whatever the selected backend actually accepts. The first entry
    in each box names what that backend is using right now -- picking it passes
    nothing at all, so it keeps that. The model box is editable so a full model
    ID can be typed; the effort box is a fixed list. Esc cancels.

    A backend that takes no effort level gets an empty list for it, so the box
    offers only "leave it alone" rather than a control the turn would ignore.
    """

    def __init__(
        self,
        parent: wx.Window,
        options: "ModelOptions",
        selected_model: str,
        selected_effort: str,
        backend_name: str = "CLI",
        allow_auto_model: bool = False,
    ):
        super().__init__(parent, title="Model")

        current = _plain(options.current_model) or "unknown"
        if options.current_effort:
            current = f"{current}, effort {_plain(options.current_effort)}"
        lines = [f"{backend_name} reports the current model as: {current}."]
        if selected_model or selected_effort:
            lines.append(
                "This tab overrides that with: "
                f"model {'automatic routing' if selected_model == AUTO_MODEL else selected_model or 'unchanged'}, "
                f"effort {selected_effort or 'unchanged'}."
            )
        if options.error:
            lines.append(options.error)
        if allow_auto_model:
            lines.append("Auto may switch providers, including free models, for each task.")
        summary = wx.StaticText(self, label="\n".join(lines))
        summary.Wrap(self.FromDIP(520))

        # "Leave it alone" is the first entry in both boxes, and it says what
        # leaving it alone actually means rather than just "(CLI default)".
        self._model_keep = _keep_choice(options.current_model)
        # Each alias is shown with the version it resolves to, "opus: Opus 5.5",
        # and mapped back to the bare alias when picked.
        self._model_by_choice = {_model_choice(m, options.labels): m for m in options.models}
        if allow_auto_model:
            self._model_by_choice[AUTO_MODEL_CHOICE] = AUTO_MODEL
        self._effort_keep = _keep_choice(options.current_effort)

        model_label = wx.StaticText(self, label="&Model:")
        self.model_box = wx.ComboBox(
            self,
            choices=[self._model_keep, *self._model_by_choice],
            style=wx.CB_DROPDOWN,
        )
        self.model_box.SetName("Model")
        self.model_box.SetValue(
            (
                AUTO_MODEL_CHOICE
                if selected_model == AUTO_MODEL and allow_auto_model
                else _model_choice(selected_model, options.labels)
                if selected_model
                else self._model_keep
            )
        )

        efforts = list(options.efforts)
        if selected_effort and selected_effort not in efforts:
            # Kept as a choice. A read-only box cannot show a value it does not
            # list, so OK would have silently cleared this tab's override.
            efforts.append(selected_effort)
        effort_label = wx.StaticText(self, label="&Effort:")
        self.effort_box = wx.ComboBox(
            self,
            choices=[self._effort_keep, *efforts],
            style=wx.CB_DROPDOWN | wx.CB_READONLY,
        )
        self.effort_box.SetName("Effort")
        self.effort_box.SetStringSelection(selected_effort or self._effort_keep)

        grid = wx.FlexGridSizer(2, 2, self.FromDIP(PAD), self.FromDIP(PAD))
        grid.AddGrowableCol(1, 1)
        grid.Add(model_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.model_box, 1, wx.EXPAND)
        grid.Add(effort_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.effort_box, 1, wx.EXPAND)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(summary, 0, wx.ALL, pad)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.model_box.SetFocus()
        self.CentreOnParent()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()

    def selection(self) -> tuple[str, str]:
        """(model, effort), with "" for either one left as the backend has it."""
        model = self.model_box.GetValue().strip()
        model = self._model_by_choice.get(model, model)
        effort = self.effort_box.GetValue().strip()
        return (
            "" if model in (DEFAULT_CHOICE, self._model_keep) else model,
            "" if effort in (DEFAULT_CHOICE, self._effort_keep) else effort,
        )
