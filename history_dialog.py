"""Recent Conversations: pick a past conversation to reopen.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

When a conversation store cannot be read, this says so instead of
showing an empty list, names the program and the problem, and says
what to do about it; the conversations that could be read are still
listed. Names the dialog calls are patched here, where the calls
are made from -- for example ``history_dialog.list_history``.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import List, Optional

import wx

from agent_backends import BACKEND_IDS, BACKEND_LABELS, backend_label, normalize_backend
from app_config import _load_config, _save_config
from conversation_list import make_conversation_list
from dialog_tasks import _call_after_alive
from history_status import history_diagnostics
from screen_speech import announce
from session_history import HistoryEntry, HistoryTurn, describe_age, list_history, load_turns

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12

# Said when a settings write failed, in place of the sentence that would claim
# a change the file did not keep.
_SAVE_FAILED = "Error: could not save settings"


def _save_or_announce(cfg: dict) -> bool:
    """Write the settings, and say so when the file refuses them.

    The bare call sites used to leave the user being told something the
    file never took; this is that guard in one place.
    """
    if _save_config(cfg):
        return True
    announce(_SAVE_FAILED)
    return False


# The two scopes the history picker can list, in the order they are offered.
_HISTORY_SCOPES = ("folder", "all")


_HISTORY_SCOPE_LABELS = ("This folder", "All folders")


# Orders Recent Conversations can be listed in; the first is the default.
_HISTORY_SORTS = ("Newest first", "Oldest first", "By title", "By folder")


# "All backends" sits first in the backend list; the rest follow BACKEND_IDS.
_HISTORY_ANY_BACKEND = "All backends"


_HISTORY_ANY_GROUP = "All groups"


def _history_key(entry: HistoryEntry) -> str:
    """How a conversation is known in BlindPilot's own names and hidden list."""
    return f"{entry.backend}:{entry.session_id}"


def sort_history(entries: List[HistoryEntry], order: str, title) -> List[HistoryEntry]:
    """`entries` in one of `_HISTORY_SORTS`, newest first within a tie."""
    newest = sorted(entries, key=lambda e: e.modified, reverse=True)
    if order == "Oldest first":
        return newest[::-1]
    if order == "By title":
        return sorted(newest, key=lambda e: title(e).casefold())
    if order == "By folder":
        return sorted(newest, key=lambda e: (e.folder or e.cwd).casefold())
    return newest


def conversation_groups(cfg: dict) -> dict[str, list[str]]:
    """BlindPilot's own groups: a conversation's history key -> its group names."""
    saved = cfg.get("conversation_groups")
    if not isinstance(saved, dict):
        return {}
    return {
        str(key): [str(name) for name in names if str(name).strip()]
        for key, names in saved.items()
        if isinstance(names, list) and names
    }


def group_names(groups: dict[str, list[str]]) -> list[str]:
    """Every group in use, alphabetically. A group nothing is in is gone."""
    return sorted({name for names in groups.values() for name in names}, key=str.casefold)


def _last_exchange(turns: List[HistoryTurn], limit: int = 1200) -> str:
    """The last prompt and answer of a conversation, cut short for a preview."""

    def cut(text: str) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"

    for turn in reversed(turns):
        if turn.prompt.strip() or turn.response.strip():
            parts = []
            if turn.prompt.strip():
                parts.append(f"You: {cut(turn.prompt)}")
            if turn.response.strip():
                parts.append(f"Answer: {cut(turn.response)}")
            return "\n\n".join(parts)
    return "Nothing to show: this conversation has no messages BlindPilot can read."


class HistoryDialog(wx.Dialog):
    """Recent Conversations: pick a past conversation and carry on with it.

    The list is every conversation the chosen backend has stored, newest first,
    each one named by the message that started it — which is the only thing
    that reliably tells two of them apart when they are read out. Typing in the
    filter narrows the list by title; the backend and folder pickers widen it.

    Enter opens the selected conversation in a new tab, the way the Open button
    does. Ctrl+Enter (or Shift+Enter) reads it in an accessible web view
    instead. Esc cancels.
    """

    def __init__(self, parent: wx.Window, backend: str, cwd: str):
        super().__init__(
            parent,
            title="Recent Conversations",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._cwd = cwd
        self._entries: List[HistoryEntry] = []
        self._shown: List[HistoryEntry] = []
        self.entry: Optional[HistoryEntry] = None
        self.webview_entry: Optional[HistoryEntry] = None
        self._load_serial = 0
        self._loading = False
        self._load_error = ""
        self._source_warning = ""

        backend_label_text = wx.StaticText(self, label="&Backend:")
        self._backend_values = [""] + list(BACKEND_IDS)
        self.backend_picker = wx.Choice(
            self,
            choices=[_HISTORY_ANY_BACKEND] + [BACKEND_LABELS[b] for b in BACKEND_IDS],
        )
        self.backend_picker.SetName("Backend")
        self.backend_picker.SetSelection(self._backend_values.index(normalize_backend(backend)))
        self.backend_picker.Bind(wx.EVT_CHOICE, lambda _e: self._reload())

        scope_label = wx.StaticText(self, label="&Show:")
        self.scope_picker = wx.Choice(self, choices=list(_HISTORY_SCOPE_LABELS))
        self.scope_picker.SetName("Show")
        # Every folder, unless the user has said otherwise. A conversation is
        # filed under the directory it ran in, and which directory that is
        # depends on where the application was started from: the source tree
        # when it is run from source, the install folder otherwise. Scoping
        # the list to one of those hid conversations the user knew they had.
        saved_scope = _load_config().get("history_scope")
        self.scope_picker.SetSelection(
            _HISTORY_SCOPES.index(saved_scope)
            if saved_scope in _HISTORY_SCOPES
            else _HISTORY_SCOPES.index("all")
        )
        self.scope_picker.Bind(wx.EVT_CHOICE, lambda _e: self._on_scope())

        sort_label = wx.StaticText(self, label="So&rt:")
        self.sort_picker = wx.Choice(self, choices=list(_HISTORY_SORTS))
        self.sort_picker.SetName("Sort")
        saved_sort = _load_config().get("history_sort")
        self.sort_picker.SetSelection(
            _HISTORY_SORTS.index(saved_sort) if saved_sort in _HISTORY_SORTS else 0
        )
        self.sort_picker.Bind(wx.EVT_CHOICE, lambda _e: self._on_sort())
        group_label = wx.StaticText(self, label="Gro&up:")
        self.group_picker = wx.Choice(self, choices=[_HISTORY_ANY_GROUP])
        self.group_picker.SetName("Group")
        self.group_picker.SetSelection(0)
        self.group_picker.Bind(wx.EVT_CHOICE, lambda _e: self._refresh())
        self.show_hidden = wx.CheckBox(self, label="Show &hidden conversations")
        self.show_hidden.Bind(wx.EVT_CHECKBOX, lambda _e: self._refresh())

        filter_label = wx.StaticText(self, label="&Filter:")
        self.filter_box = wx.TextCtrl(self)
        self.filter_box.SetName("Filter conversations")
        self.filter_box.SetHint("Type part of a conversation's first message")
        self.filter_box.Bind(wx.EVT_TEXT, lambda _e: self._refresh())

        list_label = wx.StaticText(self, label="&Conversations:")
        self.list_box = make_conversation_list(self, name="Conversations")
        self.list_box.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._accept())
        self.list_box.Bind(wx.EVT_LISTBOX, lambda _e: self._schedule_preview())

        # How the selected conversation ended, to tell apart ones that began
        # alike. Read in the background, a moment after the selection settles.
        preview_label = wx.StaticText(self, label="Las&t exchange:")
        self.preview = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2, name="Last exchange"
        )
        self._preview_for: Optional[HistoryEntry] = None
        self._preview_timer = wx.CallLater(300, self._load_preview)
        self._preview_timer.Stop()

        self.summary = wx.StaticText(self, label="")
        self.summary.SetName("Summary")

        self.source_status = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, name="History source status"
        )
        self.source_status.SetMinSize(self.FromDIP(wx.Size(560, 85)))
        self.source_status.Hide()

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        open_button = self.FindWindow(wx.ID_OK)
        if open_button is not None:
            open_button.SetLabel("&Open")
        reload_button = wx.Button(self, label="&Reload")
        reload_button.Bind(wx.EVT_BUTTON, lambda _e: self._reload())
        buttons.Insert(0, reload_button, 0, wx.RIGHT, self.FromDIP(PAD))
        # Names and hiding are BlindPilot's own: no backend's files change.
        rename_button = wx.Button(self, label="Rena&me…")
        rename_button.Bind(wx.EVT_BUTTON, lambda _e: self._rename())
        self.hide_button = wx.Button(self, label="Hi&de")
        self.hide_button.Bind(wx.EVT_BUTTON, lambda _e: self._toggle_hidden())
        buttons.Insert(0, rename_button, 0, wx.RIGHT, self.FromDIP(PAD))
        buttons.Insert(1, self.hide_button, 0, wx.RIGHT, self.FromDIP(PAD))
        groups_button = wx.Button(self, label="&Groups…")
        groups_button.Bind(wx.EVT_BUTTON, lambda _e: self._edit_groups())
        buttons.Insert(2, groups_button, 0, wx.RIGHT, self.FromDIP(PAD))

        pad = self.FromDIP(PAD_DIALOG)
        # Rows 0: as many as there are pickers. Fixing it at 2 made wx refuse
        # the third (Sort) with an assertion, so the dialog never opened.
        pickers = wx.FlexGridSizer(0, 2, self.FromDIP(PAD), self.FromDIP(PAD))
        pickers.AddGrowableCol(1, 1)
        pickers.Add(backend_label_text, 0, wx.ALIGN_CENTER_VERTICAL)
        pickers.Add(self.backend_picker, 1, wx.EXPAND)
        pickers.Add(scope_label, 0, wx.ALIGN_CENTER_VERTICAL)
        pickers.Add(self.scope_picker, 1, wx.EXPAND)
        pickers.Add(sort_label, 0, wx.ALIGN_CENTER_VERTICAL)
        pickers.Add(self.sort_picker, 1, wx.EXPAND)
        pickers.Add(group_label, 0, wx.ALIGN_CENTER_VERTICAL)
        pickers.Add(self.group_picker, 1, wx.EXPAND)
        # The list is what gives the dialog its size; Fit then follows it.
        self.list_box.SetMinSize(self.FromDIP(wx.Size(560, 220)))

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(pickers, 0, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.show_hidden, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        sizer.Add(filter_label, 0, wx.LEFT | wx.RIGHT, pad)
        sizer.Add(self.filter_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.list_box, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.summary, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        sizer.Add(self.source_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        self.preview.SetMinSize(self.FromDIP(wx.Size(560, 100)))
        sizer.Add(preview_label, 0, wx.LEFT | wx.RIGHT, pad)
        sizer.Add(self.preview, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)

        self.Bind(wx.EVT_BUTTON, lambda _e: self._accept(), id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self._reload()
        self.filter_box.SetFocus()
        self.CentreOnParent()

    # ----- Loading and filtering -----
    def _selected_backend(self) -> Optional[str]:
        value = self._backend_values[max(0, self.backend_picker.GetSelection())]
        return value or None

    def _selected_cwd(self) -> Optional[str]:
        scope = _HISTORY_SCOPES[max(0, self.scope_picker.GetSelection())]
        return self._cwd if scope == "folder" else None

    def _reload(self) -> None:
        """Re-scan the history stores for the chosen backend and scope."""
        backend, cwd = self._selected_backend(), self._selected_cwd()
        self._load_serial += 1
        serial = self._load_serial
        self._loading = True
        self._load_error = ""
        self._source_warning = ""
        self.source_status.ChangeValue("")
        self.source_status.Hide()
        self.Layout()
        self._entries = []
        self._refresh()

        def work() -> None:
            try:
                with history_diagnostics() as issues:
                    entries = list_history(backend, cwd)
                error = ""
                warning = "\n".join(issue.message for issue in issues)
            except Exception:
                entries, error, warning = (
                    [],
                    "Could not load conversations. Select Reload to try again.",
                    "",
                )
            _call_after_alive(self._history_loaded, serial, entries, error, warning)

        threading.Thread(target=work, daemon=True).start()

    def _history_loaded(
        self, serial: int, entries: List[HistoryEntry], error: str, warning: str = ""
    ) -> None:
        if not self or serial != self._load_serial:
            return
        self._loading = False
        self._load_error = error
        self._source_warning = warning
        self.source_status.ChangeValue(error or warning)
        self.source_status.Show(bool(error or warning))
        self.Layout()
        self._entries = entries
        self._refresh()
        announce(
            f"Error: {error}"
            if error
            else self.summary.GetLabel() + (f" {warning}" if warning else "")
        )

    def Destroy(self) -> bool:  # type: ignore[override]
        # Cocoa may defer native destruction past an already queued callback.
        self._load_serial += 1
        self._preview_timer.Stop()
        self._preview_for = None
        return super().Destroy()

    def _read_own_names(self) -> None:
        """Names and hidden list from config, once per refresh, not per row."""
        cfg = _load_config()
        names = cfg.get("conversation_names")
        hidden = cfg.get("hidden_conversations")
        self._names = dict(names) if isinstance(names, dict) else {}
        self._hidden_keys = set(hidden) if isinstance(hidden, list) else set()
        self._groups = conversation_groups(cfg)

    def _title(self, entry: HistoryEntry) -> str:
        """The name you gave it here, or the one the backend has."""
        if not hasattr(self, "_names"):
            self._read_own_names()
        return str(self._names.get(_history_key(entry)) or entry.title)

    def _hidden(self) -> set:
        if not hasattr(self, "_hidden_keys"):
            self._read_own_names()
        return self._hidden_keys

    def _label_for(self, entry: HistoryEntry) -> str:
        parts = [self._title(entry) or "(untitled)", describe_age(entry.modified)]
        if not entry.resumable:
            parts.append("FreeBuff Desktop, read only")
        if _history_key(entry) in self._hidden():
            parts.append("hidden")
        groups = getattr(self, "_groups", {}).get(_history_key(entry))
        if groups:
            parts.append(("group " if len(groups) == 1 else "groups ") + ", ".join(groups))
        if self._selected_cwd() is None and entry.folder:
            parts.append(entry.folder)
        if self._selected_backend() is None:
            parts.append(backend_label(entry.backend))
        return " — ".join(parts)

    def _refresh(self, keep: Optional[HistoryEntry] = None) -> None:
        self._read_own_names()
        term = self.filter_box.GetValue().strip().lower()
        hidden = self._hidden() if not self.show_hidden.GetValue() else set()
        group = self._sync_group_picker()
        matching = [
            entry
            for entry in self._entries
            if (not term or term in self._title(entry).lower())
            and _history_key(entry) not in hidden
            and (not group or group in self._groups.get(_history_key(entry), []))
        ]
        order = _HISTORY_SORTS[max(0, self.sort_picker.GetSelection())]
        self._shown = sort_history(matching, order, self._title)
        self.list_box.Set([self._label_for(entry) for entry in self._shown])
        if self._shown:
            self.list_box.SetSelection(self._shown.index(keep) if keep in self._shown else 0)
        count = len(self._shown)
        if self._loading:
            message = "Loading conversations…"
        elif self._load_error:
            message = self._load_error
        elif not self._entries:
            message = (
                "No conversations available from the sources that could be read"
                if self._source_warning
                else "No past conversations found here"
            )
        elif count == 1:
            message = "1 conversation"
        else:
            message = f"{count} conversations"
        if count and not self._loading and not self._load_error:
            # Enter changed what it does here, and a list that says only how
            # many conversations it holds does not say what to do with one.
            message += ". Enter opens it in a new tab, Ctrl+Enter reads it in a web view"
        if self._source_warning:
            message += ". History source status has details. Select Reload to try again."
        self.summary.SetLabel(message)
        self._set_open_enabled(bool(self._shown))
        self._schedule_preview()

    # ----- Last exchange -----
    def _schedule_preview(self) -> None:
        selection = self.list_box.GetSelection()
        entry = self._shown[selection] if 0 <= selection < len(self._shown) else None
        if entry == self._preview_for:
            return
        self._preview_timer.Stop()
        self._preview_for = entry
        self.preview.ChangeValue("Reading…" if entry else "")
        if entry is not None:
            self._preview_timer.Start(300)

    def _load_preview(self) -> None:
        if not self:
            return
        entry = self._preview_for
        if entry is None:
            return

        def work() -> None:
            try:
                with history_diagnostics() as issues:
                    text = _last_exchange(load_turns(entry))
                if issues:
                    warning = "\n".join(issue.message for issue in issues)
                    text = f"{warning}\n\n{text}" if text else warning
            except Exception:
                text = "Could not read this conversation."
            _call_after_alive(self._show_preview, entry, text)

        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, entry: HistoryEntry, text: str) -> None:
        # Arrowing on while it was read leaves a newer entry wanted; and the
        # dialog may be gone.
        if self and entry == self._preview_for:
            self.preview.ChangeValue(text)

    def _set_open_enabled(self, enabled: bool) -> None:
        button = self.FindWindow(wx.ID_OK)
        if button is not None:
            button.Enable(enabled)

    # ----- Choosing -----
    def _accept(self) -> None:
        selection = self.list_box.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self._shown):
            announce("Error: Choose a conversation first")
            return
        chosen = self._shown[selection]
        # Opened under the name given here, so the tab says it too.
        self.entry = replace(chosen, title=self._title(chosen))
        self.EndModal(wx.ID_OK)

    def _current(self) -> Optional[HistoryEntry]:
        selection = self.list_box.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self._shown):
            announce("Error: Choose a conversation first")
            return None
        return self._shown[selection]

    def _on_sort(self) -> None:
        cfg = _load_config()
        cfg["history_sort"] = _HISTORY_SORTS[max(0, self.sort_picker.GetSelection())]
        _save_or_announce(cfg)
        self._refresh(self._current() if self._shown else None)

    def _on_scope(self) -> None:
        """Keep the folder scope the user chose, the way the sort is kept."""
        cfg = _load_config()
        cfg["history_scope"] = _HISTORY_SCOPES[max(0, self.scope_picker.GetSelection())]
        _save_or_announce(cfg)
        self._reload()

    def _rename(self) -> None:
        """F2: a name for this conversation in BlindPilot. Blank goes back."""
        entry = self._current()
        if entry is None:
            return
        with wx.TextEntryDialog(
            self, "Name (leave blank for the original):", "Rename", self._title(entry)
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.GetValue().strip()
        cfg = _load_config()
        names = cfg.get("conversation_names")
        names = dict(names) if isinstance(names, dict) else {}
        if name and name != entry.title:
            names[_history_key(entry)] = name
        else:
            names.pop(_history_key(entry), None)
        cfg["conversation_names"] = names
        if not _save_config(cfg):
            # The file kept its old names, so the list stays as it is: a
            # refresh would re-read them and say the rename worked.
            announce(_SAVE_FAILED)
            self.list_box.SetFocus()
            return
        self._refresh(entry)
        announce(f"Renamed to {self._title(entry)}")
        self.list_box.SetFocus()

    def _sync_group_picker(self) -> str:
        """Offer every group in use, keeping the one chosen; return it, or ""."""
        chosen = self.group_picker.GetStringSelection()
        names = group_names(self._groups)
        wanted = [_HISTORY_ANY_GROUP, *names]
        if list(self.group_picker.GetStrings()) != wanted:
            self.group_picker.Set(wanted)
        self.group_picker.SetSelection(wanted.index(chosen) if chosen in wanted else 0)
        return chosen if chosen in names else ""

    def _edit_groups(self) -> None:
        """Ctrl+G: put this conversation in groups, or take it out of them."""
        entry = self._current()
        if entry is None:
            return
        key = _history_key(entry)
        names = group_names(self._groups)
        current = self._groups.get(key, [])
        new_group = "New group…"
        if names:
            with wx.MultiChoiceDialog(
                self,
                f"Groups for {self._title(entry)}. Check every group it belongs in.",
                "Groups",
                [*names, new_group],
            ) as dlg:
                dlg.SetSelections([names.index(name) for name in current if name in names])
                if dlg.ShowModal() != wx.ID_OK:
                    return
                picked = [([*names, new_group])[index] for index in dlg.GetSelections()]
        else:
            picked = [new_group]
        if new_group in picked:
            picked.remove(new_group)
            with wx.TextEntryDialog(self, "Name of the new group:", "New Group") as ask:
                if ask.ShowModal() != wx.ID_OK:
                    return
                name = " ".join(ask.GetValue().split())
            if name and name not in picked:
                picked.append(name)
        cfg = _load_config()
        groups = conversation_groups(cfg)
        if picked:
            groups[key] = picked
        else:
            groups.pop(key, None)
        cfg["conversation_groups"] = groups
        if not _save_config(cfg):
            announce(_SAVE_FAILED)
            self.list_box.SetFocus()
            return
        self._refresh(entry)
        announce(
            f"In {'groups' if len(picked) > 1 else 'group'} {', '.join(picked)}"
            if picked
            else "In no group"
        )
        self.list_box.SetFocus()

    def _toggle_hidden(self) -> None:
        """Delete: hide this conversation from the list, or bring it back."""
        entry = self._current()
        if entry is None:
            return
        hidden = self._hidden()
        key = _history_key(entry)
        cfg = _load_config()
        if key in hidden:
            hidden.discard(key)
            said = "Brought back"
        else:
            hidden.add(key)
            said = "Hidden. Show hidden conversations lists it again"
        cfg["hidden_conversations"] = sorted(hidden)
        if not _save_config(cfg):
            # The in-memory set is the one the list reads, so it goes back the
            # way it was rather than hiding a conversation the file does not.
            if key in hidden:
                hidden.discard(key)
            else:
                hidden.add(key)
            announce(_SAVE_FAILED)
            self.list_box.SetFocus()
            return
        selection = self.list_box.GetSelection()
        self._refresh(entry)
        if entry not in self._shown and self._shown:
            self.list_box.SetSelection(min(selection, len(self._shown) - 1))
        announce(said)
        self.list_box.SetFocus()

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self._shown:
            # CHAR_HOOK fires before the focused control sees the key, so this
            # has to hand Enter back when a button is focused. Otherwise Enter
            # on Cancel - the ordinary way to leave a dialog, and the only way
            # for somebody who cannot see that focus has moved - opened a
            # conversation instead. The dialog closed either way, which is what
            # made it hard to notice.
            focus = self.FindFocus()
            if isinstance(focus, wx.Button) or (
                focus is not None and focus is getattr(self, "source_status", None)
            ):
                event.Skip()
                return
            # Enter opens the conversation, the way the Open button does.
            # Ctrl+Enter reads it in the web view instead, and Shift+Enter does
            # the same, the way it opens an answer as a page. All three work
            # from the filter box too, where the conversation is the first
            # match.
            if event.ControlDown() or event.ShiftDown():
                self._open_in_webview()
                return
            self._accept()
            return
        if self.list_box.HasFocus() and key == wx.WXK_F2:
            self._rename()
            return
        if self.list_box.HasFocus() and key == wx.WXK_DELETE:
            self._toggle_hidden()
            return
        if self.list_box.HasFocus() and key == ord("G") and event.ControlDown():
            self._edit_groups()
            return
        # Down from the filter box drops straight into the list, so a filter
        # can be typed and its first result reached without hunting for Tab.
        if key == wx.WXK_DOWN and self.filter_box.HasFocus() and self._shown:
            self.list_box.SetFocus()
            self.list_box.SetSelection(0)
            return
        event.Skip()

    def _open_in_webview(self) -> None:
        """Return the selected conversation for a reading tab (Ctrl+Enter)."""
        entry = self._current()
        if entry is None:
            return
        self.webview_entry = entry
        self.EndModal(wx.ID_OK)
