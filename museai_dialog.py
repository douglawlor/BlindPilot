"""Native readers for Muse.ai account data and one-action approvals.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Read is the default button; every view gets Refresh and Close, and
action buttons exist only for the views they belong to. Account
calls happen off the UI thread through TaskSlot. Names the dialog
calls are patched here, where the calls are made from.
"""

from __future__ import annotations

from typing import Callable, Optional, cast

import wx

from agent_backends import BACKEND_MUSEAI
from connect_dialog import _open_web_page
from dialog_tasks import TaskSlot, _call_after_alive
from permission_dialog import PermissionDialog
from read_view import ReadView
from screen_speech import announce

# Dialog margin, matching blindpilot_app.PAD_DIALOG (kept local so
# this module does not import the application module).
PAD_DIALOG = 12


class MuseAiDialog(wx.Dialog):
    """Native readers for Muse.ai account data and explicit one-action approvals."""

    def __init__(self, parent: wx.Window, view: str, goal: Optional[dict] = None):
        super().__init__(
            parent,
            title=f"muse.ai {view.title()}",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.view = view
        self.goal = goal
        self.chat_request: Optional[tuple[str, str, str]] = None
        self.items: list[dict] = []
        self._action_task = TaskSlot(_call_after_alive)
        self._busy = False
        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=f"&{view.title()}:"), 0, wx.ALL, pad)
        self.list = wx.ListBox(self)
        self.list.SetName(f"muse.ai {view}")
        self.list.SetMinSize(self.FromDIP(wx.Size(620, 340)))
        self.list.Bind(wx.EVT_LISTBOX, lambda _e: self._selection_changed())
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._read())
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        self.status = wx.StaticText(self, label="")
        sizer.Add(self.status, 0, wx.ALL, pad)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.read = wx.Button(self, label="&Read")
        self.read.Bind(wx.EVT_BUTTON, lambda _e: self._read())
        self.read.SetDefault()
        buttons.Add(self.read, 0, wx.RIGHT, pad)
        self.discuss = self.run = self.links = self.history = self.manage = self.suggestions = (
            self.dismiss
        ) = None
        if view != "approvals":
            self.discuss = wx.Button(self, label="&Discuss in chat")
            self.discuss.Bind(wx.EVT_BUTTON, lambda _e: self._discuss())
            buttons.Add(self.discuss, 0, wx.RIGHT, pad)
        if view in ("feed", "ideas", "schedules", "suggestions"):
            label = "&Accept and run" if view == "suggestions" else "&Run now…"
            self.run = wx.Button(self, label=label)
            self.run.Bind(wx.EVT_BUTTON, lambda _e: self._run())
            buttons.Add(self.run, 0, wx.RIGHT, pad)
        if view == "feed":
            self.links = wx.Button(self, label="Open &link…")
            self.links.Bind(wx.EVT_BUTTON, lambda _e: self._open_link())
            buttons.Add(self.links, 0, wx.RIGHT, pad)
        if view == "schedules":
            self.history = wx.Button(self, label="Run &history")
            self.history.Bind(wx.EVT_BUTTON, lambda _e: self._history())
            buttons.Add(self.history, 0, wx.RIGHT, pad)
        if view == "goals":
            self.manage = wx.Button(self, label="&Manage goal…")
            self.manage.Bind(wx.EVT_BUTTON, lambda _e: self._manage_goal())
            self.suggestions = wx.Button(self, label="&Suggestions…")
            self.suggestions.Bind(wx.EVT_BUTTON, lambda _e: self._suggestions())
            buttons.Add(self.manage, 0, wx.RIGHT, pad)
            buttons.Add(self.suggestions, 0, wx.RIGHT, pad)
        if view == "suggestions":
            self.dismiss = wx.Button(self, label="D&ismiss suggestion")
            self.dismiss.Bind(wx.EVT_BUTTON, lambda _e: self._dismiss())
            buttons.Add(self.dismiss, 0, wx.RIGHT, pad)
        self.decide = wx.Button(self, label="&Decide approval…") if view == "approvals" else None
        if self.decide:
            self.decide.Bind(wx.EVT_BUTTON, lambda _e: self._decide())
            buttons.Add(self.decide, 0, wx.RIGHT, pad)
        self.refresh_button = wx.Button(self, label="Re&fresh")
        self.refresh_button.Bind(wx.EVT_BUTTON, lambda _e: self._refresh())
        buttons.Add(self.refresh_button, 0, wx.RIGHT, pad)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "Close"))
        sizer.Add(buttons, 0, wx.ALL, pad)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self._refresh()
        self.list.SetFocus()
        self.CentreOnParent()

    def _start_action(
        self, label: str, work: Callable[[], object], done: Callable[[object], None]
    ) -> None:
        self._busy = True
        self.status.SetLabel(label)
        announce(label)
        self.list.Enable(False)
        self._selection_changed()

        self._action_task.start(
            work,
            lambda result, error: self._action_finished(label, result, error, done),
        )

    def _action_finished(
        self,
        label: str,
        result: object,
        error: str,
        done: Callable[[object], None],
    ) -> None:
        if not self:
            return
        self._busy = False
        self.list.Enable(True)
        self._selection_changed()
        if error:
            message = f"{label}: {error}"
            self.status.SetLabel(message)
            announce(message)
            return
        done(result)

    def Destroy(self) -> bool:  # type: ignore[override]
        self._action_task.close()
        return super().Destroy()

    def _refresh(self) -> None:
        from museai_backend import museai_items, museai_item_title, museai_goal_suggestions

        self.items = []
        self.list.Clear()

        def load() -> object:
            return (
                museai_goal_suggestions(self.goal)
                if self.view == "suggestions" and self.goal is not None
                else museai_items(self.view)
            )

        def loaded(value: object) -> None:
            self.items = value if isinstance(value, list) else []
            self.list.Set([museai_item_title(self.view, item) for item in self.items])
            if self.items:
                self.list.SetSelection(0)
            message = (
                f"{len(self.items)} {self.view}. Enter reads one."
                if self.items
                else f"No {self.view}."
            )
            self.status.SetLabel(message)
            self._selection_changed()
            announce(message)

        self._start_action(f"Loading muse.ai {self.view}…", load, loaded)

    def _selection_changed(self) -> None:
        if self._busy:
            for button in (
                self.read,
                self.discuss,
                self.run,
                self.links,
                self.history,
                self.manage,
                self.suggestions,
                self.dismiss,
                self.decide,
                self.refresh_button,
            ):
                if button:
                    button.Enable(False)
            return
        selected = self.list.GetSelection()
        valid = 0 <= selected < len(self.items)
        self.read.Enable(valid)
        if self.decide:
            self.decide.Enable(valid and bool(self.items[selected].get("_pending")))
        for button in (self.discuss, self.history, self.manage, self.suggestions):
            if button:
                button.Enable(valid)
        item = self.items[selected] if valid else {}
        if self.run:
            runnable = valid
            if self.view == "feed":
                action = item.get("idea_action")
                runnable = (
                    valid
                    and item.get("origin") == "idea"
                    and isinstance(action, dict)
                    and bool(action.get("idea_id"))
                    and action.get("state") != "built"
                )
            elif self.view == "ideas":
                runnable = valid and item.get("buildStatus") not in (
                    "in_progress",
                    "complete",
                    "success",
                )
            elif self.view == "schedules":
                runnable = valid and item.get("enabled") is not False
            elif self.view == "suggestions":
                runnable = valid and item.get("status") == "suggested"
            self.run.Enable(runnable)
        if self.dismiss:
            self.dismiss.Enable(valid and item.get("status") == "suggested")
        if self.links:
            from museai_backend import museai_item_links

            self.links.Enable(valid and bool(museai_item_links(item)))

    def _selected(self) -> Optional[dict]:
        selected = self.list.GetSelection()
        return self.items[selected] if 0 <= selected < len(self.items) else None

    def _discuss(self) -> None:
        from museai_backend import museai_read_item, museai_item_text, museai_item_title

        item = self._selected()
        if item is None:
            return

        def prepare() -> object:
            read = museai_read_item(self.view, item)
            text = f"I'd like to discuss this muse.ai {self.view} item:\n\n{museai_item_text(self.view, read)}\n\nMy question: "
            return (text, museai_item_title(self.view, read))

        def prepared(value: object) -> None:
            text, title = cast("tuple[str, str]", value)
            self.chat_request = ("draft", text, title)
            self.EndModal(wx.ID_OK)

        self._start_action("Preparing discussion…", prepare, prepared)

    def _open_link(self) -> None:
        from museai_backend import museai_item_links

        item = self._selected()
        urls = museai_item_links(item) if item is not None else []
        if not urls:
            return
        url = urls[0]
        if len(urls) > 1:
            with wx.SingleChoiceDialog(
                self, "Choose a link to open:", "Article links", urls
            ) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                url = urls[dialog.GetSelection()]

        def opened(ok: object) -> None:
            if not ok:
                message = "Could not open this link in your browser."
                self.status.SetLabel(message)
                announce(message)

        self._start_action("Opening link…", lambda: _open_web_page(url), opened)

    def _run(self) -> None:
        from museai_backend import (
            museai_read_item,
            museai_run_idea,
            museai_run_schedule,
            museai_goal_decide,
            _museai_id,
        )

        item = self._selected()
        if item is None or not self.run or not self.run.IsEnabled():
            return

        if self.view == "suggestions":
            if self.goal is None:
                return
            goal = self.goal
            self._start_action(
                "Accepting suggestion…",
                lambda: museai_goal_decide(_museai_id(goal, "goal_id", "id"), item, "accepted"),
                lambda result: self._run_finished(result, item),
            )
        elif self.view == "schedules":
            self._start_action(
                "Running schedule…",
                lambda: museai_run_schedule(item),
                lambda result: self._run_finished(result, item),
            )
        elif self.view == "ideas":
            self._start_action(
                "Loading idea…",
                lambda: museai_read_item(self.view, item),
                self._choose_idea_run,
            )
        else:
            self._start_action(
                "Running action…",
                lambda: museai_run_idea(item, None),
                lambda result: self._run_finished(result, item),
            )

    def _choose_idea_run(self, value: object) -> None:
        from museai_backend import museai_run_idea, museai_item_title

        item = cast(dict, value)
        choices = [
            part
            for part in item.get("items", [])
            if isinstance(part, dict) and part.get("selectable") is not False and part.get("id")
        ]
        parts = None
        if choices:
            with wx.SingleChoiceDialog(
                self,
                "What would you like to run?",
                museai_item_title(self.view, item),
                ["Whole idea", "Choose parts…"],
            ) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                selected = dialog.GetSelection()
            if selected == 1:
                labels = [
                    str((part.get("content") or {}).get("title") or part["id"]) for part in choices
                ]
                with wx.MultiChoiceDialog(
                    self, "Choose parts to run:", "Run selected parts", labels
                ) as dialog:
                    dialog.SetSelections(
                        [i for i, part in enumerate(choices) if part.get("isSelected") is not False]
                    )
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    parts = [str(choices[i]["id"]) for i in dialog.GetSelections()]
        self._start_action(
            "Running idea…",
            lambda: museai_run_idea(item, parts),
            lambda result: self._run_finished(result, item),
        )

    def _run_finished(self, result: object, item: dict) -> None:
        from museai_backend import museai_action_session, museai_item_title

        announce("muse.ai accepted the action.")
        session = museai_action_session(cast(dict, result))
        if session:
            self.chat_request = ("follow", session, museai_item_title(self.view, item))
            self.EndModal(wx.ID_OK)
        elif self.view == "suggestions" or (
            self.view == "schedules" and item.get("execution_target") == "main"
        ):
            self.chat_request = ("main", "", museai_item_title(self.view, item))
            self.EndModal(wx.ID_OK)
        else:
            self._refresh()

    def _history(self) -> None:
        from museai_backend import museai_schedule_history, museai_item_title

        item = self._selected()
        if item is None:
            return

        def show(value: object) -> None:
            text = str(value)
            with ReadView(
                self, text, f"Run history: {museai_item_title(self.view, item)}"
            ) as dialog:
                dialog.ShowModal()

        self._start_action("Loading run history…", lambda: museai_schedule_history(item), show)

    def _suggestions(self) -> None:
        item = self._selected()
        if item is None:
            return
        with MuseAiDialog(self, "suggestions", goal=item) as dialog:
            dialog.ShowModal()
            request = dialog.chat_request
        if request:
            self.chat_request = request
            self.EndModal(wx.ID_OK)
        else:
            self._refresh()

    def _dismiss(self) -> None:
        from museai_backend import museai_goal_decide, _museai_id

        item = self._selected()
        if item is None or self.goal is None or item.get("status") != "suggested":
            return
        goal = self.goal

        def dismissed(_value: object) -> None:
            self._refresh()
            announce("Suggestion dismissed.")

        self._start_action(
            "Dismissing suggestion…",
            lambda: museai_goal_decide(_museai_id(goal, "goal_id", "id"), item, "dismissed"),
            dismissed,
        )

    def _manage_goal(self) -> None:
        from museai_backend import (
            museai_goal_status,
            museai_goal_delete,
            museai_read_item,
        )

        item = self._selected()
        if item is None:
            return
        options = [
            "Edit title and description",
            "Make active",
            "Pause",
            "Mark completed",
            "Retire",
            "Delete goal",
        ]
        with wx.SingleChoiceDialog(self, "Choose an action:", "Manage goal", options) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            choice = dialog.GetSelection()
        if choice == 0:
            self._start_action(
                "Loading goal…",
                lambda: museai_read_item("goals", item),
                self._edit_goal,
            )
            return
        if (
            choice == 5
            and wx.MessageBox(
                f"Delete the goal {item.get('title', '')}?",
                "Delete goal",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                self,
            )
            != wx.YES
        ):
            return
        if choice == 5:
            self._start_action(
                "Updating goal…", lambda: museai_goal_delete(item), self._goal_updated
            )
        else:
            status = ["", "active", "paused", "completed", "retired"][choice]
            self._start_action(
                "Updating goal…",
                lambda: museai_goal_status(item, status),
                self._goal_updated,
            )

    def _edit_goal(self, value: object) -> None:
        from museai_backend import museai_goal_edit

        item = cast(dict, value)
        with wx.TextEntryDialog(
            self, "Goal title:", "Edit goal", str(item.get("title", ""))
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            title = dialog.GetValue()
        with wx.TextEntryDialog(
            self,
            "Goal description:",
            "Edit goal",
            str(item.get("description", "")),
            style=wx.OK | wx.CANCEL | wx.TE_MULTILINE,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            description = dialog.GetValue()
        self._start_action(
            "Saving goal…",
            lambda: museai_goal_edit(item, title, description),
            self._goal_updated,
        )

    def _goal_updated(self, _value: object) -> None:
        self._refresh()
        announce("Goal updated.")

    def _read(self) -> None:
        from museai_backend import museai_read_item, museai_item_text, museai_item_title

        selected = self.list.GetSelection()
        if not 0 <= selected < len(self.items):
            return

        source = self.items[selected]

        def show(value: object) -> None:
            item = cast(dict, value)
            with ReadView(
                self, museai_item_text(self.view, item), museai_item_title(self.view, item)
            ) as dialog:
                dialog.ShowModal()

        self._start_action("Reading item…", lambda: museai_read_item(self.view, source), show)

    def _decide(self) -> None:
        from museai_backend import museai_decide, museai_item_text, museai_allowed_decisions

        selected = self.list.GetSelection()
        if not 0 <= selected < len(self.items) or not self.items[selected].get("_pending"):
            return
        item = self.items[selected]
        try:
            allowed = museai_allowed_decisions(item)
            request = museai_item_text(self.view, item)
        except ValueError as exc:
            announce(str(exc))
            return
        dialog = PermissionDialog(
            self,
            BACKEND_MUSEAI,
            "Perform an action",
            {"Request": request},
            False,
        )
        dialog.FindWindow(PermissionDialog.ALLOW).SetLabel("&Allow once")
        dialog.FindWindow(PermissionDialog.ALLOW).Enable("allow_once" in allowed)
        dialog.FindWindow(PermissionDialog.DENY).Enable("deny" in allowed)
        try:
            choice = dialog.ShowModal()
            reason = dialog.reason()
        finally:
            dialog.Destroy()
        decision = "allow_once" if choice == PermissionDialog.ALLOW else "deny"
        if decision not in allowed:
            announce("This approval needs a different choice in the muse.ai app.")
            return

        def decided(_value: object) -> None:
            announce("Allowed once" if decision == "allow_once" else "Denied")
            self._refresh()

        self._start_action(
            "Sending approval decision…",
            lambda: museai_decide(str(item["approval_id"]), decision, reason),
            decided,
        )

    def _on_key(self, event: wx.KeyEvent) -> None:
        if (
            event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
            and self.FindFocus() is self.list
        ):
            self._read()
            return
        event.Skip()
