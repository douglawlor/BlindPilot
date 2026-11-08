"""/connect: sign opencode in to a provider, or sign it out of one.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Every call to opencode's server happens off the UI thread, and a
result that lands after the dialog or the app has gone is dropped.
_open_web_page came with it: only http and https addresses are
handed to the platform opener, because a sign-in address arrives
over a provider catalog BlindPilot neither controls nor inspects.
Names the dialog calls are patched here, where the calls are made
from -- for example ``connect_dialog.opencode_providers``.
"""

from __future__ import annotations

import threading
import urllib.parse
import webbrowser

import wx

from agent_backends import (
    BACKEND_OPENCODE,
    opencode_auth_methods,
    opencode_connect_api_key,
    opencode_disconnect,
    opencode_oauth_finish,
    opencode_oauth_start,
    opencode_providers,
)
from dialog_tasks import _call_after_alive
from model_dialog import invalidate_model_options
from screen_speech import announce

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12


def _open_web_page(url: str) -> bool:
    """Open a web address, and nothing but a web address.

    Everything else handed to the platform opener is a protocol handler being
    invoked rather than a page being shown: `file:` opens whatever is at that
    path, and Windows gives `ms-msdt:` and its relatives to programs of their
    own. Sign-in addresses arrive over a provider catalog BlindPilot neither
    controls nor inspects, so the scheme is checked rather than trusted.

    False means nothing was opened, for any reason. Every caller says the
    address out loud in that case, so there is still a way through by hand.
    """
    if urllib.parse.urlsplit(url).scheme.casefold() not in ("http", "https"):
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


class ConnectDialog(wx.Dialog):
    """/connect — sign opencode in to a provider, or sign it out of one.

    opencode reaches a model through a provider you have connected, and it can
    reach nearly two hundred of them. This is that list: the ones already
    connected first, so the dialog opens on what is in use, and everything else
    after. Connecting either stores an API key or walks a browser sign-in,
    whichever the provider offers; both are done through opencode's own server,
    so the result is the same as having typed /connect in its terminal.

    Every call to the server happens off the UI thread, because a sign-in can
    take as long as it takes somebody to finish it in a browser, and a dialog
    that stops answering is a dialog a screen reader cannot describe.
    """

    def __init__(self, parent: wx.Window):
        super().__init__(parent, title="Connect a provider to opencode")
        self._providers: list[tuple[str, str]] = []
        self._connected: set[str] = set()
        self._busy = False

        self.status = wx.StaticText(self, label="Reading opencode's provider list…")
        self.status.Wrap(self.FromDIP(520))

        list_label = wx.StaticText(self, label="&Providers:")
        self.list = wx.ListBox(self, choices=[], style=wx.LB_SINGLE)
        self.list.SetName("Providers")
        self.list.SetMinSize(self.FromDIP(wx.Size(420, 260)))

        self.connect_btn = wx.Button(self, label="&Connect…")
        self.disconnect_btn = wx.Button(self, label="&Disconnect")
        close_btn = wx.Button(self, wx.ID_CANCEL, "Close")
        self.connect_btn.Bind(wx.EVT_BUTTON, lambda _e: self._connect())
        self.disconnect_btn.Bind(wx.EVT_BUTTON, lambda _e: self._disconnect())
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._connect())

        pad = self.FromDIP(PAD_DIALOG)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.connect_btn, 0, wx.RIGHT, self.FromDIP(PAD))
        buttons.Add(self.disconnect_btn, 0, wx.RIGHT, self.FromDIP(PAD))
        buttons.Add(close_btn, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.status, 0, wx.ALL, pad)
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT, pad)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, pad)
        self.SetSizerAndFit(sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.list.SetFocus()
        self._refresh()
        self.CentreOnParent()

    # ----- the list -----

    def _refresh(self) -> None:
        self._set_busy(True, "Reading opencode's provider list…")

        def work() -> None:
            providers, connected, error = opencode_providers()
            _call_after_alive(self._show, providers, connected, error)

        threading.Thread(target=work, daemon=True).start()

    def _show(self, providers: list[tuple[str, str]], connected: set, error: str) -> None:
        if not self:  # closed while the list was being read
            return
        # Read what was selected against the list it was selected in: connecting
        # a provider moves it to the top, so the position no longer means what
        # it did, and re-reading it afterwards would land on somebody else.
        selected = self._selected_id()
        self._providers = list(providers)
        self._connected = set(connected)
        self.list.Set(
            [
                f"{name} — connected" if provider_id in self._connected else name
                for provider_id, name in self._providers
            ]
        )
        if self._providers:
            index = next((i for i, (pid, _n) in enumerate(self._providers) if pid == selected), 0)
            self.list.SetSelection(index)
        message = error or (
            f"{len(self._connected)} of {len(self._providers)} providers connected."
        )
        self._set_busy(False, message)

    def _selected_id(self) -> str:
        index = self.list.GetSelection()
        if 0 <= index < len(self._providers):
            return self._providers[index][0]
        return ""

    def _selected_name(self) -> str:
        index = self.list.GetSelection()
        if 0 <= index < len(self._providers):
            return self._providers[index][1]
        return ""

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.connect_btn.Enable(not busy)
        self.disconnect_btn.Enable(not busy)
        self.status.SetLabel(message)
        self.status.Wrap(self.FromDIP(520))
        self.Layout()
        announce(message)

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE and not self._busy:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()

    # ----- connecting -----

    def _ask(self, prompts: object, secret_key: str = "") -> dict | None:
        """Collect whatever a provider needs before it can be signed in to.

        Providers ask for anything from an account id to a self-hosted URL, and
        say so in their own words, so each question is asked as opencode words
        it rather than as something guessed here. Returns None if cancelled.
        """
        answers: dict = {}
        for prompt in prompts if isinstance(prompts, list) else []:
            if not isinstance(prompt, dict):
                continue
            when = prompt.get("when")
            if isinstance(when, dict):
                # Some questions only apply given an earlier answer.
                if answers.get(str(when.get("key"))) != when.get("value"):
                    continue
            key = str(prompt.get("key") or "")
            message = str(prompt.get("message") or key)
            if not key:
                continue
            if prompt.get("type") == "select":
                options = [
                    option for option in (prompt.get("options") or []) if isinstance(option, dict)
                ]
                labels = [
                    " — ".join(
                        part
                        for part in (str(option.get("label") or ""), str(option.get("hint") or ""))
                        if part
                    )
                    for option in options
                ]
                if not options:
                    # A choice with nothing to choose from would be a dialog
                    # with no answer; ask for the value in words instead.
                    prompt = {**prompt, "type": "text"}
                else:
                    with wx.SingleChoiceDialog(self, message, "Connect", labels) as dlg:
                        chosen = dlg.GetSelection() if dlg.ShowModal() == wx.ID_OK else -1
                    if chosen < 0:
                        return None
                    answers[key] = str(options[chosen].get("value") or "")
                    continue
            placeholder = str(prompt.get("placeholder") or "")
            label = f"{message}\n{placeholder}" if placeholder else message
            with wx.TextEntryDialog(self, label, "Connect") as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return None
                answers[key] = dlg.GetValue().strip()
        if secret_key:
            with wx.TextEntryDialog(
                self,
                f"Paste the API key for {self._selected_name()}.\n"
                "It is stored by opencode, not by BlindPilot.",
                "Connect",
                style=wx.TE_PASSWORD | wx.OK | wx.CANCEL,
            ) as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return None
                key = dlg.GetValue().strip()
            if not key:
                return None
            answers[secret_key] = key
        return answers

    def _connect(self) -> None:
        if self._busy:
            return
        provider_id = self._selected_id()
        if not provider_id:
            announce("Choose a provider first.")
            return
        name = self._selected_name()
        self._set_busy(True, f"Asking opencode how {name} can be signed in to…")

        def work() -> None:
            methods = opencode_auth_methods(provider_id)
            _call_after_alive(self._choose_method, provider_id, name, methods)

        threading.Thread(target=work, daemon=True).start()

    def _choose_method(self, provider_id: str, name: str, methods: list[dict]) -> None:
        if not self:
            return
        self._set_busy(False, f"{name}: choose how to sign in.")
        if not methods:
            # The server listed methods, none of them usable. An API key is
            # the one way in every provider has.
            methods = [{"type": "api", "label": "Manually enter API key"}]
        if len(methods) == 1:
            index = 0
        else:
            labels = [str(method.get("label") or method.get("type") or "") for method in methods]
            with wx.SingleChoiceDialog(
                self, f"How do you want to sign in to {name}?", "Connect", labels
            ) as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    self._set_busy(False, "Sign-in cancelled.")
                    return
                index = dlg.GetSelection()
        method = methods[index]
        if str(method.get("type")) == "oauth":
            self._oauth(provider_id, name, index, method)
        else:
            self._api_key(provider_id, name, method)

    def _api_key(self, provider_id: str, name: str, method: dict) -> None:
        answers = self._ask(method.get("prompts"), secret_key="__key__")
        if answers is None:
            self._set_busy(False, "Sign-in cancelled.")
            return
        key = answers.pop("__key__", "")
        self._set_busy(True, f"Connecting {name}…")

        def work() -> None:
            error = opencode_connect_api_key(provider_id, key, answers)
            _call_after_alive(self._finished, name, error, "connected")

        threading.Thread(target=work, daemon=True).start()

    def _oauth(self, provider_id: str, name: str, index: int, method: dict) -> None:
        answers = self._ask(method.get("prompts"))
        if answers is None:
            self._set_busy(False, "Sign-in cancelled.")
            return
        self._set_busy(True, f"Starting the {name} sign-in…")

        def work() -> None:
            authorization, error = opencode_oauth_start(provider_id, index, answers)
            _call_after_alive(self._opened, provider_id, name, index, authorization, error)

        threading.Thread(target=work, daemon=True).start()

    def _opened(
        self, provider_id: str, name: str, index: int, authorization: dict, error: str
    ) -> None:
        if not self:
            return
        if error:
            self._set_busy(False, error)
            return
        url = str(authorization.get("url") or "")
        instructions = str(authorization.get("instructions") or "")
        opened = True
        if url:
            # opencode hands back the address and expects whoever asked to open
            # it. The address is spoken and shown either way, so a machine with
            # no default browser is not left with nothing to go on.
            opened = _open_web_page(url)
            if not opened:
                announce(f"Could not open a browser. The sign-in address is {url}")
        if str(authorization.get("method")) == "code":
            self._set_busy(False, f"Finish signing in to {name} in your browser.")
            with wx.TextEntryDialog(
                self,
                f"{instructions or 'Sign in, then paste the code it gives you.'}\n\n{url}",
                "Connect",
            ) as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    self._set_busy(False, "Sign-in cancelled.")
                    return
                code = dlg.GetValue().strip()
            if not code:
                self._set_busy(False, "Sign-in cancelled. No code was pasted.")
                return
            self._set_busy(True, f"Waiting for {name} to confirm the sign-in…")
        else:
            code = ""
            message = instructions or f"Finish signing in to {name} in your browser."
            if url and not opened:
                message = f"{message} Open this address yourself: {url}"
            elif url:
                message = f"{message} The address is {url}"
            self._set_busy(True, f"{message} Waiting for {name} to confirm it…")

        def work() -> None:
            failure = opencode_oauth_finish(provider_id, index, code)
            _call_after_alive(self._finished, name, failure, "connected")

        threading.Thread(target=work, daemon=True).start()

    def _disconnect(self) -> None:
        if self._busy:
            return
        provider_id = self._selected_id()
        name = self._selected_name()
        if not provider_id:
            announce("Choose a provider first.")
            return
        if provider_id not in self._connected:
            announce(f"{name} is not connected.")
            return
        if (
            wx.MessageBox(
                f"Sign opencode out of {name}?",
                "Disconnect",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                self,
            )
            != wx.YES
        ):
            return
        self._set_busy(True, f"Disconnecting {name}…")

        def work() -> None:
            error = opencode_disconnect(provider_id)
            _call_after_alive(self._finished, name, error, "disconnected")

        threading.Thread(target=work, daemon=True).start()

    def _finished(self, name: str, error: str, what: str) -> None:
        if not self:
            return
        if error:
            self._set_busy(False, error)
            return
        # The model list is drawn from the connected providers, so it has to be
        # re-read before /model is opened again.
        invalidate_model_options(BACKEND_OPENCODE)
        self._set_busy(False, f"{name} {what}. Type /model to pick one of its models.")
        self._refresh()
