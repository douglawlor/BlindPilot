"""New Session: pick a folder, or clone one, in a new tab.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

The folder box starts on the folder opened last, and Up and Down
walk the other folders used, newest first. The git runner the
worktree helper needs, _git, lives in process_utils. Names the
dialog calls are patched here, where the calls are made from.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from typing import Optional

import wx

from app_config import _load_config
from dialog_tasks import _call_after_alive
from process_utils import _git, no_window_kwargs
from screen_speech import announce

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12


def _recent_folders() -> list[str]:
    """Folders New Session has opened, newest first."""
    found = _load_config().get("recent_folders")
    return [f for f in found if isinstance(f, str) and f] if isinstance(found, list) else []


def create_worktree(folder: str, branch: str) -> tuple[str, str]:
    """A new git worktree for `branch` beside `folder`'s repository.

    Returns (path, "") or ("", why not). The worktree goes next to the
    repository as ``<repo>-<branch>``, so the folder the person chose stays as
    it was. An existing branch is checked out there; a new name makes one.
    """
    try:
        top = _git(folder, "rev-parse", "--show-toplevel")
        if top.returncode:
            return "", "That folder is not in a git repository."
        root = os.path.normpath(top.stdout.strip())
        safe = re.sub(r"[^\w.-]+", "-", branch).strip("-.") or "worktree"
        path = os.path.join(os.path.dirname(root), f"{os.path.basename(root)}-{safe}")
        if os.path.exists(path):
            return "", f"That folder already exists:\n{path}"
        known = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if known.returncode == 0:
            done = _git(root, "worktree", "add", path, branch)
        else:
            done = _git(root, "worktree", "add", "-b", branch, path)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"git could not make the worktree: {exc}"
    if done.returncode:
        return "", (done.stderr or done.stdout).strip() or "git could not make the worktree."
    return path, ""


class NewSessionDialog(wx.Dialog):
    """New Session. What it asks for depends on WHERE the session will run.

    A local backend runs in a folder on this disk, so the folder is the useful
    question and Browse can answer it. A Hermes on another machine cannot see
    this disk at all, so the folder picker is worse than useless there — it is
    misleading. Measured against a live gateway, with the result read back from
    the server's own state.db rather than from the reply:

        cwd sent = C:\\Users\\g\\Desktop\\projekt   (a Windows path, Linux Hermes)
        -> session.create returns OK, no error of any kind
        -> the session's stored cwd is '/home/ubuntu' — the SERVER's home

    The remote end validates the path against its own filesystem, silently
    substitutes its own directory, and says nothing. Meanwhile this dialog
    required the path to exist HERE, so a folder browsed to on the Windows
    desktop passed validation and the session ran somewhere else entirely, with
    the tab named after a directory it was never in.

    So in remote mode the dialog asks for a NAME, and the folder becomes an
    optional path ON THE SERVER — free text, because only that machine can say
    whether it exists, and with no Browse button, since a picker here would
    browse the wrong computer.

    The name is optional in both modes. Hermes titles a conversation from its
    first message when none is given, which is the better default for anyone
    who does not want to name things up front; a name typed here is stored with
    title_source='user' and is NOT overwritten by that automatic title
    (measured: 'Nazwa z BlindPilota' survived a completed turn, while a session
    created without one came out as 'Odpowiedź jednym słowem OK #9').
    """

    def __init__(
        self,
        parent: wx.Window,
        default_dir: Optional[str] = None,
        remote_label: str = "",
    ):
        super().__init__(parent, title="New Session")
        self._default_dir = default_dir or os.path.expanduser("~")
        # Empty means "the Hermes installed here", i.e. the local-folder shape.
        self._remote_label = remote_label.strip()
        self.path = ""
        self.title_text = ""
        self.worktree_box: Optional[wx.CheckBox] = None
        self.branch_box: Optional[wx.TextCtrl] = None
        self._github_serial = 0
        self._github_cancel = threading.Event()
        self._github_cancel.set()

        rows = wx.BoxSizer(wx.VERTICAL)

        if self._remote_label:
            intro_text = (
                f"This session will run on {self._remote_label}, "
                "so folders on this computer do not apply to it."
            )
            intro = wx.StaticText(self, label=intro_text)
            rows.Add(intro, 0, wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(PAD_DIALOG))
            # A StaticText is never announced when a dialog opens: the user
            # lands on the name field having heard none of the context that
            # makes the dialog's shape make sense. Say the one sentence that
            # explains it, now, before the dialog takes the focus.
            announce(intro_text)

        # The name goes first in remote mode because it is the only field that
        # can be answered from here, and tab order is the order of usefulness.
        name_label = wx.StaticText(self, label="&Name for this session (optional):")
        self.name_box = wx.TextCtrl(self, value="")
        self.name_box.SetName("Name for this session, optional")
        self.name_box.SetMinSize(self.FromDIP(wx.Size(420, -1)))
        name_help = wx.StaticText(
            self,
            label="Leave it empty to let the first message name it.",
        )

        folder_caption = (
            "&Folder on that computer (optional):"
            if self._remote_label
            else "&Folder for the new session:"
        )
        folder_label = wx.StaticText(self, label=folder_caption)
        self.folder_box: wx.TextCtrl | wx.ComboBox
        if self._remote_label:
            self.folder_box = wx.TextCtrl(self, value="")
        else:
            # Folders used before, newest first: Up and Down walk them, and
            # the newest is filled in, because it is usually the one wanted.
            recent = [f for f in _recent_folders() if os.path.isdir(f)]
            self.folder_box = wx.ComboBox(
                self, value=recent[0] if recent else "", choices=recent, style=wx.CB_DROPDOWN
            )
        self.folder_box.SetName(
            "Folder on the remote computer, optional"
            if self._remote_label
            else "Folder for the new session"
        )
        self.folder_box.SetMinSize(self.FromDIP(wx.Size(420, -1)))

        folder_row = wx.BoxSizer(wx.HORIZONTAL)
        folder_row.Add(self.folder_box, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(PAD))
        if not self._remote_label:
            # No Browse in remote mode: it would open a picker on this machine
            # for a path that has to be valid on another one. An empty control
            # in the tab order is a cost a screen reader pays on every visit.
            browse_btn = wx.Button(self, label="&Browse…")
            browse_btn.Bind(wx.EVT_BUTTON, lambda _e: self._browse())
            folder_row.Add(browse_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(PAD))
            github_btn = wx.Button(self, label="From Git&Hub…")
            github_btn.Bind(wx.EVT_BUTTON, lambda _e: self._from_github())
            self.github_btn = github_btn
            folder_row.Add(github_btn, 0, wx.ALIGN_CENTER_VERTICAL)
            self.github_status = wx.StaticText(self, label="")
            self.github_status.Hide()
            self.github_cancel = wx.Button(self, label="&Cancel GitHub operation")
            self.github_cancel.Bind(wx.EVT_BUTTON, lambda _e: self._cancel_github())
            self.github_cancel.Hide()
            worktree_box = wx.CheckBox(self, label="Work in a new git &worktree")
            branch_label = wx.StaticText(self, label="B&ranch for the worktree:")
            branch_box = wx.TextCtrl(self, value="")
            branch_box.SetName("Branch for the worktree")
            branch_box.Disable()
            worktree_box.Bind(
                wx.EVT_CHECKBOX, lambda _e: branch_box.Enable(worktree_box.IsChecked())
            )
            self.worktree_box, self.branch_box = worktree_box, branch_box

        pad = self.FromDIP(PAD_DIALOG)
        if self._remote_label:
            rows.Add(name_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(self.name_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(name_help, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(folder_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(folder_row, 0, wx.EXPAND | wx.ALL, pad)
        else:
            rows.Add(folder_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(folder_row, 0, wx.EXPAND | wx.ALL, pad)
            rows.Add(self.github_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
            rows.Add(self.github_cancel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
            rows.Add(worktree_box, 0, wx.LEFT | wx.RIGHT, pad)
            rows.Add(branch_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(branch_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(name_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(self.name_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
            rows.Add(name_help, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        if buttons is not None:
            rows.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(rows)

        # Validate before the dialog closes, so a bad path can be corrected
        # in place instead of failing after the session is created.
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self._on_cancel_dialog, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.initial_focus().SetFocus()
        self.CentreOnParent()

    def initial_focus(self) -> wx.Window:
        """The field this dialog opens on: the one this machine can answer.

        Named rather than inlined so a test can assert on it. Reading it back
        with ``FindFocus()`` does not work -- that reports the focus of a window
        the platform has SHOWN, and on macOS it answers None for a dialog that
        was only constructed, which made the same assertion pass on Windows and
        Linux and fail on macOS for a reason unrelated to this code.
        """
        return self.name_box if self._remote_label else self.folder_box

    def _browse(self) -> None:
        typed = self.folder_box.GetValue().strip().strip('"')
        start = typed if typed and os.path.isdir(os.path.expanduser(typed)) else self._default_dir
        with wx.DirDialog(
            self,
            "Choose a folder for the new session",
            defaultPath=os.path.expanduser(start),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        self.folder_box.SetValue(path)
        self.folder_box.SetInsertionPointEnd()
        self.folder_box.SetFocus()
        announce(f"Folder set to {path}")

    def _from_github(self) -> None:
        """List repositories without holding up the New Session dialog."""
        serial, cancelled = self._start_github("Loading GitHub repositories...")

        def work() -> None:
            listed, error = self._run_github(
                ["gh", "repo", "list", "--limit", "300", "--json", "nameWithOwner",
                 "--jq", ".[].nameWithOwner"], 60, cancelled,
            )  # fmt: skip
            _call_after_alive(self._github_listed, serial, listed, error)

        threading.Thread(target=work, daemon=True).start()

    def _run_github(
        self, command: list[str], timeout: int, cancelled: threading.Event
    ) -> tuple[Optional[subprocess.CompletedProcess[str]], str]:
        if cancelled.is_set():
            return None, ""
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                start_new_session=os.name != "nt", **no_window_kwargs(),
            )  # fmt: skip
            deadline = time.monotonic() + timeout
            while True:
                if cancelled.is_set():
                    self._terminate_github_process(process)
                    return None, ""
                try:
                    remaining = max(0.01, deadline - time.monotonic())
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    return subprocess.CompletedProcess(
                        command, process.returncode, stdout, stderr
                    ), ""
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        self._terminate_github_process(process)
                        return None, f"GitHub CLI timed out after {timeout} seconds."
        except FileNotFoundError:
            return None, "The GitHub CLI (gh) is not installed. Install it and sign in first."
        except (OSError, subprocess.SubprocessError) as exc:
            return None, str(exc)

    @staticmethod
    def _terminate_github_process(process: subprocess.Popen[str]) -> None:
        # gh starts git as a child; stopping only gh can leave a clone running.
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, **no_window_kwargs(),
                )  # fmt: skip
            else:
                os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            process.wait(timeout=5)

    def _start_github(self, message: str) -> tuple[int, threading.Event]:
        self._github_serial += 1
        self._github_cancel = threading.Event()
        self.github_btn.Disable()
        self.github_status.SetLabel(message)
        self.github_status.Show()
        self.github_cancel.Show()
        self.Layout()
        self.Fit()
        self.github_cancel.SetFocus()
        announce(message)
        return self._github_serial, self._github_cancel

    def _finish_github(self) -> None:
        self.github_status.Hide()
        self.github_cancel.Hide()
        self.github_btn.Enable()
        self.Layout()

    def _stop_github(self) -> bool:
        if self._github_cancel.is_set():
            return False
        self._github_serial += 1
        self._github_cancel.set()
        return True

    def _cancel_github(self) -> None:
        if self._stop_github():
            self._finish_github()
            self.github_btn.SetFocus()
            announce("GitHub operation cancelled")

    def _github_listed(
        self, serial: int, listed: Optional[subprocess.CompletedProcess[str]], error: str
    ) -> None:
        if not self or serial != self._github_serial:
            return
        self._finish_github()
        if error:
            if error.startswith("The GitHub CLI (gh) is not installed"):
                self._reject(error)
            else:
                self._reject(f"The GitHub CLI could not list your repositories:\n{error}")
            return
        if listed is None:
            return
        if listed.returncode:
            detail = (listed.stderr or "").strip() or "gh repo list failed."
            self._reject(f"The GitHub CLI could not list your repositories:\n{detail}")
            return
        repos = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        typed_choice = "Type a repository name (owner/name)\u2026"
        self.github_btn.SetFocus()
        with wx.SingleChoiceDialog(
            self, "Repository:", "From GitHub", [typed_choice, *repos]
        ) as pick:
            if pick.ShowModal() != wx.ID_OK:
                return
            repo = pick.GetStringSelection()
        if repo == typed_choice:
            with wx.TextEntryDialog(self, "Repository, as owner/name:", "From GitHub") as ask:
                if ask.ShowModal() != wx.ID_OK:
                    return
                repo = ask.GetValue().strip()
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            self._reject(f"Not a repository name: {repo}")
            return
        target = os.path.join(os.path.expanduser(self._default_dir), repo.split("/")[1])
        if os.path.isdir(target):
            self._github_cloned(serial, repo, target, None, "")
            return
        serial, cancelled = self._start_github(f"Cloning {repo}...")

        def work() -> None:
            cloned, error = self._run_github(["gh", "repo", "clone", repo, target], 900, cancelled)
            _call_after_alive(self._github_cloned, serial, repo, target, cloned, error)

        threading.Thread(target=work, daemon=True).start()

    def _github_cloned(
        self,
        serial: int,
        repo: str,
        target: str,
        cloned: Optional[subprocess.CompletedProcess[str]],
        error: str,
    ) -> None:
        if not self or serial != self._github_serial:
            return
        if cloned is not None or error:
            self._finish_github()
        if error:
            self._reject(f"Cloning {repo} failed: {error}")
            return
        if cloned is not None and cloned.returncode:
            self._reject(f"Cloning {repo} failed:\n{(cloned.stderr or '').strip()}")
            return
        self.folder_box.SetValue(target)
        self.folder_box.SetFocus()
        announce(f"Folder set to {target}")

    def _on_cancel_dialog(self, event: wx.CommandEvent) -> None:
        self._stop_github()
        event.Skip()

    def _on_close(self, event: wx.CloseEvent) -> None:
        self._stop_github()
        event.Skip()

    def Destroy(self) -> bool:  # type: ignore[override]
        self._stop_github()
        return super().Destroy()

    def _on_ok(self, event: wx.CommandEvent) -> None:
        self.title_text = self.name_box.GetValue().strip()
        # Quotes are stripped because a path copied from Explorer often has them.
        typed = self.folder_box.GetValue().strip().strip('"')

        if self._remote_label:
            # Only the remote machine can judge this path, so it travels as
            # typed — expanded neither by os.path nor by this machine's
            # environment, whose variables and separators are not the server's.
            # Empty is the normal case: Hermes then uses its own working
            # directory, which is what a remote session usually wants.
            self.path = typed
            event.Skip()
            return

        if not typed:
            self._reject("Type a folder path, or use the Browse button.")
            return
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(typed)))
        if not os.path.isdir(path):
            self._reject(f"That folder does not exist:\n{path}")
            return
        if self.worktree_box is not None and self.worktree_box.IsChecked():
            branch = self.branch_box.GetValue().strip() if self.branch_box else ""
            if not branch:
                self._reject("Type a branch name for the worktree.")
                return
            with wx.BusyCursor():
                path, why = create_worktree(path, branch)
            if why:
                self._reject(why)
                return
        self.path = path
        self._stop_github()
        event.Skip()

    def _reject(self, message: str) -> None:
        # The modal below announces the message itself when it opens, so an
        # explicit announce here would say the same sentence twice -- the
        # duplicate-speech class removed elsewhere in this application.
        with wx.MessageDialog(self, message, "New Session", style=wx.OK | wx.ICON_WARNING) as warn:
            warn.ShowModal()
        self.folder_box.SetFocus()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._stop_github()
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()
