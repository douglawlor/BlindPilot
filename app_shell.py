"""The app shell: names, shortcuts text, and platform helpers.

Constants every window shares, what Help says, and the small
platform helpers (desktop shortcut, link handover, raising the
running copy) that belong to the application rather than any one
window. Extracted from ``blindpilot_app.py``; blindpilot_app
re-exports the same names. Per issue #136, no real screen-reader
verification is represented as completed for this extraction.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import uuid
from pathlib import Path

import wx
from accessible_ai.storage.paths import bundle_dir as _mac_bundle_dir

import screen_speech
from screen_speech import announce
from agent_backends import BACKEND_LABELS, backend_label, blindpilot_data_dir
from app_config import (
    APPEARANCE_DARK,
    APPEARANCE_LIGHT,
    SETTINGS,
    _load_config,
    _save_config,
    _valid_appearance,
)
from new_session_dialog import _recent_folders
from session_panel import DEFAULT_PERMISSION_MODE, PROBE_TTL_SECONDS
from process_utils import no_window_kwargs


APP_NAME = "BlindPilot"
APP_VERSION = "0.55.12"


PAD = 8


PAD_DIALOG = 12


APP_MODE_AGENT = "agent"


APP_MODE_CHAT = "chat"


APP_MODE_LABELS = {APP_MODE_AGENT: "Agent", APP_MODE_CHAT: "Chat"}


CHAT_MODEL_ORDERS = (
    ("newest", "&Newest first", "Models discovered most recently appear first"),
    ("oldest", "&Oldest first", "Models discovered first appear first"),
    ("name_ascending", "Name, &A to Z", "Order models alphabetically"),
    ("name_descending", "Name, &Z to A", "Order models in reverse alphabetical order"),
)


ORIGINAL_APP_CREDIT = (
    "Based on the original Claude Code Reader application by doubletaponair.\n"
    "https://github.com/doubletaponair/claude-code-reader"
)


def about_description() -> str:
    """What Help, About BlindPilot says.

    The list of backends is read off the ones that exist rather than written
    out. Written out it was corrected by hand once and went stale twice over:
    Muse Code and Command Code were both shipped without it, in a sentence a
    screen reader reads aloud. ``BACKEND_LABELS`` is derived from ``BACKENDS``
    for the same reason.
    """
    labels = list(BACKEND_LABELS.values())
    listed = ", ".join(labels[:-1] + [f"and {labels[-1]}"]) if labels else ""
    return (
        f"An accessible desktop frontend for {listed}.\n\n"
        f"{ORIGINAL_APP_CREDIT}\n"
        "BlindPilot preserves and extends its accessibility-first work.\n\n"
        "Licensed under the MIT License. See LICENSE and CREDITS.md."
    )


MODEL_REFRESH_MINUTES_DEFAULT = PROBE_TTL_SECONDS // 60


def create_desktop_shortcut() -> str:
    """Put a BlindPilot shortcut on the desktop. Returns where it was written.

    An unpacked copy never went through an installer, so nothing has offered it
    a shortcut; this is how it gets one. On Windows it is a .lnk; on macOS it
    is a symlink to the application bundle, which Finder shows as an alias and
    doubles as a launcher. Raises OSError with a readable reason.
    """
    if platform.system() == "Darwin":
        bundle = _mac_bundle_dir()
        if bundle is None:
            raise OSError("A shortcut can only point at a packaged BlindPilot.")
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            raise OSError("The desktop folder could not be found.")
        link = desktop / APP_NAME
        try:
            if link.exists() or link.is_symlink():
                if link.is_dir() and not link.is_symlink():
                    raise OSError(f"{link} is a folder and was not replaced.")
                link.unlink()
            os.symlink(str(bundle), str(link))
        except OSError as exc:
            raise OSError(f"The desktop shortcut could not be created: {exc}") from exc
        return str(link)
    if platform.system() != "Windows":
        raise OSError("Desktop shortcuts are created on Windows and macOS only.")
    target = Path(sys.executable).resolve()
    if not getattr(sys, "frozen", False):
        raise OSError("A shortcut can only point at a packaged BlindPilot.")
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    if not desktop.is_dir():
        raise OSError("The desktop folder could not be found.")
    link = desktop / f"{APP_NAME}.lnk"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{str(link).replace(chr(39), chr(39) * 2)}'); "
        f"$s.TargetPath = '{str(target).replace(chr(39), chr(39) * 2)}'; "
        f"$s.WorkingDirectory = '{str(target.parent).replace(chr(39), chr(39) * 2)}'; "
        f"$s.Description = '{APP_NAME}'; $s.Save()"
    )
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        **no_window_kwargs(),
    )
    if result.returncode != 0 or not link.exists():
        raise OSError((result.stderr or "The shortcut could not be created.").strip())
    return str(link)


def _record_setup_complete(cfg: dict) -> bool:
    """Remember that the wizard was done, and say so if that failed.

    Without this the only symptom of an unwritable settings file is the whole
    wizard appearing again next launch - the CLI check, the sign-in, all of it -
    and nothing connects that to a file nobody can write.
    """
    if _save_config(cfg):
        return True
    announce(
        "Settings could not be saved. Setup will run again next launch. Check "
        "that the settings folder is writable and has space."
    )
    return False


def _appearance_for(value: str) -> wx.App.Appearance:
    """The wx.App.Appearance for a config value. Anything unknown is System."""
    return {
        APPEARANCE_LIGHT: wx.App.Appearance.Light,
        APPEARANCE_DARK: wx.App.Appearance.Dark,
    }.get(_valid_appearance(value), wx.App.Appearance.System)


def _valid_chat_model_order(value: object) -> str:
    """Return a supported model-catalog ordering, newest first by default."""
    orders = {order for order, _label, _help in CHAT_MODEL_ORDERS}
    return value if isinstance(value, str) and value in orders else "newest"


def _resource_dir() -> str:
    """Directory holding bundled resources (EarCons, etc.).

    PyInstaller unpacks data files to ``sys._MEIPASS`` at runtime; from source
    it's just the script's own directory.
    """
    base = getattr(sys, "_MEIPASS", None)
    return base if base else os.path.dirname(os.path.abspath(__file__))


def _app_icon_path() -> Path:
    """The window icon: packaging/BlindPilot.ico, from source or unpacked by PyInstaller."""
    return Path(_resource_dir()) / "packaging" / "BlindPilot.ico"


def _speech_route() -> str:
    """How announcements reach a screen reader here, for a bug report."""
    if platform.system() == "Darwin":
        return "VoiceOver announcements" if screen_speech._MAC_ANNOUNCE else "none (AppKit missing)"
    if platform.system() == "Linux":
        return "ATK announcements to Orca"
    if screen_speech._SPEAKER is not None:
        return "accessible_output2"
    return (
        "accessible_output2 (pending)" if not screen_speech._SPEAKER_INITIALIZED else "none found"
    )


def _tab_chord_notes() -> tuple[str, str]:
    """The menu accelerators for Next/Previous Session, per platform.

    Ctrl+Tab cannot work on macOS: wxWidgets maps Ctrl to Command there, and
    Cmd+Tab belongs to the system application switcher and never reaches the
    app. The chords are Cmd+Shift+]/[ there, and the menu, which is where
    anybody learns a chord, must say so. Written with Ctrl in the tabular
    form, which wxWidgets' Mac port shows and registers as Command.
    """
    if platform.system() == "Darwin":
        return "Ctrl+Shift+]", "Ctrl+Shift+["
    return "Ctrl+Tab", "Ctrl+Shift+Tab"


def _bring_to_front() -> None:
    """Force the window to the foreground on macOS.

    When launched from a plain `python` invocation (rather than a .app bundle),
    macOS may treat the process as a background accessory and never activate its
    window. Claiming the regular activation policy and activating brings it to
    the front. No-op when AppKit isn't available.
    """
    if not screen_speech._MAC_ANNOUNCE:
        return
    try:
        from AppKit import (  # type: ignore
            NSApplication,
            NSApplicationActivationPolicyRegular,
        )

        nsapp = NSApplication.sharedApplication()
        nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        nsapp.activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _links_dir() -> Path:
    return blindpilot_data_dir() / "links"


def hand_over_link(link: str) -> bool:
    """Leave a session link for the copy already running to open."""
    try:
        folder = _links_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{uuid.uuid4().hex}.link").write_text(link, encoding="utf-8")
    except OSError:
        return False
    return True


def take_handed_links() -> list[str]:
    """The links waiting in the links folder, oldest first; each is taken once."""
    try:
        files = sorted(_links_dir().glob("*.link"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return []
    links = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")[:2000]
            path.unlink()
        except OSError:
            continue
        links.append(text.strip())
    return links


def _raise_running_copy() -> bool:
    """Bring the BlindPilot that is already running to the front (Windows).

    Found by its window class and title, so a folder window that happens to be
    called BlindPilot is not the one raised. A copy just launched by the user
    holds the foreground right, which is what lets it hand the focus over.
    """
    if platform.system() != "Windows":
        return False
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    # The main window, or the setup wizard if the first copy is still in it.
    for window_class, title in (
        ("wxWindowNR", APP_NAME),
        ("wxWindow", APP_NAME),
        ("wxWindowNR", f"{APP_NAME} — Setup"),
        ("wxWindow", f"{APP_NAME} — Setup"),
    ):
        hwnd = user32.FindWindowW(window_class, title)
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    return False


KEYBOARD_SHORTCUTS = """Anywhere
Ctrl+L: focus the prompt.
F6, Shift+F6: move between the tab strip, the responses and the prompt, and read the status bar.
Ctrl+T: new session. Ctrl+W: close it. Ctrl+Q: quit.
Ctrl+Tab, Ctrl+Shift+Tab, Ctrl+Shift+], Ctrl+Shift+[: next and previous tab.
Ctrl+1 to Ctrl+9: go to that tab.
Ctrl+Shift+H: Recent Conversations. Enter opens one in a new tab; Ctrl+Enter reads one in a web view. F2 renames, Delete hides, Ctrl+G sets its groups.
Ctrl+G: Hermes conversations, when Hermes is the backend.
Ctrl+Shift+E: model and effort. Ctrl+Shift+M: cycle permission modes.
Ctrl+Shift+T: turn status. Ctrl+Shift+J: task list. Ctrl+Shift+D: changed files.
Ctrl+Shift+X: commands the agent ran. Ctrl+Shift+Y: decisions you made.
Alt+Enter: this session's properties. Ctrl+Shift+U: your Claude Code sessions on other computers.
Ctrl+Shift+S: find in all conversations; Enter on a match reopens it there.
Ctrl+Shift+O: send a response to another session, once or as an agent conversation.
Ctrl+Shift+I: what agents know: memories, tastes, instructions, skills and settings.
In Claude Code memories, Command Code tastes and Hermes memories, Alt+A adds one and Delete removes one.
Alt+E edits whatever is selected; in the text, Ctrl+S saves.
Ctrl+Shift+R: repeat the last announcement.
Ctrl+Shift+B: browse code blocks. Ctrl+Shift+L: browse links, in Agent or Chat mode.
Ctrl+Comma: Preferences. F1: this list.

Prompt
Enter: send. Shift+Enter: new line. Ctrl+Shift+Enter: save as a prompt, then send.
Ctrl+Shift+P: saved prompts. In its list, Enter inserts one and Delete removes it.
Ctrl+/: slash commands. Ctrl+Shift+A: attach files. Ctrl+V: paste a picture as an attachment.
Ctrl+Period: stop the running task.
Ctrl+Shift+K: compact the conversation. Ctrl+Shift+N: start a fresh one.
Ctrl+E: export the conversation.
Ctrl+Up or Alt+Up: into the newest response.

Responses
Up and Down: rows. Ctrl+Down: next response.
Enter: read the row in full. Shift+Enter: read the response as a formatted page.
Applications key: the row's actions.
C: copy the row. Shift+C: copy the whole response. Ctrl+Shift+C: copy its last code block.
Ctrl+F: search. F3, Shift+F3: next and previous match.
Ctrl+R: latest response, then back through the earlier ones.
Tab: back to the prompt.

Question and permission dialogs
Escape answers later or denies. Nothing is allowed by closing a dialog.
"""


def _destroy_other_top_level_windows(keep: wx.Window) -> None:
    """Destroy every top-level window but `keep`, on the way out.

    wx ends the event loop when the last top-level window is destroyed, so
    one left standing holds the whole process open: the window the person
    closed is gone, the process is still in the task list. Which window it
    is depends on the machine — a dialog parented to nothing, a window a
    platform component kept — so quitting does not go looking for a
    particular one. It takes them all.
    """
    try:
        windows = list(wx.GetTopLevelWindows())
    except Exception:
        # Teardown runs against whatever state the application is in; a wx
        # that cannot answer is not a reason to stay open.
        return
    for window in windows:
        if window is not keep:
            try:
                window.Destroy()
            except Exception:
                pass


def adopt_full_auto_default(config: dict) -> bool:
    """Move a config written before full-auto was the default onto it.

    Returns whether ``config`` was changed. Only a mode saved by an older
    BlindPilot is moved: once this has run, a mode chosen in the picker is
    the user's and is left exactly where they put it.
    """
    if config.get("permission_default") == DEFAULT_PERMISSION_MODE:
        return False
    config["permission_default"] = DEFAULT_PERMISSION_MODE
    config["permission_mode"] = DEFAULT_PERMISSION_MODE
    return True


_RECENT_FOLDERS_KEPT = 15


def _remember_folder(path: str) -> None:
    folders = [f for f in _recent_folders() if os.path.normcase(f) != os.path.normcase(path)]
    cfg = _load_config()
    cfg["recent_folders"] = [path, *folders][:_RECENT_FOLDERS_KEPT]
    _save_config(cfg)


def bug_report_facts(backend: str, app_mode: str) -> str:
    """What a bug report carries besides your words. Never a prompt, an
    answer, a folder or a file name: only versions and settings."""
    return "\n".join(
        [
            f"BlindPilot: {APP_VERSION}",
            f"System: {platform.platform()}",
            f"Python: {platform.python_version()}",
            f"wxPython: {wx.version()}",
            f"Mode: {app_mode}",
            # Chat mode talks to a provider API; no agent backend is in use.
            "Backend: none, Chat mode"
            if app_mode == APP_MODE_CHAT
            else f"Backend: {backend_label(backend)}",
            f"Narration: {SETTINGS.narration}",
            f"Live activity: {SETTINGS.live_rows}, spoken: {SETTINGS.speak_live}",
            f"Responses as text field: {SETTINGS.text_view}",
            f"Screen reader output: {_speech_route()}",
        ]
    )
