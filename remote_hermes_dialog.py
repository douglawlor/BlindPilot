"""Where a Hermes on another computer lives, and how to prove it.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the dialog's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Deliberately plain: a checkbox, an address, a port, a TLS switch, the
credential type with its username and key, and a button that tries
it. Every field has a label of its own, and the test button reports
its result in the dialog's status line rather than a message box.
Fields start from the saved settings in app_config.
"""

from __future__ import annotations

import threading
import time

import wx

from app_config import REMOTE_HERMES
from hermes_backend import remote_ws_url
from screen_speech import announce

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12


class RemoteHermesDialog(wx.Dialog):
    """Where a Hermes on another computer lives, and how to prove who we are.

    Deliberately plain: a checkbox, an address, a port, a TLS switch, the
    credential type with its username and key, and a button that tries it.
    Every field has a label of its own so a screen reader
    announces what it is on the way in, and the test button reports its result
    in the same status line rather than a message box that has to be dismissed.
    """

    def __init__(self, parent: wx.Window):
        super().__init__(parent, title="Remote Hermes", style=wx.DEFAULT_DIALOG_STYLE)
        intro = wx.StaticText(
            self,
            label=(
                "Off runs the Hermes installed on this computer. On connects to a "
                "Hermes elsewhere; start it there with 'hermes serve'."
            ),
        )
        intro.Wrap(self.FromDIP(520))

        # Tooltips carry the same explanations the labels, hints and the intro
        # already give a screen reader, for whoever hovers instead.
        self._enabled = wx.CheckBox(self, label="&Use a Hermes on another computer")
        self._enabled.SetValue(REMOTE_HERMES.enabled)
        self._enabled.SetToolTip(
            "Off runs the Hermes installed on this computer. On connects to a Hermes elsewhere."
        )

        host_label = wx.StaticText(self, label="Computer &name or address:")
        self._host = wx.TextCtrl(self, value=REMOTE_HERMES.host)
        self._host.SetHint("for example: my-server, or 100.64.0.5")
        self._host.SetToolTip(
            "The computer running hermes serve, for example: my-server, or 100.64.0.5"
        )

        port_label = wx.StaticText(self, label="&Port:")
        self._port = wx.SpinCtrl(self, min=1, max=65535, initial=REMOTE_HERMES.port)
        self._port.SetToolTip("The port hermes serve listens on")

        self._secure = wx.CheckBox(self, label="Connect over &TLS (wss)")
        self._secure.SetValue(REMOTE_HERMES.secure)
        self._secure.SetToolTip("Connect over TLS (wss) rather than plain ws")

        credential_label = wx.StaticText(self, label="Sign in &with:")
        self._credential = wx.Choice(
            self,
            choices=[
                "Session token (same computer)",
                "Username and password (another computer)",
            ],
        )
        self._credential.SetSelection(0 if REMOTE_HERMES.credential == "token" else 1)
        self._credential.Bind(wx.EVT_CHOICE, lambda _e: self._sync_credential_fields())
        self._credential.SetToolTip(
            "Sign in with a session token from the same computer, "
            "or a username and password from another computer"
        )

        user_label = wx.StaticText(self, label="&Username:")
        self._user = wx.TextCtrl(self, value=REMOTE_HERMES.username)
        self._user.SetToolTip("Only needed when signing in with a password")

        key_label = wx.StaticText(self, label="&Key or password:")
        # Not masked: a screen-reader user has to be able to review what was
        # pasted, and a session token is a machine-generated string nobody
        # types from memory. It is stored in a file of its own either way.
        self._key = wx.TextCtrl(self, value=REMOTE_HERMES.key)
        self._key.SetToolTip("The session token, or the password for the username above")

        self._status = wx.StaticText(self, label=REMOTE_HERMES.describe())
        self._status.Wrap(self.FromDIP(520))

        self._test_btn = wx.Button(self, label="&Test connection")
        self._test_btn.Bind(wx.EVT_BUTTON, lambda _e: self._test())
        self._test_btn.SetToolTip(
            "Try the address now, so a mistake is found here and not mid-turn"
        )

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        grid = wx.FlexGridSizer(cols=2, vgap=self.FromDIP(PAD), hgap=self.FromDIP(PAD))
        grid.AddGrowableCol(1, 1)
        for label, control in (
            (host_label, self._host),
            (port_label, self._port),
            (credential_label, self._credential),
            (user_label, self._user),
            (key_label, self._key),
        ):
            grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)

        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(intro, 0, wx.ALL, pad)
        sizer.Add(self._enabled, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        sizer.Add(self._secure, 0, wx.ALL, pad)
        sizer.Add(self._test_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        sizer.Add(self._status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)
        self._sync_credential_fields()
        self._host.SetFocus()
        self.CentreOnParent()

    def _credential_name(self) -> str:
        return "token" if self._credential.GetSelection() == 0 else "password"

    def _sync_credential_fields(self) -> None:
        """A username only means something when signing in with a password."""
        wants_user = self._credential_name() == "password"
        self._user.Enable(wants_user)

    def _say(self, text: str) -> None:
        self._status.SetLabel(text)
        self._status.Wrap(self.FromDIP(520))
        self.Layout()
        announce(text)

    def _test(self) -> None:
        """Try the address now, so a mistake is found here and not mid-turn."""
        host = self._host.GetValue().strip()
        if not host:
            self._say("Enter the computer's name or address first.")
            self._host.SetFocus()
            return
        url = remote_ws_url(host, int(self._port.GetValue()), self._secure.GetValue())
        self._test_btn.Disable()
        self._say(f"Trying {url}…")
        key = self._key.GetValue().strip()
        credential = self._credential_name()
        username = self._user.GetValue().strip()
        threading.Thread(
            target=self._run_test, args=(url, key, credential, username), daemon=True
        ).start()

    def _run_test(self, url: str, key: str, credential: str, username: str) -> None:
        from hermes_backend import WebSocketTransport

        transport = WebSocketTransport(url, key, credential, username)
        try:
            transport.start()
        except OSError as exc:
            wx.CallAfter(self._test_done, str(exc))
            return
        # Connecting proves the address and the key. Waiting for Hermes to
        # announce itself proves the far end really is a Hermes gateway, which
        # is the difference between a wrong port and a wrong program.
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                frame = transport.receive(0.5)
                if frame is None:
                    continue
                params = frame.get("params")
                if isinstance(params, dict) and params.get("type") == "gateway.ready":
                    wx.CallAfter(self._test_done, "")
                    return
            wx.CallAfter(
                self._test_done,
                f"Connected to {url}, but it did not identify itself as Hermes.",
            )
        finally:
            transport.close()

    def _test_done(self, error: str) -> None:
        if not self:
            # Escape while the test ran. The dialog is gone.
            return
        self._test_btn.Enable()
        if error:
            self._say(error)
        else:
            self._say("Hermes answered. This address and key work.")

    def apply(self) -> bool:
        """Copy the fields into the saved settings. False if they were not written."""
        REMOTE_HERMES.enabled = self._enabled.GetValue()
        REMOTE_HERMES.host = self._host.GetValue().strip()
        REMOTE_HERMES.port = int(self._port.GetValue())
        REMOTE_HERMES.secure = self._secure.GetValue()
        REMOTE_HERMES.credential = self._credential_name()
        REMOTE_HERMES.username = self._user.GetValue().strip()
        REMOTE_HERMES.key = self._key.GetValue().strip()
        return REMOTE_HERMES.save()
