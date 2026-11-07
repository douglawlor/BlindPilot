"""Pick any Hermes conversation, including one running right now.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Recent Conversations reads what this machine has on disk, which is
empty for a Hermes running on another computer. This asks the Hermes
itself. Opening a conversation the gateway is currently running
ATTACHES to it, and the dialog says so before Enter is pressed.
Names the dialog calls are patched here, where the calls are made
from -- for example ``hermes_sessions_dialog.hermes_session_catalog``.
"""

from __future__ import annotations

from typing import Optional, cast

import wx

from agent_backends import BACKEND_HERMES
from app_config import REMOTE_HERMES
from conversation_list import make_conversation_list
from dialog_tasks import TaskSlot, _call_after_alive
from hermes_backend import hermes_session_catalog
from screen_speech import announce
from session_history import HistoryEntry, _hermes_db_path, describe_age

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so
# this module does not import the application module).
PAD_DIALOG = 12

# One row in the Hermes sessions list. A conversation that is live in the
# gateway process is marked, because attaching to it is a different act from
# reopening a finished one: the running turn is joined, and its event stream
# moves here.
_HERMES_LIVE_MARK = "Running now"


# Where a Hermes conversation came from, said the way a person would. The
# source matters when picking one: a conversation started in a terminal on the
# server is a different thing from one started in this window.
_HERMES_SOURCE_LABELS = {
    "cli": "terminal",
    "tui": "Hermes TUI",
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "whatsapp": "WhatsApp",
    "signal": "Signal",
    "webhook": "webhook",
    "acp": "editor",
}


def hermes_session_label(entry: dict, live: bool) -> str:
    """The single line a screen reader reads for one Hermes conversation.

    Ordered by what decides the choice: whether it is running, what it is
    about, how long ago it was touched, how much is in it, and where it was
    started. Running first, because that is the one fact that changes what
    opening it will do.
    """
    parts = []
    if live:
        parts.append(_HERMES_LIVE_MARK)
    title = str(entry.get("title") or "").strip()
    preview = " ".join(str(entry.get("preview") or "").split())
    parts.append(title or preview or "(untitled)")
    started = float(entry.get("started_at") or 0)
    if started:
        parts.append(describe_age(started))
    count = int(entry.get("message_count") or 0)
    if count:
        parts.append("1 message" if count == 1 else f"{count} messages")
    source = str(entry.get("source") or "").strip().lower()
    if source:
        parts.append(_HERMES_SOURCE_LABELS.get(source, source))
    return " — ".join(parts)


class HermesSessionsDialog(wx.Dialog):
    """Pick any Hermes conversation — including one that is running right now.

    Recent Conversations reads what this machine has on disk, which is empty
    for a Hermes running on another computer. This asks the Hermes itself, so
    the list holds every conversation it knows: the ones started in this
    window, the ones started in a terminal on that machine, and the ones a
    messaging channel started.

    Conversations the gateway is currently running are marked. Opening one of
    those ATTACHES to it: its running turn is read out here as it happens.
    Hermes keeps one event stream per conversation, so attaching takes that
    stream over from whatever was reading it before — which is why the dialog
    says so rather than letting it be discovered.
    """

    def __init__(self, parent: wx.Window, cwd: str):
        super().__init__(
            parent,
            title="Hermes Conversations",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._cwd = cwd
        self._entries: list[dict] = []
        self._shown: list[dict] = []
        self._live: set[str] = set()
        self.entry: Optional[dict] = None
        self.webview_entry: Optional[HistoryEntry] = None
        self.attaching = False
        self._load_task = TaskSlot(_call_after_alive)
        self._loading = False
        self._load_error = ""
        self._announce_refresh = False

        filter_label = wx.StaticText(self, label="&Filter:")
        self.filter_box = wx.TextCtrl(self)
        self.filter_box.SetName("Filter conversations")
        self.filter_box.SetHint("Type part of a conversation's first message")
        self.filter_box.Bind(wx.EVT_TEXT, lambda _e: self._refresh())

        self.running_only = wx.CheckBox(self, label="Only the ones &running now")
        self.running_only.SetName("Only the ones running now")
        self.running_only.Bind(wx.EVT_CHECKBOX, lambda _e: self._refresh())

        list_label = wx.StaticText(self, label="&Conversations:")
        self.list_box = make_conversation_list(self, name="Conversations")
        self.list_box.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._accept())
        # The consequence of the selected row, spoken on arrow keys: attaching
        # and reopening are different acts and the difference must be heard
        # before Enter, not after.
        self.list_box.Bind(wx.EVT_LISTBOX, lambda _e: self._announce_selection())

        self.summary = wx.StaticText(self, label="")
        self.summary.SetName("Summary")

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        open_button = self.FindWindowById(wx.ID_OK)
        if open_button is not None:
            open_button.SetLabel("&Open")

        pad = self.FromDIP(PAD_DIALOG)
        # The list is what gives the dialog its size; Fit then follows it.
        self.list_box.SetMinSize(self.FromDIP(wx.Size(580, 240)))
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(filter_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.filter_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        sizer.Add(self.running_only, 0, wx.ALL, pad)
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT, pad)
        sizer.Add(self.list_box, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.summary, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)

        self.Bind(wx.EVT_BUTTON, lambda _e: self._accept(), id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self._reload()
        self.filter_box.SetFocus()
        self.CentreOnParent()

    # ----- Loading and filtering -----
    def _reload(self, refreshing: bool = False) -> None:
        """Ask Hermes for its conversations on a worker.

        The dialog opens and stays usable while Hermes is asked. The answer is
        filled in when it arrives, unless a newer request or a closed dialog
        has made it stale.
        """
        self._loading = True
        self._load_error = ""
        self._announce_refresh = refreshing
        self._entries = []
        self._shown = []
        self.list_box.Set([])
        self._set_open_enabled(False)
        asking = "Asking Hermes for its conversations…"
        self.summary.SetLabel(asking)
        announce(asking)

        # Read the remote settings here, on the GUI thread, so the worker only
        # does the talking to Hermes.
        remote_url = REMOTE_HERMES.url()
        remote_token = REMOTE_HERMES.key if remote_url else ""
        remote_credential = REMOTE_HERMES.credential if remote_url else "token"
        remote_username = REMOTE_HERMES.username if remote_url else ""
        cwd = self._cwd

        def work() -> object:
            return hermes_session_catalog(
                cwd,
                remote_url=remote_url,
                remote_token=remote_token,
                remote_credential=remote_credential,
                remote_username=remote_username,
            )

        def loaded(result: object, error: str) -> None:
            if error:
                self._sessions_loaded([], set(), error)
                return
            entries, live, catalog_error = cast(tuple[list[dict], set[str], str], result)
            self._sessions_loaded(entries, live, catalog_error)

        self._load_task.start(work, loaded)

    def _sessions_loaded(self, entries: list[dict], live: set[str], error: str) -> None:
        """Fill the list with the current worker result."""
        if not self:
            return
        self._loading = False
        self._load_error = error
        refreshing = self._announce_refresh
        self._announce_refresh = False
        if error:
            # The reason is spoken, not logged: a blind user cannot glance at a
            # console to find out why the list is empty.
            self._entries = []
            self._live = set()
            self._refresh()
            self._set_open_enabled(False)
            announce(f"Error: {error}")
            return
        self._entries = entries
        self._live = live
        self._refresh()
        announce("Refreshed" if refreshing else self.summary.GetLabel())

    def Destroy(self) -> bool:  # type: ignore[override]
        self._load_task.close()
        return super().Destroy()

    def _is_live(self, entry: dict) -> bool:
        return str(entry.get("id") or "") in self._live

    def _refresh(self) -> None:
        term = self.filter_box.GetValue().strip().lower()
        running_only = self.running_only.GetValue()
        self._shown = [
            entry
            for entry in self._entries
            if (not running_only or self._is_live(entry))
            and (
                not term
                or term in str(entry.get("title") or "").lower()
                or term in str(entry.get("preview") or "").lower()
            )
        ]
        self.list_box.Set(
            [hermes_session_label(entry, self._is_live(entry)) for entry in self._shown]
        )
        if self._shown:
            self.list_box.SetSelection(0)
        count = len(self._shown)
        running = sum(1 for entry in self._shown if self._is_live(entry))
        if self._loading:
            message = "Asking Hermes for its conversations…"
        elif self._load_error:
            message = self._load_error
        elif not self._entries:
            message = "Hermes reported no conversations"
        else:
            message = "1 conversation" if count == 1 else f"{count} conversations"
            if running:
                message += f", {running} running now"
        self.summary.SetLabel(message)
        self._set_open_enabled(bool(self._shown))

    def _set_open_enabled(self, enabled: bool) -> None:
        button = wx.FindWindowById(wx.ID_OK, self)
        if button is not None:
            button.Enable(enabled)

    def _selected(self) -> Optional[dict]:
        index = self.list_box.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._shown):
            return None
        return self._shown[index]

    def _announce_selection(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        if self._is_live(entry):
            announce("Running now. Opening it moves the live turn to this window.")

    # ----- Choosing -----
    def _accept(self) -> None:
        entry = self._selected()
        if entry is None:
            announce("Error: Choose a conversation first")
            return
        self.entry = entry
        self.attaching = self._is_live(entry)
        self.EndModal(wx.ID_OK)

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self._shown:
            # CHAR_HOOK sees Enter before the focused button does. Enter on
            # Cancel has to cancel, not open a conversation.
            if isinstance(self.FindFocus(), wx.Button):
                event.Skip()
                return
            # Enter opens the conversation, the way the Open button does.
            # Ctrl+Enter reads it in the web view instead, and Shift+Enter
            # does the same.
            if event.ControlDown() or event.ShiftDown():
                self._open_in_webview()
                return
            self._accept()
            return
        if key == wx.WXK_F5:
            self._reload(refreshing=True)
            return
        if key == wx.WXK_DOWN and self.filter_box.HasFocus() and self._shown:
            self.list_box.SetFocus()
            self.list_box.SetSelection(0)
            return
        event.Skip()

    def _open_in_webview(self) -> None:
        """Open the selected conversation in an accessible web view (Ctrl+Enter)."""
        entry = self._selected()
        if entry is None:
            announce("Error: Choose a conversation first")
            return
        title = str(entry.get("title") or entry.get("preview") or "Hermes conversation")
        # The catalog carries the session id, not a file: Hermes keeps every
        # conversation in one store, and the id is what identifies one in it.
        self.webview_entry = HistoryEntry(
            backend=BACKEND_HERMES,
            session_id=str(entry.get("id") or ""),
            title=title,
            path=_hermes_db_path().as_posix(),
            modified=float(entry.get("started_at") or 0),
            cwd="",
            folder="",
        )
        self.EndModal(wx.ID_OK)
