"""BlindPilot's own settings file, and the preferences it holds.

This module owns where the settings live, the names of the preferences
that file holds, the settings object built from them, and the address of
a Hermes running on another computer.

The file itself is read and written by
`accessible_ai.storage.preferences`, which the Codex backend's Account
choice shares: one file, two writers, one lock.

One rule governs the boundary. The functions here call each other, so a
name patched on this module is what reaches them; a name patched on the
module that *calls* into here is not. `_load_config` reads `_config_path`
and `_legacy_config_path` from this module's namespace, and `_save_config`
reads `_config_path` and `_config_dir` from it, so those four are
patched here. `_load_config` and `_save_config` themselves are called
from all over the application, and each caller reaches the name in its
own namespace.

Nothing here imports wx. The settings file and the words in it do not
need a window, and a module that did would take one with it.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from accessible_ai.storage import preferences
from agent_backends import blindpilot_config_dir, migrate_macos_legacy_dirs
from hermes_backend import REMOTE_CREDENTIALS, remote_ws_url

MODEL_REFRESH_MINUTES_DEFAULT = 15
MODEL_REFRESH_MINUTES_MIN = 0
MODEL_REFRESH_MINUTES_MAX = 240


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
    return preferences.load_config((_config_path(), _legacy_config_path()))


def _save_config(cfg: dict) -> bool:
    """Write the settings. False if they did not get written.

    Callers announce what they saved, so they need to know. The write itself is
    `accessible_ai.storage.preferences`, shared with the Codex backend's Account
    choice, which lives in this same file: only the keys handed in are changed,
    so a save built from the settings as they were read cannot put back an
    Account choice -- or anything else -- that was written in between.
    """
    return preferences.update_config(_config_path(), cfg)


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


def _valid_model_refresh_minutes(value: object) -> int:
    """The background model-refresh interval in minutes, whatever config holds.

    A hand-edited or newer-version config must not stop the app from starting,
    so anything unreadable falls back to the default, and anything outside the
    offered range is clamped into it -- a negative reading as "off" rather than
    as a refresh that never runs.
    """
    try:
        minutes = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MODEL_REFRESH_MINUTES_DEFAULT
    return max(MODEL_REFRESH_MINUTES_MIN, min(MODEL_REFRESH_MINUTES_MAX, minutes))


class _Settings:
    """User preferences that change how a run is presented, saved to config.

    ``live_rows`` and ``speak_live`` default to on, so activity appears in the
    list and is spoken automatically through NVDA, JAWS, or VoiceOver. Turning both off restores the
    pre-live-narration behaviour: nothing appears until the turn ends, and
    nothing is spoken. ``text_view`` swaps the responses list for a read-only
    edit field and defaults to off.

    ``progress_cue`` decides how the working cue behaves while a turn runs:

    ``loop``
        the original behaviour, the cue repeating end to end for the whole turn
    ``periodic``
        the cue once every ``progress_cue_seconds``, so a long run still says it
        is alive without repeating over the answer being spoken
    ``off``
        no working cue at all; send and received still play

    ``periodic`` is the default. The cue file is under a second long, so looping
    it means dozens of repeats per turn on top of the speech, and until now
    there was no way to turn it down.
    """

    def __init__(self) -> None:
        cfg = _load_config()
        self.live_rows = bool(cfg.get("live_rows", True))
        self.speak_live = bool(cfg.get("speak_live", True))
        self.sounds_enabled = bool(cfg.get("sounds_enabled", True))
        # A mode this version does not know is somebody else's config, not an
        # instruction to go quiet.
        narration = cfg.get("narration")
        self.narration = (
            narration
            if isinstance(narration, str)
            and narration in {mode for mode, _label, _help in NARRATION_MODES}
            else NARRATION_EVERYTHING
        )
        # `sounds_enabled` above is the master switch and keeps its meaning.
        # These say which cues it turns on, because the three are not
        # interchangeable: "working" is a loop that runs for the whole turn,
        # so wanting it gone is not the same wish as wanting silence.
        #
        # Missing keys default to on, so a configuration written before this
        # existed reads as it always did; unknown ones are dropped, so one
        # written by a newer version cannot mute or break this one.
        stored = cfg.get("sound_cues")
        stored = stored if isinstance(stored, dict) else {}
        self.sound_cues = {cue: bool(stored.get(cue, True)) for cue, _label, _help in SOUND_CUES}
        self.text_view = bool(cfg.get("text_view", False))
        self.show_thinking = bool(cfg.get("show_thinking", False))
        # Whether a question a turn only wrote into its answer opens the same
        # dialog a question asked through the backend's own tool opens. On by
        # default: a turn that ends waiting on an answer nobody was told about
        # is the thing this exists to stop. It can be turned off because the
        # judgement is a reading of prose and can be wrong, and a dialog that
        # opens when nothing was asked interrupts.
        self.ask_written_questions = bool(cfg.get("ask_written_questions", True))
        # A system notification when a turn ends, fails or asks something while
        # another application has focus. Nothing is shown while BlindPilot is
        # in front: the announcement already said it.
        self.notify_in_background = bool(cfg.get("notify_in_background", True))
        # Read your message back as it goes, so you hear what was actually
        # sent: dictation and paste put text in the prompt nobody heard.
        self.read_back_sent = bool(cfg.get("read_back_sent", True))
        self.progress_cue = _valid_progress_cue(cfg.get("progress_cue"))
        self.progress_cue_seconds = _valid_cue_seconds(cfg.get("progress_cue_seconds"))
        # Minutes between background re-reads of a tab's model catalog; 0 turns
        # it off. Off is a real choice: each read is a provider CLI start.
        self.model_refresh_minutes = _valid_model_refresh_minutes(
            cfg.get("model_refresh_minutes", MODEL_REFRESH_MINUTES_DEFAULT)
        )
        # Read again in main() before the first window, which is the only
        # point wxWidgets lets it be set. Kept here so Preferences can show
        # and save it like the rest.
        self.appearance = _valid_appearance(cfg.get("appearance"))

    def save(self) -> bool:
        """Write the settings out. False means the file did not take them."""
        cfg = _load_config()
        cfg["live_rows"] = self.live_rows
        cfg["speak_live"] = self.speak_live
        cfg["sounds_enabled"] = self.sounds_enabled
        cfg["narration"] = self.narration
        cfg["sound_cues"] = self.sound_cues
        cfg["text_view"] = self.text_view
        cfg["show_thinking"] = self.show_thinking
        cfg["ask_written_questions"] = self.ask_written_questions
        cfg["notify_in_background"] = self.notify_in_background
        cfg["read_back_sent"] = self.read_back_sent
        cfg["progress_cue"] = self.progress_cue
        cfg["progress_cue_seconds"] = self.progress_cue_seconds
        cfg["model_refresh_minutes"] = self.model_refresh_minutes
        cfg["appearance"] = self.appearance
        return _save_config(cfg)


migrate_macos_legacy_dirs()
SETTINGS = _Settings()


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
