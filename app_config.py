"""BlindPilot's own configuration file, and the vocabulary stored in it.

This module owns where the settings live and how they are read and written,
and the names of the preferences that file holds. It does not own the
settings object itself, nor the dialogs that change it: those are callers,
and they stay where they can reach the window.

One rule governs the boundary. The functions here call each other, so a name
patched on this module is what reaches them; a name patched on the module
that *calls* into here is not. `_load_config` reads `_config_path` and
`_legacy_config_path` from this module's namespace, and `_save_config` reads
`_config_path` and `_config_dir` from it, so those four are patched here.
`_load_config` and `_save_config` themselves are called from all over the
application, and each caller reaches the name in its own namespace.

Nothing here imports wx. The settings file and the words in it do not need a
window, and a module that did would take one with it.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from pathlib import Path

from agent_backends import blindpilot_config_dir, migrate_macos_legacy_dirs
from hermes_backend import REMOTE_CREDENTIALS, remote_ws_url

# Before any setting is read: an install that predates the macOS conventions
# keeps its files in ~/.config and ~/.local/share, and they belong in
# ~/Library/Application Support. The move is on the import path because the
# remote settings below read config.json to build themselves.
migrate_macos_legacy_dirs()


def _config_dir() -> Path:
    return blindpilot_config_dir()


def _legacy_config_path() -> Path:
    """Original Claude Code Reader config, read-only for one-way migration."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "claude-reader" / "config.json"
    return Path.home() / ".config" / "claude-reader" / "config.json"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _load_config() -> dict:
    for path in (_config_path(), _legacy_config_path()):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def _save_config(cfg: dict) -> bool:
    """Write the settings. False if they did not get written.

    Callers announce what they saved, so they need to know. Written to one
    side and moved into place, so an interruption partway through cannot
    leave a half-written file: `_load_config` cannot parse one of those and
    falls back to the legacy claude-reader file, or to empty.
    """
    path = _config_path()
    temporary = path.with_name(path.name + ".new")
    try:
        _config_dir().mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError):
        logging.getLogger("blindpilot").warning("could not write the settings to %s", path)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


# How much of a run is read out.
#
# "Everything" is what BlindPilot has always done and stays the default: every
# tool call, result and subagent line spoken in order. On a short turn that is
# right. On a fan-out it is minutes of backlog, and the backlog is not ours -
# it sits in the screen reader's own queue, which cannot be measured, shortened
# or popped from, only purged wholesale, which would silence other applications
# too. So this is a choice offered rather than a cleverness applied.
#
# "Keep up" speaks what the turn is saying - the message, the answer, notices
# and errors - and leaves the step-by-step in the list to be read.
NARRATION_EVERYTHING = "everything"
NARRATION_KEEP_UP = "keep_up"
NARRATION_MODES = (
    (NARRATION_EVERYTHING, "Follow &everything", "Speak every step of a run as it happens"),
    (
        NARRATION_KEEP_UP,
        "&Keep up",
        "Speak the message, the answer and anything important, and leave the steps in the list",
    ),
)

# Kinds of activity that are spoken whatever the mode. "notice" is BlindPilot
# speaking for itself - waiting for background agents, how a run ended - which
# is not tool narration and must not be muted along with it.
_ALWAYS_SPOKEN = ("assistant", "notice")

# The four cues, in the order the menu offers them. The key is what the
# configuration stores, so it must not change; the rest is only wording.
SOUND_CUES: tuple[tuple[str, str, str], ...] = (
    ("send", "Message &sent", "Play a sound when a message is sent"),
    ("working", "&Working", "Play a sound for as long as a turn is running"),
    ("received", "&Answer received", "Play a sound when the answer arrives"),
    ("error", "So&mething went wrong", "Play a sound when a turn fails"),
)


CUE_LOOP = "loop"
CUE_PERIODIC = "periodic"
CUE_OFF = "off"
PROGRESS_CUES = (CUE_LOOP, CUE_PERIODIC, CUE_OFF)

# How often the periodic working cue plays, and the range the setting allows. A
# cue every couple of seconds is barely different from the loop; one every few
# minutes stops answering "is it still working".
CUE_SECONDS_DEFAULT = 10
CUE_SECONDS_MIN = 2
CUE_SECONDS_MAX = 120


def _valid_progress_cue(value: object) -> str:
    """One of the three cue behaviours, whatever the config file holds.

    A config written by a newer version, or edited by hand, must not stop the
    app from starting, so anything unrecognised falls back to the default.
    """
    text = str(value or "").strip().lower()
    return text if text in PROGRESS_CUES else CUE_PERIODIC


# How the windows are drawn. "system" follows the operating system's light or
# dark setting; the other two force one. wxWidgets applies this once, before
# the first window exists, so a change waits for the next start.
APPEARANCE_SYSTEM = "system"
APPEARANCE_LIGHT = "light"
APPEARANCE_DARK = "dark"
APPEARANCES = (
    (APPEARANCE_SYSTEM, "Follow system"),
    (APPEARANCE_LIGHT, "Light"),
    (APPEARANCE_DARK, "Dark"),
)
APPEARANCE_RESTART_NOTE = "Appearance will change the next time BlindPilot starts."


def _valid_appearance(value: object) -> str:
    """One of the three appearances, whatever the config file holds.

    Missing or unrecognised means follow the system, so a config written by a
    newer version cannot leave the app forced light or dark.
    """
    text = str(value or "").strip().lower()
    return text if text in {key for key, _label in APPEARANCES} else APPEARANCE_SYSTEM


def _valid_cue_seconds(value: object) -> int:
    try:
        seconds = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return CUE_SECONDS_DEFAULT
    return max(CUE_SECONDS_MIN, min(CUE_SECONDS_MAX, seconds))


class _RemoteHermes:
    """Where to find a Hermes that is not on this computer.

    Off by default: with nothing configured, the Hermes backend launches the
    local copy and there is nothing to set up. Filling this in points it at a
    Hermes running elsewhere -- a home server, or the same machine reached over
    a private network -- so the desktop can work with it without a terminal.

    The key is stored separately from the display preferences, in a file of its
    own with owner-only permissions where the platform supports them. It is a
    credential: it belongs no more in a settings file full of checkboxes than a
    password does.
    """

    def __init__(self) -> None:
        cfg = _load_config()
        remote = cfg.get("remote_hermes")
        remote = remote if isinstance(remote, dict) else {}
        self.enabled = bool(remote.get("enabled", False))
        self.host = str(remote.get("host", "") or "")
        try:
            self.port = int(remote.get("port", 9119))
        except (TypeError, ValueError):
            self.port = 9119
        self.secure = bool(remote.get("secure", False))
        # "token" for a server on this machine; "password" for one reachable
        # from elsewhere, where Hermes requires a login of its own and its
        # WebSocket upgrade then wants a short-lived ticket.
        credential = str(remote.get("credential", "token") or "token")
        self.credential = credential if credential in REMOTE_CREDENTIALS else "token"
        self.username = str(remote.get("username", "") or "")
        self.key = self._read_key()

    @staticmethod
    def _key_path() -> Path:
        return _config_dir() / "remote-hermes-key"

    def _read_key(self) -> str:
        try:
            return self._key_path().read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return ""

    def _write_key(self) -> bool:
        """Write the credential file. False when it refused the write.

        The failure is returned rather than raised or logged alone, because
        `save` has to carry it to the caller: a key that never reached disk
        leaves the next launch connecting without it, in silence.
        """
        path = self._key_path()
        try:
            _config_dir().mkdir(parents=True, exist_ok=True)
            if not self.key:
                path.unlink(missing_ok=True)
                return True
            # Created owner-only rather than chmod'ed afterwards, so it is
            # never readable by others even for an instant. On Windows the
            # per-user AppData directory is already the boundary.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.key)
            if platform.system() != "Windows":
                os.chmod(path, 0o600)
            return True
        except OSError:
            return False

    def url(self) -> str:
        """The gateway address, or "" when the remote mode is not in use."""
        if not self.enabled or not self.host.strip():
            return ""
        return remote_ws_url(self.host, self.port, self.secure)

    def describe(self) -> str:
        """One line saying where this will connect, for the settings dialog."""
        if not self.enabled:
            return "Off. Hermes runs the copy installed here."
        url = self.url()
        if not url:
            return "On, but no address is set yet."
        how = "username and password" if self.credential == "password" else "session token"
        return f"On. {url}, signing in with a {how}"

    def save(self) -> bool:
        """Write the address, who it is, and the key. False when either file refused.

        One answer for both writes: the caller speaks the failure once, so a
        settings file and a key file that both refused cost one sentence
        between them, not two.
        """
        cfg = _load_config()
        cfg["remote_hermes"] = {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "secure": self.secure,
            "credential": self.credential,
            "username": self.username,
        }
        saved = _save_config(cfg)
        key_saved = self._write_key()
        return saved and key_saved


REMOTE_HERMES = _RemoteHermes()
