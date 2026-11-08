"""First run: set BlindPilot up, one backend at a time.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the wizard's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of the wizard is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Installing, signing in and checking all happen off the UI thread,
and the sign-in itself is run by backend_login where the user
cannot see it. Names the wizard calls are patched here, where the
calls are made from -- for example ``setup_wizard._find_npm``.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

import wx

from agent_backends import (
    BACKENDS,
    BACKEND_CLAUDE,
    BACKEND_GEMINI,
    BACKEND_HERMES,
    BACKEND_IDS,
    BACKEND_LABELS,
    BACKEND_MUSE,
    BACKEND_OPENCODE,
    backend_auth_ok,
    backend_label,
    blindpilot_data_dir,
    end_hidden_terminal,
    find_backend_cli,
    normalize_backend,
    spawn_hidden_terminal,
)
from backend_login import BackendLogin
from backend_updates import BackendUpdateResult, BackendUpdateService, BackendUpdateState
from cli_install import (
    POSIX_INSTALL_SH_URL,
    WINDOWS_INSTALL_PS1_URL,
    _automatic_npm_install_available,
    _backend_installs_with_npm,
    _find_claude,
    _find_npm,
    _hermes_install_argv,
    _hermes_missing_prereq_message,
    _install_argv,
    _install_failure_message,
    _is_on_persistent_path,
    _missing_prereq_message,
    _muse_install_argv,
    _muse_missing_prereq_message,
    _npm_install_argv,
    _path_shells,
    ensure_on_path,
    install_backend,
    update_backend,
)
from connect_dialog import ConnectDialog
from dialog_tasks import _call_after_alive
from model_dialog import invalidate_model_options
from muse_backend import muse_command
from process_utils import no_window_kwargs
from read_view import _monospace_font
from screen_speech import announce
from view_dialogs import WrappedText

# How long the pre-sign-in reset is given. It deletes a file and, at most, tells
# the server the old key is gone; it never waits for a person.
_LOGIN_RESET_TIMEOUT = 30.0

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12

# How long a hidden sign-in is given before the wizard stops waiting on it.
# The person has to finish in a browser, so this is generous; running out is
# not itself a failure, because the CLI is asked afterwards whether the
# sign-in landed either way.
_HIDDEN_LOGIN_TIMEOUT = 300.0


# How long, and how often, the wizard asks a terminal sign-in whether it has
# landed (see SetupWizard._watch_for_sign_in). Ten minutes covers a browser
# sign-in with two-factor; five seconds keeps agy's `models` check infrequent.
_SIGN_IN_WATCH_SECONDS = 600.0


_SIGN_IN_WATCH_INTERVAL = 5.0


def _chord(chord: str) -> str:
    """Spell a chord the way this platform reads it.

    A note written in parentheses is literal text: wxWidgets renders it as
    written, and a screen reader reads it as written, so on macOS it must say
    Cmd rather than Ctrl. Tabular accelerators (the ``\tCtrl+...`` form) are
    deliberately left alone: wxWidgets' Mac port converts those itself, both
    on screen and in what it recognises as the Command key.
    """
    return chord.replace("Ctrl", "Cmd") if platform.system() == "Darwin" else chord


def _clear_stale_login(binary: str, reset_args: tuple[str, ...]) -> None:
    """Run a backend's pre-sign-in reset, if it has one.

    Command Code keeps its credential file until `logout` deletes it, and
    `login` refuses to start while one exists ("Already logged in"), so a key
    the server has revoked could never be replaced from the wizard. Its own
    troubleshooting says `logout` then `login`. Failure is not reported: the
    sign-in that follows is what says whether it worked.
    """
    if not reset_args:
        return
    try:
        subprocess.run(
            [binary, *reset_args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_LOGIN_RESET_TIMEOUT,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminal_running(terminal: object) -> bool:
    """Whether the command in a hidden terminal is still running.

    _ConPtyTerminal and pexpect both answer `isalive`. A handle that cannot say is
    treated as finished, so the wizard settles for asking the CLI whether the
    sign-in landed rather than waiting forever on a terminal it cannot see.
    """
    alive = getattr(terminal, "isalive", None)
    if alive is None:
        return False
    try:
        return bool(alive())
    except Exception:
        return False


class SetupWizard(wx.Dialog):
    """Choose, install, and authenticate a BlindPilot backend."""

    _STEPS = ["Welcome", "Coding Agent CLI", "Sign In", "Projects Folder", "All Done"]

    def __init__(
        self,
        parent: Optional[wx.Window],
        initial_projects_folder: Optional[str] = None,
        initial_backend: str = BACKEND_CLAUDE,
    ):
        super().__init__(
            parent,
            title="BlindPilot — Setup",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        # Sized before the pages are built, so their text wraps to this width.
        self.SetSize(self.FromDIP(wx.Size(580, 400)))
        self._paragraphs: list[WrappedText] = []
        self.projects_folder: Optional[str] = initial_projects_folder
        self.backend = normalize_backend(initial_backend)
        self._step = 0
        self._backend_path: Optional[str] = None
        self._update_check_generation = 0
        self._backend_update_service = BackendUpdateService(
            cache_path=blindpilot_data_dir() / "backend-update-cache.json"
        )
        self._login: Optional[BackendLogin] = None
        # A sign-in running in a terminal the user never sees, kept so backing
        # out of the wizard stops it rather than leaving it to finish unseen.
        self._hidden_login: Optional[object] = None
        self._code_dialog: Optional[wx.TextEntryDialog] = None

        self._step_label = wx.StaticText(self, label="")
        f = self._step_label.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        f.SetPointSize(f.GetPointSize() + 2)
        self._step_label.SetFont(f)

        self._book = wx.Simplebook(self)
        self._pages = [
            self._make_welcome(),
            self._make_cli(),
            self._make_signin(),
            self._make_projects(),
            self._make_done(),
        ]
        for page in self._pages:
            self._book.AddPage(page, "")
        self._refresh_backend_copy()

        self._back_btn = wx.Button(self, label="Back")
        self._next_btn = wx.Button(self, label="Next")
        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, "Cancel")
        self._back_btn.Bind(wx.EVT_BUTTON, lambda _e: self._go(-1))
        self._next_btn.Bind(wx.EVT_BUTTON, lambda _e: self._go(+1))

        pad = self.FromDIP(PAD_DIALOG)
        nav = wx.BoxSizer(wx.HORIZONTAL)
        nav.Add(self._cancel_btn, 0)
        nav.AddStretchSpacer()
        nav.Add(self._back_btn, 0, wx.RIGHT, self.FromDIP(PAD))
        nav.Add(self._next_btn, 0)

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._step_label, 0, wx.ALL, pad)
        root.Add(self._book, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        root.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.TOP, self.FromDIP(PAD))
        root.Add(nav, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizer(root)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, self._on_wizard_close)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self._show_step(0)
        self.CentreOnParent()

    # ---- text that follows the dialog's width ----

    def _wrap_width(self) -> int:
        """How wide a paragraph on a page may be: the dialog less its margins."""
        width = self.GetClientSize().width - 2 * self.FromDIP(PAD_DIALOG + PAD)
        return width if width > self.FromDIP(200) else self.FromDIP(520)

    def _paragraph(self, parent: wx.Window, label: str = "") -> WrappedText:
        """A page paragraph, wrapped to the dialog and rewrapped as it resizes."""
        text = WrappedText(parent, label)
        text.Wrap(self._wrap_width())
        self._paragraphs.append(text)
        return text

    def _on_size(self, event: wx.SizeEvent) -> None:
        event.Skip()
        width = self._wrap_width()
        for text in self._paragraphs:
            text.Wrap(width)
        for page in self._pages:
            page.Layout()

    # ---- page builders ----

    def _make_welcome(self) -> wx.Panel:
        p = wx.Panel(self._book)
        # Every label the backend changes is set by _refresh_backend_copy.
        self._welcome_text = self._paragraph(p)
        backend_label_widget = wx.StaticText(p, label="&Backend:")
        self._setup_backend_picker = wx.Choice(
            p, choices=[BACKEND_LABELS[value] for value in BACKEND_IDS]
        )
        self._setup_backend_picker.SetName("Backend")
        self._setup_backend_picker.SetSelection(BACKEND_IDS.index(self.backend))
        self._setup_backend_picker.Bind(wx.EVT_CHOICE, self._on_backend_choice)
        pad = self.FromDIP(PAD)
        picker_row = wx.BoxSizer(wx.HORIZONTAL)
        picker_row.Add(backend_label_widget, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        picker_row.Add(self._setup_backend_picker, 1)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._welcome_text, 0, wx.ALL, pad)
        s.Add(picker_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        p.SetSizer(s)
        return p

    def _make_cli(self) -> wx.Panel:
        p = wx.Panel(self._book)
        self._cli_status = self._paragraph(p, "Checking for Claude Code…")
        self._cli_detail = self._paragraph(p)

        self._cli_install_btn = wx.Button(p, label="Install backend")
        self._cli_install_btn.Bind(wx.EVT_BUTTON, lambda _e: self._install_cli())
        self._cli_install_btn.Hide()
        self._cli_update_btn = wx.Button(p, label="Update backend")
        self._cli_update_btn.Bind(wx.EVT_BUTTON, lambda _e: self._update_cli())
        self._cli_update_btn.Hide()
        self._cli_path_btn = wx.Button(p, label="Add to PATH")
        self._cli_path_btn.Bind(wx.EVT_BUTTON, lambda _e: self._repair_path())
        self._cli_path_btn.Hide()
        self._cli_check_btn = wx.Button(p, label="Check Again")
        self._cli_check_btn.Bind(wx.EVT_BUTTON, lambda _e: self._check_cli(force_update_check=True))
        self._cli_check_btn.Hide()
        self._cli_check_pending = False

        # Read-only multiline field rather than a label: NVDA can review the
        # installer's output line by line, and it stays reachable by Tab.
        self._cli_log = wx.TextCtrl(
            p,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self._cli_log.SetName("Installer output")
        # Installer output is columns of text; a fixed-width face keeps them.
        self._cli_log.SetFont(_monospace_font(self._cli_log))
        self._cli_log.Hide()

        pad = self.FromDIP(PAD)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(self._cli_install_btn, 0, wx.RIGHT, pad)
        btns.Add(self._cli_update_btn, 0, wx.RIGHT, pad)
        btns.Add(self._cli_path_btn, 0, wx.RIGHT, pad)
        btns.Add(self._cli_check_btn, 0)

        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._cli_status, 0, wx.ALL, pad)
        s.Add(self._cli_detail, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        s.Add(btns, 0, wx.LEFT | wx.BOTTOM, pad)
        s.Add(self._cli_log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        p.SetSizer(s)
        return p

    def _make_signin(self) -> wx.Panel:
        p = wx.Panel(self._book)
        self._signin_intro = self._paragraph(p)
        self._signin_status = self._paragraph(p)
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._signin_btn = wx.Button(p, label="Sign In")
        self._signin_btn.Bind(wx.EVT_BUTTON, lambda _e: self._do_login())
        self._already_btn = wx.Button(p, label="Already Signed In")
        self._already_btn.Bind(wx.EVT_BUTTON, lambda _e: self._go(+1))
        # The CLI opens the browser for some backends and refuses to for
        # others, and a browser closed by accident used to mean starting the
        # whole sign-in again. This reopens the address the CLI gave, whoever
        # was meant to open it the first time.
        self._open_page_btn = wx.Button(p, label="Open Sign-in Page")
        self._open_page_btn.Bind(wx.EVT_BUTTON, lambda _e: self._open_sign_in_page())
        self._open_page_btn.Disable()
        pad = self.FromDIP(PAD)
        btn_row.Add(self._signin_btn, 0, wx.RIGHT, pad)
        btn_row.Add(self._already_btn, 0, wx.RIGHT, pad)
        btn_row.Add(self._open_page_btn, 0)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._signin_intro, 0, wx.ALL, pad)
        s.Add(self._signin_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        s.Add(btn_row, 0, wx.LEFT, pad)
        p.SetSizer(s)
        return p

    def _make_projects(self) -> wx.Panel:
        p = wx.Panel(self._book)
        intro = self._paragraph(
            p,
            "Choose the folder that holds your projects, if you have one. "
            "New Session browses from there.\n\n"
            "You can skip this and set it later under File, Set Projects Folder.",
        )
        self._proj_label = wx.StaticText(p, label=self._proj_display())
        choose_btn = wx.Button(p, label="Choose Folder…")
        choose_btn.Bind(wx.EVT_BUTTON, lambda _e: self._pick_folder())
        pad = self.FromDIP(PAD)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(intro, 0, wx.ALL, pad)
        s.Add(self._proj_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        s.Add(choose_btn, 0, wx.LEFT, pad)
        p.SetSizer(s)
        return p

    def _make_done(self) -> wx.Panel:
        p = wx.Panel(self._book)
        self._done_text = self._paragraph(p)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._done_text, 0, wx.ALL, self.FromDIP(PAD))
        p.SetSizer(s)
        return p

    def _refresh_backend_copy(self) -> None:
        """Update every wizard page for the backend chosen on Welcome."""
        info = BACKENDS[self.backend]
        label = info.label
        login = " ".join((info.executable, *info.login_args))
        self._signin_btn.SetLabel(
            "Connect a Provider" if self.backend == BACKEND_OPENCODE else "Sign In"
        )
        # opencode's sign-in runs through the Connect dialog, which opens the
        # provider's page itself; there is no CLI address for this button to
        # reopen, so it is not offered.
        # Gemini CLI and agy open the browser themselves from their terminal,
        # so there is no address here to reopen and no button to tab past.
        self._open_page_btn.Show(
            self.backend != BACKEND_OPENCODE and not info.login_watch_until_signed_in
        )
        self._welcome_text.SetLabel(
            "Welcome to BlindPilot.\n\n"
            "Choose a backend. This wizard checks that it is installed and signed "
            "in, and can set your projects folder.\n\n"
            "You can change backends later under Model, Backend."
        )
        self._cli_install_btn.SetLabel(f"Install {label}")
        self._cli_update_btn.SetLabel(f"Update {label}")
        if self.backend == BACKEND_OPENCODE:
            self._signin_intro.SetLabel(
                f"{label} reaches a model through a provider you connect it to.\n\n"
                "Choose Connect a Provider to pick one and give it an API key, or to "
                "sign in through your browser. If you have already connected one, or "
                f"already ran '{login}' in a terminal, choose Already Signed In."
            )
        elif info.login_watch_until_signed_in:
            self._signin_intro.SetLabel(
                f"Sign in to {label} with your Google account.\n\n"
                f"If you have already signed in to {label} in a terminal, choose "
                f"Already Signed In. Otherwise choose Sign In: {label} opens in a "
                "terminal window and starts its Google sign-in in your browser"
                + (
                    " (if it asks how to sign in, choose Sign in with Google)"
                    if self.backend == BACKEND_GEMINI
                    else ""
                )
                + ". When the browser says you are signed in, the wizard notices "
                "and moves on by itself; you can then close the terminal window."
                + (
                    "\n\nNote: since June 2026 Google refuses Gemini CLI turns from "
                    "a personal Google account (free or Google AI Pro) and points to "
                    "Antigravity CLI. If yours is one, choose Antigravity CLI instead, "
                    "or set up a Gemini API key in Gemini CLI itself."
                    if self.backend == BACKEND_GEMINI
                    else ""
                )
            )
        elif info.login_terminal_hidden:
            # Nothing opens for the user to read or answer, so what this says
            # is where the sign-in actually happens.
            self._signin_intro.SetLabel(
                f"Sign in to {label}.\n\n"
                f"If you have already run '{login}' in a terminal, choose Already "
                "Signed In. Otherwise choose Sign In: the sign-in page opens in "
                "your browser, and the wizard reports the result here."
            )
        elif info.login_needs_terminal and self.backend == BACKEND_HERMES:
            # Hermes' "login" is really its model picker, so its setup is about
            # choosing a provider and model rather than about an account.
            self._signin_intro.SetLabel(
                f"BlindPilot needs {label} to have a provider and model configured.\n\n"
                f"If you have already run '{login}' in a terminal, choose Already "
                "Signed In. Otherwise choose Sign In: its setup opens in a terminal "
                "window where you can answer its questions."
            )
        elif info.login_needs_terminal:
            self._signin_intro.SetLabel(
                f"Sign in to {label}.\n\n"
                f"If you have already run '{login}' in a terminal, choose Already "
                "Signed In. Otherwise choose Sign In: its setup opens in a terminal "
                "window where you can answer its questions."
            )
        else:
            self._signin_intro.SetLabel(
                f"Sign in to {label}.\n\n"
                f"If you already ran '{login}' in a terminal, choose Already Signed "
                "In. Otherwise choose Sign In and finish in the browser or terminal "
                "that opens."
            )
        limitations = ""
        if not info.supports_model:
            limitations += f"\n{label} manages model selection in its own terminal UI."
        if not info.supports_permissions:
            limitations += f"\n{label} manages permissions internally."
        if not info.supports_effort:
            limitations += f"\n{label} does not expose a reasoning effort level."
        if not info.supports_compaction:
            limitations += f"\n{label} cannot compact a conversation; start a new one instead."
        self._done_text.SetLabel(
            f"BlindPilot is ready to use {label}.\n\n"
            "Type in the Prompt and press Enter to send. "
            f"{_chord('Ctrl+R')} jumps to the latest response, {_chord('Ctrl+/')} lists "
            f"slash commands, {_chord('Ctrl+period')} stops a task."
            f"{limitations}\n\nChoose Finish."
        )
        for page in self._pages:
            page.Layout()

    def _on_backend_choice(self, _event: wx.CommandEvent) -> None:
        selection = self._setup_backend_picker.GetSelection()
        if not (0 <= selection < len(BACKEND_IDS)):
            return
        self.backend = BACKEND_IDS[selection]
        self._backend_path = None
        # The address the previous CLI handed out signs you in to the previous
        # provider. Opening it from here would be worse than offering nothing.
        self._stop_login()
        self._signin_status.SetLabel("")
        self._refresh_backend_copy()
        self.Layout()

    def _find_selected_cli(self) -> Optional[str]:
        return SetupWizard._find_selected_cli_for_backend(self.backend)

    @staticmethod
    def _find_selected_cli_for_backend(backend: str) -> Optional[str]:
        if backend == BACKEND_CLAUDE:
            return _find_claude()
        if backend == BACKEND_MUSE:
            # The launcher lives inside WSL on Windows, where a Windows
            # process cannot run it; asking the ordinary search would find
            # nothing and the wizard would offer an install that already
            # happened. The Muse adapter knows where its own CLI is.
            from muse_backend import muse_cli_path

            return muse_cli_path()
        return find_backend_cli(backend)

    def _selected_install_argv(self) -> Optional[List[str]]:
        return SetupWizard._selected_install_argv_for_backend(self.backend)

    @staticmethod
    def _selected_install_argv_for_backend(backend: str) -> Optional[List[str]]:
        if backend == BACKEND_CLAUDE:
            return _install_argv()
        if backend == BACKEND_HERMES:
            return _hermes_install_argv()
        if backend == BACKEND_MUSE:
            return _muse_install_argv()
        argv = _npm_install_argv(backend)
        if argv is not None:
            return argv
        # The actual Node.js command is discovered at install time. A sentinel
        # keeps the accessible Install button available on a clean computer.
        return ["managed-node-lts"] if _automatic_npm_install_available() else None

    # ---- navigation ----

    def _show_step(self, step: int) -> None:
        if step != self._step:
            self._update_check_generation += 1
            self._cli_check_pending = False
        self._step = step
        self._book.SetSelection(step)
        n = len(self._STEPS)
        title = f"{backend_label(self.backend)} CLI" if step == 1 else self._STEPS[step]
        self._step_label.SetLabel(f"Step {step + 1} of {n}: {title}")
        self._back_btn.Enable(step > 0)
        if step == n - 1:
            self._next_btn.SetLabel("Finish")
        else:
            self._next_btn.SetLabel("Next")
        self._next_btn.Enable(True)
        if step == 1:
            wx.CallAfter(self._check_cli)
        elif step == 2:
            wx.CallAfter(self._check_signin)
        self.Layout()
        announce(f"Step {step + 1} of {n}: {title}")

    def _go(self, direction: int) -> None:
        target = self._step + direction
        if target < 0:
            return
        if target >= len(self._STEPS):
            self._stop_login()
            self.EndModal(wx.ID_OK)
            return
        self._show_step(target)

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._stop_login()
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()

    def _on_wizard_close(self, event: wx.CloseEvent) -> None:
        self._update_check_generation = getattr(self, "_update_check_generation", 0) + 1
        self._stop_login()
        event.Skip()

    def _watch_for_sign_in(self, backend: str, step: int) -> None:
        """Wait, off the GUI thread, for a terminal sign-in to land.

        Gemini CLI and agy sign in from inside their interactive UI and keep
        running afterwards, so the terminal closing says nothing. The CLI's own
        signed-in check is asked every few seconds instead, and the wizard moves
        on once it answers yes -- while it is still on the sign-in step, for the
        backend the sign-in was started for.
        """
        ticket = getattr(self, "_sign_in_watch", 0) + 1
        self._sign_in_watch = ticket
        deadline = time.monotonic() + _SIGN_IN_WATCH_SECONDS
        while time.monotonic() < deadline:
            time.sleep(_SIGN_IN_WATCH_INTERVAL)
            try:
                if not self or self._sign_in_watch != ticket or self.backend != backend:
                    return
                if self._step != step:
                    return
            except RuntimeError:
                # The wizard was destroyed while this slept.
                return
            try:
                signed_in = backend_auth_ok(backend)
            except Exception:  # noqa: BLE001 - a failed check is "not yet"
                signed_in = False
            if signed_in:
                wx.CallAfter(self._on_watched_sign_in, ticket, backend, step)
                return

    def _on_watched_sign_in(self, ticket: int, backend: str, step: int) -> None:
        if not self or getattr(self, "_sign_in_watch", 0) != ticket:
            return
        if self.backend != backend or self._step != step:
            return
        self._on_login_done(True, "")
        announce(f"{backend_label(backend)} is signed in. You can close its terminal window.")

    def _stop_login(self) -> None:
        """Leave no half-finished sign-in running behind a closed wizard."""
        # A watched terminal sign-in stops being watched too.
        self._sign_in_watch = getattr(self, "_sign_in_watch", 0) + 1
        self._close_code_dialog()
        login, self._login = self._login, None
        if login is not None:
            login.cancel()
        self._stop_hidden_login()

    def _stop_hidden_login(self) -> None:
        """End a sign-in running in a terminal nobody can see."""
        hidden, self._hidden_login = self._hidden_login, None
        if hidden is not None:
            end_hidden_terminal(hidden)

    # ---- CLI step ----

    def _check_cli(
        self,
        force_update_check: bool = False,
        _facts: Optional[tuple[Optional[str], bool, bool, bool]] = None,
        _generation: Optional[int] = None,
        _backend: Optional[str] = None,
    ) -> None:
        if not self:
            # `_show_step` queues these, so one can land an event-loop
            # iteration after the wizard was closed.
            return
        if _facts is None:
            if self._cli_check_pending:
                return
            self._update_check_generation += 1
            generation = self._update_check_generation
            backend = self.backend
            self._cli_check_pending = True
            self._cli_status.SetLabel(f"Checking for {backend_label(backend)} CLI…")
            self._cli_detail.SetLabel("This may take several seconds.")
            self._next_btn.Enable(False)
            self._pages[1].Layout()
            self.Layout()
            announce(self._cli_status.GetLabel())

            def work() -> None:
                try:
                    path = SetupWizard._find_selected_cli_for_backend(backend)
                    on_path = _is_on_persistent_path(Path(path).parent) if path else False
                    install_available = (
                        SetupWizard._selected_install_argv_for_backend(backend) is not None
                    )
                    npm_available = _find_npm() is not None
                    facts = path, on_path, install_available, npm_available
                    error = ""
                except Exception as exc:  # noqa: BLE001 - report check failures accessibly
                    facts = None
                    error = str(exc) or type(exc).__name__
                _call_after_alive(
                    self._on_cli_check_finished,
                    generation,
                    backend,
                    force_update_check,
                    facts,
                    error,
                )

            threading.Thread(target=work, daemon=True).start()
            return

        if (
            _generation != self._update_check_generation
            or _backend != self.backend
            or self._step != 1
        ):
            return
        self._cli_check_pending = False
        self._backend_path, on_path, install_available, npm_available = _facts
        self._cli_check_btn.Enable(True)
        if self.backend != BACKEND_CLAUDE:
            self._check_npm_backend_cli(
                force_update_check,
                on_path=on_path,
                install_available=install_available,
                npm_available=npm_available,
                backend_path=self._backend_path,
                prepared=True,
            )
            return
        windows = platform.system() == "Windows"
        # Spoken after the status line. The labels are long, and what the user
        # needs to hear is which button to Tab to.
        hint = ""

        if self._backend_path:
            folder = Path(self._backend_path).parent
            self._cli_status.SetLabel("Claude Code found:")
            if on_path:
                self._cli_detail.SetLabel(self._backend_path)
                self._cli_path_btn.Hide()
            else:
                # Reachable from this app but not from a terminal. Worth
                # fixing, since /login and everything else assume a shell.
                self._cli_detail.SetLabel(
                    f"{self._backend_path}\n\n"
                    f"{folder} is not on your PATH, so typing 'claude' in "
                    f"{_path_shells()} will not work. "
                    "Click Add to PATH to fix that."
                )
                self._cli_path_btn.Show()
                hint = (
                    f"But {folder} is not on your PATH, so 'claude' will not "
                    "work in a terminal. Tab to the Add to PATH button to fix it."
                )
            self._cli_install_btn.Hide()
            self._cli_update_btn.Hide()
            self._cli_check_btn.Hide()
            self._next_btn.Enable(True)
            self._check_backend_updates(self._backend_path, force_update_check)
        elif install_available:
            flavour = "native Windows version" if windows else "native version"
            self._cli_status.SetLabel("Claude Code is not installed on this computer.")
            self._cli_detail.SetLabel(
                f"Choose Install Claude Code. It installs the {flavour} with no "
                "administrator rights and adds it to PATH.\n\n"
                "Or install it yourself from claude.com/claude-code and choose "
                "Check Again. To use another backend, go Back and pick one."
            )
            self._cli_install_btn.Show()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)
            hint = "Tab to Install Claude Code, or go Back to pick another backend."
        else:
            # No PowerShell, or no curl. Nothing to drive an install with.
            command = (
                f"irm {WINDOWS_INSTALL_PS1_URL} | iex"
                if windows
                else f"curl -fsSL {POSIX_INSTALL_SH_URL} | bash"
            )
            self._cli_status.SetLabel("Claude Code CLI was not found on this computer.")
            self._cli_detail.SetLabel(
                f"{_missing_prereq_message()}\n\n"
                f"Install Claude Code by running this in a terminal:\n\n"
                f"{command}\n\n"
                "Then choose Check Again. To use another backend, go Back and pick one."
            )
            self._cli_install_btn.Hide()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)

        self._pages[1].Layout()
        self.Layout()
        announce(" ".join(filter(None, (self._cli_status.GetLabel(), hint))))

    def _on_cli_check_finished(
        self,
        generation: int,
        backend: str,
        force_update_check: bool,
        facts: Optional[tuple[Optional[str], bool, bool, bool]],
        error: str,
    ) -> None:
        if (
            not self
            or self._step != 1
            or generation != self._update_check_generation
            or backend != self.backend
        ):
            return
        self._cli_check_pending = False
        if error or facts is None:
            label = backend_label(backend)
            self._cli_status.SetLabel(f"Could not check for {label} CLI.")
            self._cli_detail.SetLabel(
                f"{error or 'Try Check Again.'}\n\nChoose Check Again to retry."
            )
            self._cli_check_btn.Show()
            self._cli_check_btn.Enable(True)
            self._next_btn.Enable(False)
            self._pages[1].Layout()
            self.Layout()
            announce(self._cli_status.GetLabel() + " " + self._cli_detail.GetLabel())
            return
        self._check_cli(force_update_check, facts, generation, backend)

    def _check_npm_backend_cli(
        self,
        force_update_check: bool = False,
        *,
        on_path: Optional[bool] = None,
        install_available: Optional[bool] = None,
        npm_available: Optional[bool] = None,
        backend_path: Optional[str] = None,
        prepared: bool = False,
    ) -> None:
        """Check a non-Claude backend without showing Claude-specific guidance."""
        info = BACKENDS[self.backend]
        self._backend_path = backend_path if prepared else self._find_selected_cli()
        hint = ""
        if self._backend_path:
            folder = Path(self._backend_path).parent
            if on_path is None:
                on_path = _is_on_persistent_path(folder)
            self._cli_status.SetLabel(f"{info.label} found:")
            self._cli_detail.SetLabel(self._backend_path)
            self._cli_install_btn.Hide()
            self._cli_update_btn.Hide()
            self._cli_check_btn.Hide()
            if on_path:
                self._cli_path_btn.Hide()
            else:
                self._cli_detail.SetLabel(
                    f"{self._backend_path}\n\n{folder} is not on your persistent "
                    f"PATH. Choose Add to PATH so '{info.executable}' also works "
                    f"in {_path_shells()}."
                )
                self._cli_path_btn.Show()
                hint = "Tab to Add to PATH to make the CLI available in new terminals."
            self._next_btn.Enable(True)
            self._check_backend_updates(self._backend_path, force_update_check)
        elif not _backend_installs_with_npm(self.backend):
            # This backend does not come from npm: it has an installer of its
            # own. Offer it when the prerequisites are here, and name what is
            # missing when they are not. npm is never named, since saying it
            # would send the user after the wrong thing.
            has_installer = (
                install_available
                if install_available is not None
                else self._selected_install_argv() is not None
            )
            if has_installer:
                self._cli_status.SetLabel(f"{info.label} is not installed.")
                self._cli_detail.SetLabel(
                    f"Choose Install {info.label} to run its official installer. No "
                    "administrator rights are needed.\n\n"
                    f"Or run it yourself: {info.install_command}\n\n"
                    "Then choose Check Again."
                )
                self._cli_install_btn.Show()
                self._cli_update_btn.Hide()
                self._cli_path_btn.Hide()
                self._cli_check_btn.Show()
                self._next_btn.Enable(False)
                hint = f"Tab to Install {info.label}."
            else:
                self._cli_status.SetLabel(f"{info.label} was not found.")
                self._cli_detail.SetLabel(
                    f"{_muse_missing_prereq_message() if self.backend == BACKEND_MUSE else _hermes_missing_prereq_message()}\n\n"
                    f"Install {info.label} by running this in a terminal:\n\n"
                    f"{info.install_command}\n\n"
                    "Then choose Check Again, or go Back and select another backend."
                )
                self._cli_install_btn.Hide()
                self._cli_update_btn.Hide()
                self._cli_path_btn.Hide()
                self._cli_check_btn.Show()
                self._next_btn.Enable(False)
                hint = "Tab to Check Again once it is installed."
        elif (
            install_available
            if install_available is not None
            else self._selected_install_argv() is not None
        ):
            self._cli_status.SetLabel(f"{info.label} is not installed.")
            has_npm = npm_available if npm_available is not None else _find_npm() is not None
            if not has_npm:
                self._cli_detail.SetLabel(
                    f"Choose Install {info.label}. BlindPilot will first install the "
                    "latest Node.js LTS and npm for your user account, without administrator "
                    f"rights, then install and verify {info.label} and add it to PATH.\n\n"
                    f"To do it yourself, install Node.js and run:\n\n{info.install_command}\n\n"
                    "Then choose Check Again."
                )
            else:
                self._cli_detail.SetLabel(
                    f"Choose Install {info.label} to run:\n\n{info.install_command}\n\n"
                    "BlindPilot installs it for your user account, verifies that it starts, "
                    "and adds it to PATH. You can also run the command in a terminal and "
                    "choose Check Again."
                )
            self._cli_install_btn.Show()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)
            hint = f"Tab to Install {info.label}."
        else:
            self._cli_status.SetLabel(f"{info.label} was not found.")
            self._cli_detail.SetLabel(
                f"Automatic Node.js installation is unavailable on this computer. Install "
                f"Node.js and npm, then run:\n\n{info.install_command}\n\nThen choose "
                "Check Again, or go Back and select another backend."
            )
            self._cli_install_btn.Hide()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)
        self._pages[1].Layout()
        self.Layout()
        announce(" ".join(filter(None, (self._cli_status.GetLabel(), hint))))

    def _check_backend_updates(self, binary: str, force: bool = False) -> None:
        backend = self.backend
        generation = self._update_check_generation

        def work() -> None:
            result = self._backend_update_service.check(backend, force=force)
            wx.CallAfter(self._on_backend_update_check, generation, backend, binary, result)

        threading.Thread(target=work, daemon=True).start()

    def _on_backend_update_check(
        self, generation: int, backend: str, binary: str, result: BackendUpdateResult
    ) -> None:
        if (
            not self
            or self._step != 1
            or generation != self._update_check_generation
            or backend != self.backend
        ):
            return
        label = backend_label(backend)
        self._cli_update_btn.Hide()
        self._cli_check_btn.Hide()
        state = result.state
        if state is BackendUpdateState.UPDATE_AVAILABLE:
            self._cli_status.SetLabel(f"{label} update {result.available_version} is available.")
            self._cli_detail.SetLabel(f"{binary}\n\nChoose Update {label} to install it manually.")
            self._cli_update_btn.Show()
        elif state is BackendUpdateState.CURRENT:
            self._cli_status.SetLabel(f"{label} version {result.installed_version} is current.")
            self._cli_detail.SetLabel(binary)
        elif state is BackendUpdateState.FAILED:
            self._cli_status.SetLabel(f"Could not check {label} for updates.")
            self._cli_detail.SetLabel(f"{binary}\n\n{result.failure_reason or 'Try Check Again.'}")
            self._cli_check_btn.Show()
        self._pages[1].Layout()
        self.Layout()
        announce(self._cli_status.GetLabel())

    def _cli_log_line(self, text: str) -> None:
        """Append a line of installer output and speak it."""
        if not self:
            # Installing takes a minute, Cancel and Escape stay live
            # throughout, and nothing tells the install thread to stop.
            # So this can arrive against a dialog that is already gone.
            return
        if not self._cli_log.IsShown():
            self._cli_log.Show()
            self._pages[1].Layout()
        self._cli_log.AppendText(text + "\n")
        announce(text)

    def _repair_path(self) -> None:
        if not self._backend_path:
            return
        folder = Path(self._backend_path).parent
        try:
            changed = ensure_on_path(folder)
        except OSError as exc:
            self._cli_log_line(f"Could not update your PATH: {exc}")
            return
        self._cli_log_line(
            f"Added {folder} to {changed}. Open a new terminal window to use it."
            if changed
            else f"{folder} was already on your PATH."
        )
        self._cli_path_btn.Hide()
        self._check_cli()

    # ---- Installing the CLI (Windows) ----

    def _install_cli(self) -> None:
        label = backend_label(self.backend)
        argv = self._selected_install_argv()
        if argv is None:
            # A stale Install button (a dialog built before a check that no
            # longer offers one), or the prerequisites vanished mid-session.
            # Captured from NVDA: pressing this used to promise "under a
            # minute" for an install that could not happen, then report npm —
            # whether or not npm existed — three milliseconds later. Promise
            # nothing; say what is missing in the backend's own terms.
            if _backend_installs_with_npm(self.backend):
                announce(_missing_prereq_message())
            elif self.backend == BACKEND_MUSE:
                announce(_muse_missing_prereq_message())
            else:
                announce(_hermes_missing_prereq_message())
            return
        self._cli_install_btn.Disable()
        self._cli_update_btn.Disable()
        self._cli_check_btn.Disable()
        self._back_btn.Disable()
        self._next_btn.Disable()
        self._cli_status.SetLabel(f"Installing {label}...")
        self._cli_log.Show()
        self._cli_log.SetValue("")
        self._pages[1].Layout()
        self.Layout()
        announce(f"Installing {label}. This usually takes under a minute.")
        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self) -> None:
        def log(text: str) -> None:
            wx.CallAfter(self._cli_log_line, text)

        try:
            binary = install_backend(self.backend, log)
        except Exception as exc:  # never leave the wizard wedged on a crash
            log(f"The install failed: {exc}")
            binary = None
        wx.CallAfter(self._on_install_done, binary)

    def _on_install_done(self, binary: Optional[str]) -> None:
        if not self:
            # Installing takes a minute, Cancel and Escape stay live
            # throughout, and nothing tells the install thread to stop.
            # So this can arrive against a dialog that is already gone.
            return
        label = backend_label(self.backend)
        self._cli_install_btn.Enable()
        self._cli_update_btn.Enable()
        self._cli_check_btn.Enable()
        self._back_btn.Enable(self._step > 0)
        self._next_btn.Enable(True)
        if binary:
            self._backend_path = binary
            announce(f"{label} installed.")
        else:
            self._cli_status.SetLabel("The install did not complete.")
            announce(_install_failure_message(self.backend))
        self._check_cli()

    def _update_cli(self) -> None:
        """Update the selected installed backend without blocking the dialog."""
        label = backend_label(self.backend)
        self._cli_install_btn.Disable()
        self._cli_update_btn.Disable()
        self._cli_check_btn.Disable()
        self._back_btn.Disable()
        self._next_btn.Disable()
        self._cli_status.SetLabel(f"Updating {label}...")
        self._cli_log.Show()
        self._cli_log.SetValue("")
        self._pages[1].Layout()
        self.Layout()
        announce(f"Updating {label}. Progress will be announced.")
        threading.Thread(target=self._run_update, daemon=True).start()

    def _run_update(self) -> None:
        def log(text: str) -> None:
            wx.CallAfter(self._cli_log_line, text)

        try:
            updated = update_backend(self.backend, log)
        except Exception as exc:  # never leave the wizard wedged on a crash
            log(f"The update failed: {exc}")
            updated = False
        wx.CallAfter(self._on_update_done, updated)

    def _on_update_done(self, updated: bool) -> None:
        if not self:
            # Installing takes a minute, Cancel and Escape stay live
            # throughout, and nothing tells the install thread to stop.
            # So this can arrive against a dialog that is already gone.
            return
        label = backend_label(self.backend)
        self._cli_install_btn.Enable()
        self._cli_update_btn.Enable()
        self._cli_check_btn.Enable()
        self._back_btn.Enable(self._step > 0)
        self._next_btn.Enable(True)
        if updated:
            invalidate_model_options(self.backend)
            announce(f"{label} is up to date.")
            self._backend_update_service.invalidate(self.backend)
        else:
            self._cli_status.SetLabel(f"The {label} update did not complete.")
            announce(f"The {label} update did not complete. Review the updater output.")
        self._check_cli()

    # ---- Sign-in step ----

    def _check_signin(self) -> None:
        if not self:
            # `_show_step` queues these, so one can land an event-loop
            # iteration after the wizard was closed.
            return
        label = backend_label(self.backend)
        self._open_page_btn.Enable(self._login is not None and bool(self._login.url))
        self._backend_path = self._find_selected_cli()
        if not self._backend_path:
            self._show_signin_status(
                f"{label} is not installed. Go Back and complete the CLI step first."
            )
            return
        self._show_signin_status(f"Checking whether {label} is signed in…")
        backend = self.backend

        def work() -> None:
            # The probe runs a CLI with a timeout of up to 25 seconds. On the
            # GUI thread that froze the wizard and the screen reader with it.
            ok = backend_auth_ok(backend)
            wx.CallAfter(self._on_signin_checked, backend, ok)

        threading.Thread(target=work, daemon=True).start()

    def _on_signin_checked(self, backend: str, ok: bool) -> None:
        if not self or backend != self.backend:
            return
        label = backend_label(backend)
        self._show_signin_status(
            f"{label} reports that you are signed in."
            if ok
            else f"BlindPilot could not confirm a {label} sign-in yet."
        )

    def _show_signin_status(self, text: str) -> None:
        self._signin_status.SetLabel(text)
        self._pages[2].Layout()
        self.Layout()
        announce(text)

    def _do_login(self) -> None:
        # A previous attempt whose terminal is still open on the browser is
        # stopped first, so asking again cannot leave two sign-ins running.
        self._stop_hidden_login()
        if self.backend == BACKEND_OPENCODE:
            # opencode signs in by picking a provider and giving it a key or a
            # browser round-trip, which is exactly what /connect does. Shelling
            # out to the CLI's version of it would leave a terminal prompt
            # nobody can see, waiting on input nobody can give it.
            dlg = ConnectDialog(self)
            try:
                dlg.ShowModal()
            finally:
                dlg.Destroy()
            self._check_signin()
            return
        if not self._backend_path:
            self._backend_path = self._find_selected_cli()
        if not self._backend_path:
            self._signin_status.SetLabel(
                f"{backend_label(self.backend)} CLI not found. "
                "Please complete the previous step first."
            )
            announce(self._signin_status.GetLabel())
            return
        self._signin_btn.Disable()
        self._already_btn.Disable()
        self._next_btn.Disable()
        self._open_page_btn.Disable()
        # A setup that asks its questions in a terminal is not watched for a
        # browser address; `_run_login` gives it a console instead -- shown for
        # a backend the user has to answer, hidden for one that needs the
        # terminal only to exist.
        # The login runner builds [binary, *login_args] and hands it to Popen.
        # On Windows Muse's binary is a bash script inside WSL, which Popen
        # cannot execute, so the argv is rebuilt through the same bridge every
        # other Muse path takes. Elsewhere plain Popen runs it.
        muse_popen: Optional[Callable[..., subprocess.Popen]] = None
        muse_argv = (
            muse_command() or []
            if self.backend == BACKEND_MUSE and platform.system() == "Windows"
            else []
        )
        if muse_argv:

            def through_wsl(args: list[str], **kwargs: Any) -> subprocess.Popen:
                return subprocess.Popen([*muse_argv, *args[1:]], **kwargs)

            muse_popen = through_wsl
        self._login = (
            None
            if BACKENDS[self.backend].login_needs_terminal
            else BackendLogin(self.backend, self._backend_path, popen=muse_popen)
        )
        self._signin_status.SetLabel(
            "Waiting for sign-in… Complete authentication in your browser, then return here."
        )
        self._pages[2].Layout()
        self.Layout()
        announce(self._signin_status.GetLabel())
        threading.Thread(target=self._run_login, daemon=True).start()

    def _run_login(self) -> None:
        if BACKENDS[self.backend].login_needs_terminal:
            # An interactive setup cannot run hidden with no stdin: it dies
            # immediately and the wizard would report a failed sign-in for a
            # backend that is simply waiting to be asked.
            binary = self._backend_path
            if binary is None:
                wx.CallAfter(self._on_login_terminal_opened, "", False)
                return
            _clear_stale_login(binary, BACKENDS[self.backend].login_reset_args)
            if BACKENDS[self.backend].login_terminal_hidden:
                self._run_hidden_login(binary)
                return
            # Its questions are the user's to answer, so give it a real
            # console and let them read it.
            self._launch_login_terminal([binary, *BACKENDS[self.backend].login_args])
            if BACKENDS[self.backend].login_watch_until_signed_in:
                self._watch_for_sign_in(self.backend, self._step)
            return
        login = self._login
        assert login is not None
        rc = login.run(
            lambda text: wx.CallAfter(self._on_login_progress, text),
            lambda url, opened: wx.CallAfter(self._on_login_url, url, opened),
            lambda prompt, url: wx.CallAfter(self._ask_login_code, prompt, url),
        )
        # The CLI is the only thing that knows whether the browser round-trip
        # landed, and not all of them say so with an exit code — Claude Code
        # keeps running after a rejected code, and a kill for taking too long
        # looks identical to one for going wrong. Asking the CLI whether it is
        # signed in settles it either way.
        ok = rc == 0 or backend_auth_ok(self.backend)
        wx.CallAfter(self._on_login_done, ok, login.failure)

    def _run_hidden_login(self, binary: str) -> None:
        """Sign in through a terminal the user never sees.

        Command Code's login mounts an Ink UI, which refuses to start without a
        real terminal and needs nothing else from it: the sign-in is the
        browser page the CLI opens. Running it in a hidden terminal keeps a
        console that exists only for that UI from appearing, from taking focus,
        and from taking screen-reader focus with it. When the command ends the
        CLI is asked whether the sign-in landed, because it is the only thing
        that knows.
        """
        args = [binary, *BACKENDS[self.backend].login_args]
        try:
            terminal = spawn_hidden_terminal(args, str(Path.home()))
        except Exception as exc:  # noqa: BLE001 - the crash IS the report
            wx.CallAfter(
                self._on_login_done,
                False,
                f"Could not start {backend_label(self.backend)} to sign in: {exc}",
            )
            return
        self._hidden_login = terminal
        deadline = time.monotonic() + _HIDDEN_LOGIN_TIMEOUT
        while _terminal_running(terminal) and time.monotonic() < deadline:
            time.sleep(0.25)
        if _terminal_running(terminal):
            # Still open on the browser. It is left running rather than killed:
            # a sign-in that finishes late still finishes, and the window has
            # already said the wizard stopped waiting. Leaving the wizard, or
            # asking to sign in again, stops it.
            wx.CallAfter(self._on_login_done, False, "")
            return
        self._hidden_login = None
        end_hidden_terminal(terminal)
        ok = backend_auth_ok(self.backend)
        wx.CallAfter(self._on_login_done, ok, "")

    def _launch_login_terminal(self, args: List[str]) -> None:
        """Open a real console for a backend whose setup asks questions.

        The user answers in that window, not in this one, so there is no exit
        code worth waiting for here: the wizard says what to do and re-checks
        afterwards rather than declaring a result it cannot know.
        """
        command = subprocess.list2cmdline(args)
        try:
            if platform.system() == "Windows":
                # start opens a console window of its own; the empty title
                # argument is what keeps a quoted path from being read as one.
                subprocess.Popen(["cmd", "/c", "start", "", *args], close_fds=True)
            elif platform.system() == "Darwin":
                script = f'tell application "Terminal" to do script "{command}"'
                subprocess.Popen(["osascript", "-e", script], close_fds=True)
            else:
                for terminal in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
                    if shutil.which(terminal):
                        subprocess.Popen([terminal, "-e", *args], close_fds=True)
                        break
                else:
                    raise OSError("no terminal emulator found")
        except (OSError, ValueError):
            wx.CallAfter(self._on_login_terminal_opened, command, False)
            return
        wx.CallAfter(self._on_login_terminal_opened, command, True)

    def _on_login_terminal_opened(self, command: str, opened: bool) -> None:
        if not self:
            return
        self._signin_btn.Enable()
        self._already_btn.Enable()
        self._next_btn.Enable()
        if opened and BACKENDS[self.backend].login_watch_until_signed_in:
            self._signin_status.SetLabel(
                f"{backend_label(self.backend)} has opened in a terminal window. "
                "Finish the Google sign-in in your browser; the wizard moves on by "
                "itself once you are signed in."
            )
        elif opened:
            self._signin_status.SetLabel(
                f"{backend_label(self.backend)} setup has opened in a terminal "
                "window. Answer its questions there, then come back and choose "
                "Already Signed In."
            )
        elif command:
            self._signin_status.SetLabel(
                f"BlindPilot could not open a terminal. Run this yourself, then "
                f"choose Already Signed In:\n\n{command}"
            )
        else:
            self._signin_status.SetLabel(
                f"BlindPilot could not find {backend_label(self.backend)} to run. "
                "Go back a page and install or locate it first."
            )
        self._pages[2].Layout()
        self.Layout()
        announce(self._signin_status.GetLabel())

    def _on_login_progress(self, text: str) -> None:
        if not self:
            return
        self._signin_status.SetLabel(text)
        self._pages[2].Layout()
        self.Layout()
        announce(text)

    def _on_login_url(self, url: str, opened: bool) -> None:
        """The CLI has said where to sign in. Say it, and offer to open it."""
        if not self:
            return
        self._open_page_btn.Enable()
        if opened:
            text = "The sign-in page is open in your browser. Complete it, then return here."
        else:
            text = (
                "Your browser should have opened the sign-in page. If it did not, "
                f"choose Open Sign-in Page. The address is {url}"
            )
        self._on_login_progress(text)

    def _open_sign_in_page(self) -> None:
        """The browser did not arrive, or it was closed. Open it again."""
        login = self._login
        if login is None or not login.url:
            announce("There is no sign-in page yet. Choose Sign In first.")
            return
        if login.open_page():
            announce("Opened the sign-in page in your browser.")
        else:
            announce(f"Could not open a browser. The sign-in address is {login.url}")

    def _ask_login_code(self, prompt: str, url: str) -> None:
        """The CLI is waiting for the code the sign-in page hands back.

        Not every sign-in ends this way, since the same page usually completes
        the round-trip on its own, so this dialog is a way in, not a wall. It
        closes itself the moment the CLI finishes without it.
        """
        if not self or self._code_dialog is not None:
            return
        message = (
            f"{prompt or 'The sign-in page may give you a code.'}\n\n"
            "Paste the code and choose OK. If there was no code, leave this "
            "open; it closes when the browser finishes."
        )
        if url:
            message = f"{message}\n\n{url}"
        dlg = wx.TextEntryDialog(self, message, "Sign In")
        self._code_dialog = dlg
        try:
            code = dlg.GetValue().strip() if dlg.ShowModal() == wx.ID_OK else ""
        finally:
            self._code_dialog = None
            dlg.Destroy()
        if code and self._login is not None:
            self._login.submit_code(code)

    def _close_code_dialog(self) -> None:
        dlg = self._code_dialog
        if dlg is None:
            return
        self._code_dialog = None
        try:
            dlg.EndModal(wx.ID_CANCEL)
        except Exception:
            pass

    def _on_login_done(self, ok: bool, failure: str) -> None:
        if not self:
            return
        self._close_code_dialog()
        self._signin_btn.Enable()
        self._already_btn.Enable()
        self._next_btn.Enable()
        if ok:
            self._signin_status.SetLabel("Signed in successfully.")
            self._go(+1)
        else:
            trouble = f"{failure} " if failure else ""
            self._signin_status.SetLabel(
                f"{trouble}Sign-in did not complete (or timed out). "
                "Try again, choose Open Sign-in Page to reopen the browser, or "
                "choose Already Signed In if you are authenticated."
            )
        self._pages[2].Layout()
        self.Layout()
        announce(self._signin_status.GetLabel())

    # ---- Projects step ----

    def _proj_display(self) -> str:
        return (
            f"Selected: {self.projects_folder}"
            if self.projects_folder
            else "None selected yet (optional)."
        )

    def _pick_folder(self) -> None:
        with wx.DirDialog(
            self,
            "Choose your Projects folder",
            defaultPath=self.projects_folder or os.path.expanduser("~"),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self.projects_folder = dlg.GetPath()
        self._proj_label.SetLabel(self._proj_display())
        self._pages[3].Layout()
        announce(f"Projects folder: {self.projects_folder}")
