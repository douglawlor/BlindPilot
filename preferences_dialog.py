"""Every setting that lives in the Options menu, in one dialog.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

The menu is the source of truth on every platform; this dialog only
collects the choices from the live SETTINGS in app_config, and the
frame applies them through the same switches the menu items use, so
the two can never disagree. Nothing is changed here.
"""

from __future__ import annotations

import wx

from app_config import (
    APPEARANCE_RESTART_NOTE,
    APPEARANCES,
    CUE_LOOP,
    CUE_OFF,
    CUE_PERIODIC,
    CUE_SECONDS_MAX,
    CUE_SECONDS_MIN,
    NARRATION_EVERYTHING,
    NARRATION_KEEP_UP,
    NARRATION_MODES,
    SETTINGS,
    SOUND_CUES,
    _load_config,
    _valid_cue_seconds,
    _valid_model_refresh_minutes,
)

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12

MODEL_REFRESH_MINUTES_MIN = 0


MODEL_REFRESH_MINUTES_MAX = 240


class PreferencesDialog(wx.Dialog):
    """Every setting that lives in the Options menu, in one dialog.

    The menu is the source of truth on every platform; this is the same state
    as a form, reachable on macOS from the application menu (Preferences…,
    Cmd+,), which wxWidgets automatically moves a wx.ID_PREFERENCES item to.
    Nothing is changed here -- the dialog only collects the choices, and the
    frame applies them through the same switches the menu items use, so the
    two can never disagree.
    """

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, title="BlindPilot Preferences")
        panel = wx.Panel(self)
        pad = self.FromDIP(PAD_DIALOG)
        root = wx.BoxSizer(wx.VERTICAL)

        narration_choices = [label.replace("&", "") for _key, label, _help in NARRATION_MODES]
        self._narration_box = wx.RadioBox(
            panel,
            label="Narration",
            choices=narration_choices,
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self._narration_box.SetSelection(0 if SETTINGS.narration == NARRATION_EVERYTHING else 1)
        root.Add(self._narration_box, 0, wx.EXPAND | wx.ALL, pad)

        self._live_rows = wx.CheckBox(panel, label="Show live activity in the list")
        self._live_rows.SetValue(SETTINGS.live_rows)
        self._speak_live = wx.CheckBox(panel, label="Speak activity aloud")
        self._speak_live.SetValue(SETTINGS.speak_live)
        self._thinking = wx.CheckBox(panel, label="Include the backend's reasoning")
        self._thinking.SetValue(SETTINGS.show_thinking)
        self._text_view = wx.CheckBox(panel, label="Responses as a read-only text field")
        self._text_view.SetValue(SETTINGS.text_view)
        self._written_questions = wx.CheckBox(
            panel, label="Ask me questions a turn wrote into its answer"
        )
        self._written_questions.SetValue(SETTINGS.ask_written_questions)
        self._notify = wx.CheckBox(
            panel, label="Notify me when a turn ends while BlindPilot is in the background"
        )
        self._notify.SetValue(SETTINGS.notify_in_background)
        self._read_back = wx.CheckBox(panel, label="Read my message back when it is sent")
        self._read_back.SetValue(SETTINGS.read_back_sent)
        for check in (
            self._live_rows,
            self._speak_live,
            self._thinking,
            self._text_view,
            self._written_questions,
            self._notify,
            self._read_back,
        ):
            root.Add(check, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)

        root.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, pad)
        self._sounds = wx.CheckBox(panel, label="Play sound cues")
        self._sounds.SetValue(SETTINGS.sounds_enabled)
        root.Add(self._sounds, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        # The cues sit one dialog margin further in than the box that turns
        # them all on, and the same distance apart as the boxes above them.
        cues_box = wx.BoxSizer(wx.VERTICAL)
        self._cue_checks: dict[str, wx.CheckBox] = {}
        for cue, label, _help in SOUND_CUES:
            check = wx.CheckBox(panel, label=label.replace("&", ""))
            check.SetValue(SETTINGS.sound_cues.get(cue, True))
            self._cue_checks[cue] = check
            cues_box.Add(check, 0, wx.LEFT | wx.TOP, pad)
        root.Add(cues_box, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        self._sounds.Bind(wx.EVT_CHECKBOX, lambda _e: self._sync_sound_checks())
        self._sync_sound_checks()

        root.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, pad)
        # The group box already says "Working sound"; the choices do not
        # repeat it.
        cue_choices = [
            "Continuous",
            f"Every N seconds ({CUE_SECONDS_MIN}-{CUE_SECONDS_MAX})",
            "Off",
        ]
        self._cue_box = wx.RadioBox(
            panel,
            label="Working sound",
            choices=cue_choices,
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self._cue_box.SetSelection(
            {
                CUE_LOOP: 0,
                CUE_PERIODIC: 1,
                CUE_OFF: 2,
            }[SETTINGS.progress_cue]
        )
        root.Add(self._cue_box, 0, wx.EXPAND | wx.ALL, pad)
        interval_row = wx.BoxSizer(wx.HORIZONTAL)
        interval_row.Add(
            wx.StaticText(panel, label="Seconds between working sounds:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            self.FromDIP(PAD),
        )
        self._interval = wx.SpinCtrl(
            panel,
            min=CUE_SECONDS_MIN,
            max=CUE_SECONDS_MAX,
            initial=SETTINGS.progress_cue_seconds,
        )
        interval_row.Add(self._interval, 0)
        root.Add(interval_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        self._cue_box.Bind(wx.EVT_RADIOBOX, lambda _e: self._sync_interval())
        self._sync_interval()

        root.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, pad)
        self._appearance_box = wx.RadioBox(
            panel,
            label="Appearance",
            choices=[label for _key, label in APPEARANCES],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self._appearance_box.SetSelection(
            [key for key, _label in APPEARANCES].index(SETTINGS.appearance)
        )
        # wxWidgets applies the appearance before the first window exists and
        # cannot change it afterwards, so the choice waits for the next start.
        # Said under the box and in its tooltip, so it is read either way.
        self._appearance_box.SetToolTip(APPEARANCE_RESTART_NOTE)
        root.Add(self._appearance_box, 0, wx.EXPAND | wx.ALL, pad)
        root.Add(
            wx.StaticText(panel, label=APPEARANCE_RESTART_NOTE),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            pad,
        )

        root.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, pad)
        refresh_row = wx.BoxSizer(wx.HORIZONTAL)
        refresh_row.Add(
            wx.StaticText(
                panel,
                label=(
                    "Re-read the model list every N minutes "
                    f"({MODEL_REFRESH_MINUTES_MIN} turns it off):"
                ),
            ),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            self.FromDIP(PAD),
        )
        self._model_refresh = wx.SpinCtrl(
            panel,
            min=MODEL_REFRESH_MINUTES_MIN,
            max=MODEL_REFRESH_MINUTES_MAX,
            initial=SETTINGS.model_refresh_minutes,
        )
        self._model_refresh.SetToolTip(
            "BlindPilot re-reads each open tab's model list so a model retired "
            "upstream drops out of the picker, and says so. Each read starts the "
            "provider's command-line tool; 0 stops the background read."
        )
        refresh_row.Add(self._model_refresh, 0)
        root.Add(refresh_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        root.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, pad)
        self._updates = wx.CheckBox(panel, label="Check for updates at startup")
        self._updates.SetValue(bool(_load_config().get("check_for_updates_at_startup", True)))
        root.Add(self._updates, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        # The buttons must be born on the panel: the sizer lives on the panel,
        # so the widgets it manages have to be children of it, and a standard
        # button sizer places them the way the platform expects (on macOS, OK
        # on the right). Enter closes-without-applying rather than firing OK,
        # because this dialog is reached from a keyboard and the choices are
        # not made yet.
        buttons = wx.StdDialogButtonSizer()
        ok_button = wx.Button(panel, wx.ID_OK, "OK")
        cancel_button = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        cancel_button.SetDefault()
        buttons.AddButton(ok_button)
        buttons.AddButton(cancel_button)
        buttons.Realize()
        root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        panel.SetSizer(root)
        root.Fit(self)
        self.CentreOnParent()

    def _sync_sound_checks(self) -> None:
        enabled = self._sounds.GetValue()
        for check in self._cue_checks.values():
            check.Enable(enabled)

    def _sync_interval(self) -> None:
        self._interval.Enable(self._cue_box.GetSelection() == 1)

    def narration_selection(self) -> str:
        return (
            NARRATION_EVERYTHING if self._narration_box.GetSelection() == 0 else NARRATION_KEEP_UP
        )

    @property
    def live_rows(self) -> bool:
        return self._live_rows.GetValue()

    @property
    def speak_live(self) -> bool:
        return self._speak_live.GetValue()

    @property
    def show_thinking(self) -> bool:
        return self._thinking.GetValue()

    @property
    def text_view(self) -> bool:
        return self._text_view.GetValue()

    @property
    def ask_written_questions(self) -> bool:
        return self._written_questions.GetValue()

    @property
    def notify_in_background(self) -> bool:
        return self._notify.GetValue()

    @property
    def read_back_sent(self) -> bool:
        return self._read_back.GetValue()

    @property
    def sounds_enabled(self) -> bool:
        return self._sounds.GetValue()

    @property
    def sound_cues(self) -> dict[str, bool]:
        return {cue: check.GetValue() for cue, check in self._cue_checks.items()}

    @property
    def progress_cue(self) -> str:
        return (CUE_LOOP, CUE_PERIODIC, CUE_OFF)[self._cue_box.GetSelection()]

    @property
    def progress_interval(self) -> int:
        return _valid_cue_seconds(self._interval.GetValue())

    @property
    def model_refresh_minutes(self) -> int:
        return _valid_model_refresh_minutes(self._model_refresh.GetValue())

    @property
    def check_updates_startup(self) -> bool:
        return self._updates.GetValue()

    @property
    def appearance(self) -> str:
        return APPEARANCES[self._appearance_box.GetSelection()][0]
