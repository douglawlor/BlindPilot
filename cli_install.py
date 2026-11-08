"""Finding and installing the command-line tools BlindPilot drives.

Everything here is about the tools on the other side of the window: where
Claude Code is, how to install it, how to install Node.js and npm for the
backends that need them, and how to put the folders they land in on the
PATH so they work in a terminal as well as in here.

Nothing here touches the screen reader or a window, so it can be tested on
its own.

The names the installer calls among its own are patched on this module,
because that is where the calls are made from. A patch aimed at
blindpilot_app rebinds the name there and the installer carries on with
the real one -- which, for install_backend, is a real npm install.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from collections.abc import Callable, Sequence

import backend_pool
from agent_backends import (
    BACKENDS,
    BACKEND_ANTIGRAVITY,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_COMMANDCODE,
    BACKEND_COPILOT,
    BACKEND_FREEBUFF,
    BACKEND_GEMINI,
    BACKEND_HERMES,
    BACKEND_MUSE,
    BACKEND_MUSEAI,
    BACKEND_OPENCODE,
    FREEBUFF_PREFERRED_MODEL,
    backend_label,
    blindpilot_data_dir,
    find_backend_cli,
    freebuff_model_options,
    invalidate_backend_cache,
    normalize_backend,
    set_freebuff_model,
    stop_opencode_server,
    subprocess_env,
)
from app_updater import version_tuple
from certificates import open_url
from process_utils import no_window_kwargs
from hermes_backend import wsl_exe as muse_wsl_exe
from muse_backend import (
    muse_cli_path,
    muse_installed,
    reset_discovery as reset_muse_discovery,
)


def _user_agent() -> str:
    # The packaging spec reads the version directly from blindpilot_app.py.
    from blindpilot_app import APP_VERSION

    return f"BlindPilot/{APP_VERSION}"


CLAUDE_BIN = "claude"


# Common install locations to check when `claude` isn't on PATH.
# macOS GUI apps launched from Finder/Dock inherit a minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) and miss Homebrew, nvm, the official
# ~/.claude/local installer, etc. Windows GUI apps usually inherit the
# user PATH, but installs to non-default npm prefixes can still miss it.
def _native_bin_dir() -> Path:
    """Where the official native installer puts the launcher, every platform."""
    return Path.home() / ".local" / "bin"


def _fallback_claude_paths() -> tuple[Path, ...]:
    home = Path.home()
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
        local_appdata = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        candidates: list[Path] = []
        for name in ("claude.exe", "claude.cmd", "claude.ps1"):
            candidates.extend(
                [
                    # Native installer (install.ps1 / install.cmd) — the default.
                    _native_bin_dir() / name,
                    # WinGet's shim directory.
                    Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / name,
                    Path(appdata) / "npm" / name,
                    home / ".claude" / "local" / name,
                    home / ".volta" / "bin" / name,
                    Path(local_appdata) / "Programs" / "claude" / name,
                ]
            )
        return tuple(candidates)
    return (
        _native_bin_dir() / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".npm-global" / "bin" / "claude",
        home / ".volta" / "bin" / "claude",
    )


def _login_shell() -> str | None:
    """The user's real login shell, if there is one (POSIX only)."""
    if platform.system() == "Windows":
        return None
    shell = os.environ.get("SHELL")
    return shell if shell and os.path.isfile(shell) else None


def _login_shell_which(name: str) -> str | None:
    """Resolve *name* the way a login shell would.

    A GUI app launched from Finder or the Dock inherits a minimal PATH, so this
    is how a CLI the user can run is found. It runs `$SHELL -l -c`, a login
    shell that is not interactive, so for zsh it reads .zprofile and not
    .zshrc. `command -v` is POSIX and also works in fish.
    """
    shell = _login_shell()
    if shell is None:
        return None
    try:
        result = subprocess.run(
            [shell, "-l", "-c", f"command -v {name}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = result.stdout.strip().splitlines()[-1].strip()
    if path and os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def _find_claude() -> str | None:
    """Locate the `claude` binary even when launched from a GUI app.

    Order: PATH, well-known install locations, then (POSIX only) the user's
    login shell so any custom PATH from .zprofile / .bash_profile is honored.
    """
    binary = shutil.which(CLAUDE_BIN)
    if binary:
        return binary

    for candidate in _fallback_claude_paths():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return _login_shell_which(CLAUDE_BIN)


WINDOWS_INSTALL_PS1_URL = "https://claude.ai/install.ps1"
POSIX_INSTALL_SH_URL = "https://claude.ai/install.sh"

# Hermes ships per-platform script installers of the same shape — a PowerShell
# one-liner on native Windows, curl through bash on macOS, Linux and WSL2 —
# and is not on npm. It drops a launcher under the user's home directory and
# self-updates from then on; no administrator rights, no Node.js.
HERMES_INSTALL_PS1_URL = "https://hermes-agent.nousresearch.com/install.ps1"
HERMES_INSTALL_SH_URL = "https://hermes-agent.nousresearch.com/install.sh"


def _same_dir(a: str, b: str) -> bool:
    """Compare two directory strings the way the platform resolves them.

    ``normcase`` folds case and slashes on Windows and is a no-op on POSIX,
    which is what we want: PATH is case-insensitive on one and not the other.
    ``$HOME`` / ``%USERPROFILE%`` style references are expanded, since PATH
    entries are routinely written that way and comparing them literally would
    append a duplicate entry on every launch.
    """

    def norm(p: str) -> str:
        p = os.path.expandvars(os.path.expanduser(p.strip().strip('"')))
        return os.path.normcase(os.path.normpath(p))

    return bool(a.strip()) and norm(a) == norm(b)


def _bundle_dir() -> str | None:
    """The folder a packaged build keeps its own libraries in, if this is one."""
    if not getattr(sys, "frozen", False):
        return None
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))


def path_without_bundle_entries(current: str, bundle: str) -> str:
    """*current* PATH with every entry inside the packaged folder removed."""

    def inside(entry: str) -> bool:
        try:
            candidate = os.path.normcase(
                os.path.normpath(os.path.abspath(entry.strip().strip('"')))
            )
        except (OSError, ValueError):
            return False
        root = os.path.normcase(os.path.normpath(os.path.abspath(bundle)))
        return candidate == root or candidate.startswith(root + os.sep)

    kept = [entry for entry in current.split(os.pathsep) if entry.strip() and not inside(entry)]
    return os.pathsep.join(kept)


def keep_bundle_off_child_path() -> None:
    """Stop BlindPilot's private DLL folder from reaching child processes.

    PyInstaller's pywin32 hook puts ``_internal\\pywin32_system32`` on this
    process's PATH. It registers the same folder with ``os.add_dll_directory``,
    which is what actually makes pywin32 load; the PATH entry is only a
    fallback for Anaconda builds where that call does nothing. Unlike the DLL
    directory, PATH is inherited — by the agent CLI, by the terminal, and by
    everything those start in turn, for as long as any of them live.

    Those processes then resolve ordinary libraries (the Visual C++ runtime,
    pythoncom) out of BlindPilot's install folder and hold them open. The next
    update finds its own files in use by programs it has no business closing,
    and the installer gives up rather than replace them — the silent "installer
    exited with code 5" this used to end in.
    """
    bundle = _bundle_dir()
    if not bundle:
        return
    current = os.environ.get("PATH", "")
    cleaned = path_without_bundle_entries(current, bundle)
    if cleaned != current:
        os.environ["PATH"] = cleaned


def _windows_persistent_path_dirs() -> list[str]:
    """Every directory on the *persistent* PATH — user PATH plus system PATH.

    This is what a freshly opened cmd, PowerShell 5, pwsh or Windows Terminal
    tab composes its PATH from, which is not necessarily what this process
    inherited. Checking the registry rather than ``os.environ`` is the only way
    to know whether `claude` will actually resolve in a new terminal.
    """
    import winreg

    dirs: list[str] = []
    for root, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _type = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if isinstance(value, str):
            dirs.extend(p for p in value.split(os.pathsep) if p.strip())
    return dirs


def _posix_persistent_path_dirs() -> list[str]:
    """The PATH a fresh Terminal window would have.

    Asks the user's own login shell, so whatever their .zprofile / .zshrc /
    .bash_profile / fish config builds up is what we see — the same PATH they
    would get by opening Terminal or iTerm and typing `claude`. Printed one per
    line because in fish ``$PATH`` is a list, not a colon-joined string.
    """
    shell = _login_shell()
    if shell is None:
        return []
    if os.path.basename(shell) == "fish":
        # fish's $PATH is a real list, so this is already space-safe.
        script = "for p in $PATH; echo $p; end"
    else:
        # Split on the colon with tr rather than by word-splitting: PATH
        # entries containing spaces are normal on macOS (/Applications/...)
        # and word-splitting would shred them into fragments.
        script = 'printf \'%s\\n\' "$PATH" | tr ":" "\\n"'
    try:
        result = subprocess.run(
            [shell, "-l", "-c", script],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [p for p in os.environ.get("PATH", "").split(":") if p.strip()]
    if result.returncode != 0:
        # A configured shell can be temporarily unusable (for example a stale
        # WSL launcher on Windows while exercising the macOS code path).  The
        # inherited POSIX PATH is still better evidence than an empty list.
        return [p for p in os.environ.get("PATH", "").split(":") if p.strip()]
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_on_persistent_path(directory: Path) -> bool:
    """Would a newly opened terminal find things in *directory*?

    Deliberately not a check of ``os.environ``: this process may have inherited
    a PATH that a fresh terminal will not have, or vice versa.
    """
    try:
        if platform.system() == "Windows":
            dirs = _windows_persistent_path_dirs()
        else:
            dirs = _posix_persistent_path_dirs()
            if not dirs:
                return True  # No usable login shell to ask — don't cry wolf.
        return any(_same_dir(p, str(directory)) for p in dirs)
    except Exception:
        return True  # Never block the user on a check we couldn't run.


def _broadcast_environment_change() -> None:
    """Tell Explorer (and everything else) that the environment changed.

    Without this broadcast a newly opened terminal still inherits Explorer's
    stale copy of the environment, so a PATH edit appears to do nothing until
    the user signs out and back in.
    """
    try:
        import ctypes
        from ctypes import wintypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002

        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            ctypes.c_wchar_p,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(wintypes.DWORD),
        ]
        send.restype = wintypes.LPARAM
        result = wintypes.DWORD()
        send(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def _add_to_process_path(directory: Path) -> None:
    """Make the directory usable in *this* process without a restart."""
    entry = str(directory)
    current = os.environ.get("PATH", "")
    if not any(_same_dir(p, entry) for p in current.split(os.pathsep) if p.strip()):
        os.environ["PATH"] = entry + os.pathsep + current


def _path_with_entry(current: str, directory: str) -> str | None:
    """The PATH string *current* with *directory* appended, or None if present.

    Kept separate from the registry write so the string surgery — the part that
    can wreck someone's PATH — is testable on its own.
    """
    entries = [p for p in current.split(os.pathsep) if p.strip()]
    if any(_same_dir(p, directory) for p in entries):
        return None
    entries.append(directory)
    return os.pathsep.join(entries)


def _shell_profile_file() -> Path:
    """The startup file to extend for the user's login shell.

    zsh is the default on macOS since Catalina; ``.zshrc`` is read by both
    interactive login and non-login shells, so it is the one place that covers
    Terminal, iTerm and a shell opened inside an editor. Bash on macOS reads
    ``.bash_profile`` for login shells (which is what Terminal opens) while
    Linux desktops open non-login shells that read ``.bashrc``.
    """
    home = Path.home()
    shell = os.path.basename(_login_shell() or "")
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    if shell == "bash":
        if platform.system() == "Darwin":
            return home / ".bash_profile"
        return home / ".bashrc"
    return home / ".profile"


PATH_STANZA_MARKER = "# Added by BlindPilot"


def _path_export_line(directory: Path, shell: str) -> str:
    """The one line that puts *directory* on PATH for the given shell.

    Separators are forced to POSIX form — the line we are composing is shell
    script, not a host path, so it must read the same whatever built it.
    """
    # Written against $HOME rather than the expanded path, so the profile stays
    # portable and reads the way a person would have written it by hand.
    home = Path.home().as_posix()
    text = directory.as_posix()
    if text == home or text.startswith(home + "/"):
        text = "$HOME" + text[len(home) :]
    if shell == "fish":
        return f'fish_add_path "{text}"'
    return f'export PATH="{text}:$PATH"'


def ensure_on_posix_path(directory: Path) -> str | None:
    """Add *directory* to PATH for future terminal sessions on macOS / Linux.

    Appends an export line to the login shell's startup file — the equivalent
    of the registry write on Windows, and the only way to affect terminals the
    user opens later. Returns the file that was changed, or None if nothing
    needed changing. Raises OSError if the write fails.
    """
    if _is_on_persistent_path(directory):
        return None

    profile = _shell_profile_file()
    line = _path_export_line(directory, os.path.basename(_login_shell() or ""))
    try:
        existing = profile.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    # Appending twice would leave a duplicate stanza in a file the user owns.
    if line in existing:
        return None

    profile.parent.mkdir(parents=True, exist_ok=True)
    with open(profile, "a", encoding="utf-8") as fh:
        # Lead with a newline: the file may not end with one, and appending to
        # a half-finished last line would corrupt it.
        fh.write(f"\n{PATH_STANZA_MARKER}\n{line}\n")
    return str(profile)


def ensure_on_path(directory: Path) -> str | None:
    """Make *directory* reachable from a terminal, persistently.

    Returns a description of what was changed, or None if nothing needed
    changing. Raises OSError if the change could not be written.
    """
    _add_to_process_path(directory)
    if platform.system() == "Windows":
        return "your user PATH" if ensure_on_windows_path(directory) else None
    return ensure_on_posix_path(directory)


def ensure_on_windows_path(directory: Path) -> bool:
    """Append *directory* to the user's persistent PATH if it isn't there.

    Writes ``HKCU\\Environment``, which cmd, PowerShell 5.1, pwsh 7 and Windows
    Terminal all read when they start, so one entry covers every shell. `setx`
    is deliberately not used — it silently truncates PATH at 1024 characters.

    Returns True when an entry was added, False when it was already present.
    Raises OSError if the registry write fails.
    """
    if platform.system() != "Windows":
        return False

    import winreg  # after the platform check — the module is Windows-only

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        try:
            current, regtype = winreg.QueryValueEx(key, "Path")
        except OSError:
            current, regtype = "", winreg.REG_EXPAND_SZ
        if not isinstance(current, str):
            current, regtype = "", winreg.REG_EXPAND_SZ
        updated = _path_with_entry(current, str(directory))
        if updated is None:
            return False
        # Preserve REG_EXPAND_SZ when that's what's there: existing entries may
        # contain %USERPROFILE% and rewriting the value as REG_SZ would leave
        # those literal, breaking the rest of the user's PATH.
        if regtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            regtype = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, "Path", 0, regtype, updated)

    _broadcast_environment_change()
    return True


def _powershell_exe() -> str | None:
    """Windows PowerShell first — it ships with Windows 11, pwsh may not."""
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _script_installer_argv(powershell_command: str, posix_url: str) -> list[str] | None:
    """The command that runs an official script installer on this platform.

    None when the prerequisites are missing: no PowerShell on Windows, no curl
    or shell on macOS and Linux. macOS ships both curl and bash; a Linux box
    without curl is possible.
    """
    if platform.system() == "Windows":
        shell = _powershell_exe()
        if shell is None:
            return None
        return [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            powershell_command,
        ]
    if shutil.which("curl") is None:
        return None
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:
        return None
    return [shell, "-c", f"curl -fsSL {posix_url} | bash"]


def _install_argv() -> list[str] | None:
    """Claude Code's official native installer for this platform."""
    return _script_installer_argv(f"irm {WINDOWS_INSTALL_PS1_URL} | iex", POSIX_INSTALL_SH_URL)


def _hermes_install_argv() -> list[str] | None:
    """Hermes' official installer for this platform."""
    return _script_installer_argv(f"iex (irm {HERMES_INSTALL_PS1_URL})", HERMES_INSTALL_SH_URL)


def _missing_prereq_message(installer: str = "the installer") -> str:
    missing = "PowerShell" if platform.system() == "Windows" else "curl and bash"
    return f"Could not find {missing} on this computer, so {installer} cannot be run automatically."


def _hermes_missing_prereq_message() -> str:
    return _missing_prereq_message("Hermes' installer")


# Where uv -- the tool Hermes' installer bootstraps a Python with -- keeps the
# interpreters it downloads. Measured with ``uv python dir`` on a healthy
# machine: %APPDATA%\\uv\\python, the ROAMING folder. Roaming is also the one
# folder a roaming profile or a cloud-sync client takes over, and Windows
# refuses to walk through a folder it does not trust when an executable is
# about to live under it. uv then dies at "Downloading Python" with
# "Failed to create Python minor version link directory ... (os error 448)" --
# ERROR_TRUSTING_PATH -- while the installer around it still exits 0.
UV_PYTHON_DIR_ENV = "UV_PYTHON_INSTALL_DIR"


def _uv_roaming_python_dir() -> Path:
    """Where uv would put its managed Pythons if nothing were configured."""
    roaming = os.environ.get("APPDATA", "").strip()
    base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
    return base / "uv" / "python"


def _uv_local_python_dir() -> Path:
    """The same folder under local app data, where uv keeps its cache."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "uv" / "python"


def _windows_file_attributes(directory: Path) -> int | None:
    """The Windows ``st_file_attributes`` of a folder, or None when it cannot be asked."""
    try:
        return getattr(os.stat(directory), "st_file_attributes", 0)
    except OSError:
        return None


def _folder_is_behind_reparse_point(directory: Path) -> bool:
    """Whether a step on the way to *directory* is a reparse point.

    That is what a cloud-sync placeholder or a roaming redirect looks like to
    Windows, and it is what makes it refuse to place a program underneath.
    Only folders that already exist can be asked, so the walk stops at the
    first one that does not -- a path uv has not created yet is judged by the
    deepest part of it that exists.
    """
    current = directory
    while True:
        attributes = _windows_file_attributes(current)
        if attributes is None:
            return False
        if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _folder_is_cloud_synced(directory: Path) -> bool:
    """Whether *directory* sits inside a folder a sync client owns.

    Two signals, because each alone misses a machine: the reparse attribute
    Windows puts on the synced folder, and OneDrive's own recorded roots, which
    name the tree even where nothing has been turned into a placeholder yet.
    """
    if _folder_is_behind_reparse_point(directory):
        return True
    try:
        candidate = os.path.normcase(os.path.abspath(str(directory)))
    except OSError:
        return False
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(name, "").strip()
        if not root:
            continue
        try:
            prefix = os.path.normcase(os.path.abspath(root)) + os.sep
        except OSError:
            continue
        if candidate.startswith(prefix):
            return True
    return False


def _remember_windows_env_var(name: str, value: str) -> None:
    """Set one user environment variable for every program started later.

    ``HKCU\\Environment`` is the registry twin of ``setx``, read by cmd,
    PowerShell and Windows Terminal when they start. Raises OSError when the
    write fails.
    """
    import winreg  # after the platform check -- the module is Windows-only

    os.environ[name] = value
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        try:
            current, regtype = winreg.QueryValueEx(key, name)
        except OSError:
            current, regtype = None, winreg.REG_EXPAND_SZ
        if current == value:
            return
        if regtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            regtype = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, name, 0, regtype, value)
    _broadcast_environment_change()


def _hermes_installer_env(log: Callable[[str], None]) -> dict[str, str] | None:
    """The environment Hermes' installer runs in, or None to change nothing.

    Its Python bootstrap is uv's, and uv's default home for interpreters is the
    roaming folder (see ``UV_PYTHON_DIR_ENV``). On the machines that report
    ``os error 448`` that folder sits behind a cloud-sync placeholder, so the
    interpreters are sent to local app data instead -- where uv already keeps
    its cache, and which no sync client owns. Asked for on every Windows
    install rather than only when the check below agrees, because a synced
    folder is not always recognisable from here and a wrong guess costs a
    folder, while a missed one costs the install.

    A location the user configured themselves is left exactly as it is.
    """
    if platform.system() != "Windows":
        return None
    if os.environ.get(UV_PYTHON_DIR_ENV, "").strip():
        return None
    local = _uv_local_python_dir()
    if _folder_is_cloud_synced(local):
        log(
            "Both of the usual folders for Hermes' Python sit inside a folder that a "
            "cloud-sync program owns, and Windows will not put a program there. Set "
            "UV_PYTHON_INSTALL_DIR to a folder outside every synced folder, then try again."
        )
        return None
    if _folder_is_cloud_synced(_uv_roaming_python_dir()):
        # Remembered, not just handed to this one installer run: the same
        # bootstrap runs again whenever Hermes is updated from a terminal, and
        # an environment variable BlindPilot set for one child process does not
        # reach that one.
        try:
            _remember_windows_env_var(UV_PYTHON_DIR_ENV, str(local))
            log(
                "Your roaming AppData is inside a synced folder, which Windows will not "
                f"put Hermes' Python in. It will go in {local} instead, and Windows has "
                "been told so that Hermes' own updater finds it again."
            )
        except OSError as exc:
            log(
                "Hermes' Python will go in the local app-data folder for this install, "
                f"but Windows could not be told to keep doing so: {exc}"
            )
    return {UV_PYTHON_DIR_ENV: str(local)}


def _hermes_bootstrap_failure(lines: Sequence[str]) -> str:
    """What to hear when Hermes' installer left no Python and no clear reason.

    uv's own sentence for a folder it is not allowed through is localised -- on
    a Polish machine it begins "Nie można przejść do tej ścieżki" -- and the
    installer exits 0 regardless, so the only stable text in the whole log is
    the error number and the name of the step that failed. Both are looked for
    here, because "exit code 0 but hermes was not found" describes nothing a
    user can act on.
    """
    text = "\n".join(lines).lower()
    if "os error 448" not in text and "minor version link" not in text:
        return ""
    return (
        "Windows refused to put the Python Hermes needs inside a folder owned by a "
        "cloud-sync program. BlindPilot uses the local app-data folder for it instead; if "
        "this still fails, set UV_PYTHON_INSTALL_DIR to a folder outside every synced "
        "folder and try again."
    )


def _hermes_binary_after_install() -> str | None:
    """The Hermes launcher after an install, with its install dirs on PATH.

    The installers drop the launcher under the user's home, ``~/.local/bin``
    on POSIX and ``LOCALAPPDATA\\hermes\\bin`` on Windows, so that directory
    is put on this process' PATH before the search.
    """
    from hermes_backend import find_hermes_cli

    local_bin = Path.home() / ".local" / "bin"
    if local_bin.is_dir():
        _add_to_process_path(local_bin)
    if platform.system() == "Windows":
        # Measured on the machine the installer ran on: the launcher lands in
        # %LOCALAPPDATA%\hermes\bin, beside the managed uv, and the
        # installer's own "PATH already configured" only helps shells started
        # afterwards -- not this process, which was running during the
        # install.
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        hermes_bin = local / "hermes" / "bin"
        if hermes_bin.is_dir():
            _add_to_process_path(hermes_bin)
    return find_hermes_cli()


def install_hermes(log: Callable[[str], None]) -> str | None:
    """Run Hermes' official installer for this platform and report the result.

    The same discipline as install_claude: the installer's exit code is
    advisory, and a working `hermes` afterwards is the fact that counts.
    """
    argv = _hermes_install_argv()
    if argv is None:
        log(_hermes_missing_prereq_message())
        return None

    log(
        "Downloading and running the official Hermes installer. This usually takes a minute or two."
    )
    heard: list[str] = []

    def logged(line: str) -> None:
        heard.append(line)
        log(line)

    rc = _run_logged_process(argv, logged, env=_hermes_installer_env(log))
    if rc is None:
        return None

    binary = _hermes_binary_after_install()
    if binary is None:
        log(f"The installer finished with exit code {rc} but `hermes` was not found afterwards.")
        problem = _hermes_bootstrap_failure(heard)
        if problem:
            log(problem)
        return None

    log(f"Installed: {binary}")
    folder = Path(binary).parent
    try:
        changed = ensure_on_path(folder)
        if changed:
            log(f"Added {folder} to {changed}.")
    except OSError as exc:
        log(f"Installed, but adding it to PATH failed: {exc}")
    return binary


MUSE_INSTALL_SH_URL = "https://dev.meta.ai/install.sh"

# uv's official installers. BlindPilot installs uv itself when a backend's
# Python tooling is needed on a computer that has neither uv nor a Python:
# a fresh computer has neither, and without a bootstrap the muse.ai install
# could only tell the user to go and fetch the tooling by hand.
UV_INSTALL_PS1_URL = "https://astral.sh/uv/install.ps1"
UV_INSTALL_SH_URL = "https://astral.sh/uv/install.sh"

# Antigravity CLI ships Google's own per-platform script installers (not npm):
# a native agy binary in ~/.local/bin, or %LOCALAPPDATA%\agy\bin on Windows,
# that updates itself from then on.
ANTIGRAVITY_INSTALL_PS1_URL = "https://antigravity.google/cli/install.ps1"
ANTIGRAVITY_INSTALL_SH_URL = "https://antigravity.google/cli/install.sh"


def install_antigravity(log: Callable[[str], None]) -> str | None:
    """Run Google's Antigravity CLI installer and report the result.

    As with the other script installers, a working `agy` afterwards is the
    fact that counts, not the installer's exit code.
    """
    argv = _script_installer_argv(
        f"irm {ANTIGRAVITY_INSTALL_PS1_URL} | iex", ANTIGRAVITY_INSTALL_SH_URL
    )
    if argv is None:
        log(_missing_prereq_message("Antigravity's installer"))
        return None
    log(
        "Downloading and running the official Antigravity CLI installer. This usually takes under a minute."
    )
    rc = _run_logged_process(argv, log)
    if rc is None:
        return None
    binary = find_backend_cli(BACKEND_ANTIGRAVITY)
    if binary is None:
        log(f"The installer finished with exit code {rc} but `agy` was not found afterwards.")
        return None
    log(f"Installed: {binary}")
    try:
        changed = ensure_on_path(Path(binary).parent)
        if changed:
            log(f"Added {Path(binary).parent} to {changed}.")
    except OSError as exc:
        log(f"Installed, but adding it to PATH failed: {exc}")
    return binary


def _muse_install_argv() -> list[str] | None:
    """Muse's official installer, run wherever Muse can actually live.

    Muse's CLI ships for macOS and Linux only, so on Windows there is no
    PowerShell one-liner to run natively: the install happens inside WSL, if
    WSL is here, and cannot happen at all if it is not.
    """
    if platform.system() == "Windows":
        # The only prerequisite is WSL itself. This used to also require
        # Muse to be installed already, which made a first install on a
        # fresh computer impossible: the installer was refused exactly
        # when there was nothing installed yet.
        launcher = muse_wsl_exe()
        if launcher is None:
            return None
        # curl inside the distribution, bash inside it: the script is
        # POSIX-only, so running it from Windows is not an option to fall
        # back to.
        return [launcher, "-e", "bash", "-c", f"curl -fsSL {MUSE_INSTALL_SH_URL} | bash"]
    if shutil.which("curl") is None:
        return None
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:
        return None
    return [shell, "-c", f"curl -fsSL {MUSE_INSTALL_SH_URL} | bash"]


def _muse_missing_prereq_message() -> str:
    if platform.system() == "Windows":
        return (
            "Muse Code runs on macOS and Linux, so on Windows it is installed "
            "inside WSL -- and WSL with a working distribution was not found on "
            "this computer. Install WSL first, then try again."
        )
    return _missing_prereq_message("Muse's installer")


def install_muse(log: Callable[[str], None]) -> str | None:
    """Run Muse's official installer for this platform and report the result.

    Same discipline as install_hermes: the installer's exit code is advisory,
    and a working `muse` afterwards is the fact that counts. On Windows that
    launcher lives inside WSL, so discovery is reset before the check -- the
    first probe's answer predates the install and must not outlive it.
    """
    argv = _muse_install_argv()
    if argv is None:
        log(_muse_missing_prereq_message())
        return None

    log(
        "Downloading and running the official Muse Code installer. "
        "This usually takes under a minute."
    )
    rc = _run_logged_process(argv, log)
    if rc is None:
        return None

    reset_muse_discovery()
    if not muse_installed():
        log(f"The installer finished with exit code {rc} but `muse` was not found afterwards.")
        return None
    path = muse_cli_path()
    log(f"Installed: {path}")
    return path


def _path_shells() -> str:
    """The shells worth naming when telling the user to open a new terminal."""
    if platform.system() == "Windows":
        return "cmd, PowerShell, pwsh and Windows Terminal"
    return "Terminal and iTerm"


def install_claude(log: Callable[[str], None]) -> str | None:
    """Run the official native installer for this platform and put it on PATH.

    Streams installer output line by line to *log* (so the caller can show and
    speak progress) and returns the path to the installed binary, or None if
    the install did not produce a working `claude`.
    """
    argv = _install_argv()
    if argv is None:
        log(_missing_prereq_message())
        return None

    log("Downloading and running the Claude Code installer. This usually takes under a minute.")
    rc = _run_logged_process(argv, log)
    if rc is None:
        return None

    # The installer's own exit code is advisory. What matters is whether a
    # working binary exists afterwards, so look before reporting failure.
    _add_to_process_path(_native_bin_dir())
    binary = _find_claude()
    if binary is None:
        log(f"The installer finished with exit code {rc} but `claude` was not found afterwards.")
        return None

    log(f"Installed: {binary}")
    folder = Path(binary).parent
    try:
        changed = ensure_on_path(folder)
        if changed:
            log(
                f"Added {folder} to {changed}. Open a new terminal window for "
                f"{_path_shells()} to see it."
            )
        else:
            log(f"Already on your PATH. `claude` will work in {_path_shells()}.")
    except OSError as exc:
        log(f"Installed, but adding it to PATH failed: {exc}")
    return binary


_NPM_BACKEND_PACKAGES = {
    BACKEND_CODEX: "@openai/codex",
    BACKEND_FREEBUFF: "freebuff",
    BACKEND_OPENCODE: "opencode-ai",
    BACKEND_COMMANDCODE: "command-code",
    BACKEND_GEMINI: "@google/gemini-cli",
    BACKEND_COPILOT: "@github/copilot",
}

# Backends whose turn is one process per message. A message typed while one
# runs cannot join it, so it is queued and sent when the turn ends, and Steer
# stops the turn and resumes with the instruction.
_QUEUEING_BACKENDS = frozenset(
    {
        BACKEND_COMMANDCODE,
        BACKEND_GEMINI,
        BACKEND_ANTIGRAVITY,
        BACKEND_MUSEAI,
        BACKEND_FREEBUFF,
        BACKEND_COPILOT,
    }
)


def _backend_installs_with_npm(backend: str) -> bool:
    """Whether this backend is one BlindPilot can install for the user.

    Not every backend ships on npm: Hermes installs itself from its own
    installer, so telling the user that npm is required would send them after
    the wrong thing entirely.
    """
    return normalize_backend(backend) in _NPM_BACKEND_PACKAGES


NODE_RELEASE_INDEX_URL = "https://nodejs.org/dist/index.json"
NODE_RELEASE_BASE_URL = "https://nodejs.org/dist"
_NODE_MINIMUM_MAJOR = 18


def _managed_npm_prefix() -> Path:
    """A writable per-user prefix owned by BlindPilot, never a system folder."""
    return blindpilot_data_dir() / "npm"


def _managed_npm_bin_dir() -> Path:
    prefix = _managed_npm_prefix()
    return prefix if platform.system() == "Windows" else prefix / "bin"


def _node_runtime_root() -> Path:
    return blindpilot_data_dir() / "runtimes" / "node"


def _node_archive_spec(
    version: str, system: str | None = None, machine: str | None = None
) -> tuple[str, str] | None:
    """Return the official Node archive name and its extracted folder."""
    system = system or platform.system()
    machine = (machine or platform.machine()).casefold()
    os_name = {"Windows": "win", "Darwin": "darwin", "Linux": "linux"}.get(system)
    arch = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if not os_name or not arch:
        return None
    stem = f"node-{version}-{os_name}-{arch}"
    extension = ".zip" if system == "Windows" else ".tar.gz"
    return stem + extension, stem


def _managed_node_dir() -> Path | None:
    """Newest complete portable Node runtime previously installed by BlindPilot."""
    spec = _node_archive_spec("v0.0.0")
    if spec is None:
        return None
    marker = "-".join(spec[1].split("-")[-2:])
    executable = "node.exe" if platform.system() == "Windows" else "bin/node"
    npm = "npm.cmd" if platform.system() == "Windows" else "bin/npm"
    candidates: list[tuple[tuple[int, ...], Path]] = []
    try:
        folders = _node_runtime_root().glob(f"node-v*-{marker}")
        for folder in folders:
            match = re.match(r"node-v(\d+(?:\.\d+)+)-", folder.name)
            if match and (folder / executable).is_file() and (folder / npm).is_file():
                candidates.append((tuple(int(part) for part in match.group(1).split(".")), folder))
    except OSError:
        return None
    return max(candidates, default=((), None), key=lambda item: item[0])[1]


def _managed_npm() -> str | None:
    runtime = _managed_node_dir()
    if runtime is None:
        return None
    relative = "npm.cmd" if platform.system() == "Windows" else "bin/npm"
    return str(runtime / relative)


def activate_managed_cli_paths() -> None:
    """Make BlindPilot-managed Node and backend launchers usable this run."""
    runtime = _managed_node_dir()
    if runtime is not None:
        _add_to_process_path(runtime if platform.system() == "Windows" else runtime / "bin")
    managed_bin = _managed_npm_bin_dir()
    if managed_bin.is_dir():
        _add_to_process_path(managed_bin)


def _find_npm() -> str | None:
    return shutil.which("npm") or _managed_npm()


def _automatic_npm_install_available() -> bool:
    return _find_npm() is not None or _node_archive_spec("v0.0.0") is not None


def _fetch_url_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    with open_url(request, timeout=30) as response:
        return response.read()


def _safe_extract_node_archive(archive_path: Path, destination: Path) -> None:
    """Extract one verified Node archive without permitting path traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if archive_path.suffix.casefold() == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if os.path.commonpath((str(root), str(target))) != str(root):
                    raise OSError("The Node.js archive contains an unsafe path.")
            archive.extractall(destination)
        return
    with tarfile.open(archive_path, mode="r:gz") as archive:
        archive.extractall(destination, filter="data")


def install_portable_node(log: Callable[[str], None]) -> str | None:
    """Install the latest Node LTS and npm for this user, with no elevation."""
    existing = _managed_npm()
    if existing:
        node_bin = Path(existing).parent
        try:
            changed = ensure_on_path(node_bin)
            if changed:
                log(f"Added {node_bin} to {changed}.")
        except OSError as exc:
            log(f"Node.js is installed, but adding it to PATH failed: {exc}")
        return existing
    if _node_archive_spec("v0.0.0") is None:
        log(
            f"Automatic Node.js installation is not available for "
            f"{platform.system()} {platform.machine()}."
        )
        return None

    log("Node.js and npm were not found. Installing the latest Node.js LTS for this user.")
    try:
        releases = json.loads(_fetch_url_bytes(NODE_RELEASE_INDEX_URL).decode("utf-8"))
        release = next(
            item
            for item in releases
            if isinstance(item, dict)
            and item.get("lts")
            and isinstance(item.get("version"), str)
            and int(item["version"].lstrip("v").split(".", 1)[0]) >= _NODE_MINIMUM_MAJOR
        )
        version = release["version"]
        archive_name, extracted_name = _node_archive_spec(version) or ("", "")
        if not archive_name:
            raise OSError("No official Node.js archive is available for this computer.")
        release_url = f"{NODE_RELEASE_BASE_URL}/{version}"
        checksums = _fetch_url_bytes(f"{release_url}/SHASUMS256.txt").decode("utf-8")
        checksum = next(
            line.split()[0] for line in checksums.splitlines() if line.split()[1:] == [archive_name]
        )
    except (OSError, ValueError, StopIteration, urllib.error.URLError) as exc:
        log(f"Could not discover the current Node.js LTS release: {exc}")
        return None

    runtime_root = _node_runtime_root()
    destination = runtime_root / extracted_name
    if destination.is_dir():
        npm = destination / ("npm.cmd" if platform.system() == "Windows" else "bin/npm")
        return str(npm) if npm.is_file() else None

    try:
        runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="install-", dir=runtime_root) as temporary:
            temporary_path = Path(temporary)
            archive_path = temporary_path / archive_name
            log(f"Downloading Node.js {version} from nodejs.org...")
            request = urllib.request.Request(
                f"{release_url}/{archive_name}",
                headers={"User-Agent": _user_agent()},
            )
            digest = hashlib.sha256()
            with (
                open_url(request, timeout=60) as response,
                open(archive_path, "wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest().casefold() != checksum.casefold():
                raise OSError("The downloaded Node.js archive failed SHA-256 verification.")
            extract_root = temporary_path / "extracted"
            _safe_extract_node_archive(archive_path, extract_root)
            extracted = extract_root / extracted_name
            if not extracted.is_dir():
                raise OSError("The Node.js archive did not contain its expected folder.")
            try:
                os.replace(extracted, destination)
            except FileExistsError:
                pass  # Another install thread completed the same release first.
    except (OSError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        log(f"Node.js installation failed: {exc}")
        return None

    npm_path = destination / ("npm.cmd" if platform.system() == "Windows" else "bin/npm")
    node_bin = destination if platform.system() == "Windows" else destination / "bin"
    if not npm_path.is_file():
        log("Node.js was extracted, but npm was missing from the installed runtime.")
        return None
    try:
        changed = ensure_on_path(node_bin)
        if changed:
            log(f"Added {node_bin} to {changed}.")
    except OSError as exc:
        log(f"Node.js was installed, but adding it to PATH failed: {exc}")
    log(f"Installed Node.js and npm: {npm_path}")
    return str(npm_path)


def _npm_environment(npm: str) -> dict[str, str]:
    """npm's own directory first, then everything a terminal would have.

    npm is itself a shim that has to find `node`, so it fails from the macOS
    Dock for exactly the reason the provider CLIs do.
    """
    env = subprocess_env(npm)
    env.update(
        {
            "CI": "1",
            "npm_config_yes": "true",
            "npm_config_progress": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
        }
    )
    return env


def _npm_install_argv(backend: str, latest: bool = False) -> list[str] | None:
    """The npm command that installs a backend, or None if npm is unavailable.

    `latest` pins the package's latest tag, which is what an update wants.
    """
    package = _NPM_BACKEND_PACKAGES.get(normalize_backend(backend))
    npm = _find_npm()
    if not package or not npm:
        return None
    return [
        npm,
        "install",
        "--global",
        "--prefix",
        str(_managed_npm_prefix()),
        f"{package}@latest" if latest else package,
    ]


def _npm_update_argv(backend: str) -> list[str] | None:
    return _npm_install_argv(backend, latest=True)


def _managed_backend_binary(backend: str) -> str | None:
    executable = BACKENDS[normalize_backend(backend)].executable
    suffixes = (".exe", ".cmd", ".ps1", "") if platform.system() == "Windows" else ("",)
    return next(
        (
            str(candidate)
            for suffix in suffixes
            if (candidate := _managed_npm_bin_dir() / f"{executable}{suffix}").is_file()
        ),
        None,
    )


def _run_logged_process(
    argv: list[str], log: Callable[[str], None], env: dict[str, str] | None = None
) -> int | None:
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env if env is not None else subprocess_env(argv[0]),
            **no_window_kwargs(),
        )
    except OSError as exc:
        log(f"The installer could not be started: {exc}")
        return None
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
    return proc.wait()


def _install_failure_message(backend: str) -> str:
    """What to hear when an install did not complete: the command, unspliced."""
    info = BACKENDS[normalize_backend(backend)]
    return (
        "The install did not complete. Read the installer output, or install "
        f"{info.label} yourself. {info.install_command} Then click Check Again."
    )


def _install_uv(log: Callable[[str], None]) -> bool:
    """Install uv with its official installer. True when uv exists afterwards.

    The installer's exit code is advisory, like the other script
    installers: a `uv` that discovery can find afterwards is the fact
    that counts.
    """
    from museai_backend import find_uv

    argv = _script_installer_argv(f"irm {UV_INSTALL_PS1_URL} | iex", UV_INSTALL_SH_URL)
    if argv is None:
        return False
    log("Installing uv, the tool manager muse-cli is installed with...")
    if _run_logged_process(argv, log) is None:
        return False
    return find_uv() is not None


def install_museai(log: Callable[[str], None], upgrade: bool = False) -> str | None:
    """Install or upgrade muse-cli, the client muse.ai is driven through.

    It is a Python package, so uv installs it as a tool (its own environment,
    a launcher on PATH) and pip for this user is the fallback.
    """
    from museai_backend import MUSEAI_MISSING_PREREQ, museai_install_argv

    label = backend_label(BACKEND_MUSEAI)
    argv = museai_install_argv(upgrade=upgrade)
    if argv is None:
        # Neither uv nor a usable Python: a fresh computer. Install uv with
        # its official installer -- the same script-installer pattern the
        # other backends use -- then ask again.
        if _install_uv(log):
            argv = museai_install_argv(upgrade=upgrade)
    if argv is None:
        log(MUSEAI_MISSING_PREREQ)
        return None
    log(f"{'Updating' if upgrade else 'Installing'} {label}'s command line, muse-cli...")
    rc = _run_logged_process(argv, log)
    binary = find_backend_cli(BACKEND_MUSEAI)
    if binary is None:
        log(f"muse-cli was not found afterwards (exit code {rc}).")
        return None
    log(f"muse-cli is ready at {binary}. Sign in with muse-cli auth export.")
    return binary


def install_backend(backend: str, log: Callable[[str], None]) -> str | None:
    """Install one selected backend and return its discovered executable."""
    backend = normalize_backend(backend)
    if backend == BACKEND_CLAUDE:
        return install_claude(log)
    if backend == BACKEND_HERMES:
        # Hermes is not on npm. Its official per-platform script installer is
        # the install path, the same shape as Claude's.
        return install_hermes(log)
    if backend == BACKEND_MUSE:
        return install_muse(log)
    if backend == BACKEND_MUSEAI:
        return install_museai(log)
    if backend == BACKEND_ANTIGRAVITY:
        return install_antigravity(log)
    label = backend_label(backend)
    npm = _find_npm()
    if npm is None:
        npm = install_portable_node(log)
    if npm is None:
        log(f"npm could not be installed, so BlindPilot cannot install {label} automatically.")
        return None
    argv = _npm_install_argv(backend)
    if argv is None:
        log(f"npm could not be installed, so BlindPilot cannot install {label} automatically.")
        return None
    if backend == BACKEND_CODEX:
        _drop_codex_server(log)
    if backend == BACKEND_OPENCODE:
        # Same reason as an update: npm cannot replace a running executable.
        stop_opencode_server()
    log(f"Installing {label} with npm. This can take a minute.")
    rc = _run_logged_process(argv, log, env=_npm_environment(npm))
    if rc is None:
        return None
    _add_to_process_path(_managed_npm_bin_dir())
    binary = _managed_backend_binary(backend) or find_backend_cli(backend)
    if binary is None:
        log(f"npm finished with exit code {rc}, but {label} was not found afterwards.")
        return None
    try:
        changed = ensure_on_path(Path(binary).parent)
        if changed:
            log(f"Added {Path(binary).parent} to {changed}.")
    except OSError as exc:
        log(f"Installed, but adding it to PATH failed: {exc}")

    # Freebuff's npm package is only a launcher. Asking every backend for its
    # version both verifies it can start and makes Freebuff download and verify
    # the native binary before the setup wizard advances to sign-in.
    log(f"Verifying the {label} installation...")
    verify_rc = _run_logged_process([binary, "--version"], log, env=_npm_environment(npm))
    if verify_rc is None:
        return None
    if verify_rc != 0:
        log(f"{label} was installed but failed its startup check (exit code {verify_rc}).")
        return None
    log(f"Installed and verified: {binary}")
    return binary


def _executable_version(binary: str) -> str:
    """Return one provider executable's own version text."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=subprocess_env(binary),
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return ((result.stdout or "") + (result.stderr or "")).strip()


def _repair_claude_native_update(binary: str, log: Callable[[str], None]) -> bool:
    """Make the Windows launcher use the newest downloaded Claude version."""
    if platform.system() != "Windows" or Path(binary).suffix.lower() != ".exe":
        # An npm `claude.cmd` shim is not the native launcher; a PE image
        # copied over it would break it.
        return True
    versions = Path.home() / ".local" / "share" / "claude" / "versions"
    try:
        candidates = [
            path for path in versions.iterdir() if path.is_file() and version_tuple(path.name)
        ]
    except OSError:
        return True
    if not candidates:
        return True
    newest = max(candidates, key=lambda path: version_tuple(path.name))
    current_version = version_tuple(_executable_version(binary))
    newest_version = version_tuple(newest.name)
    if not newest_version or newest_version <= current_version:
        return True
    try:
        shutil.copy2(newest, binary)
    except OSError as exc:
        log(f"Claude downloaded {newest.name}, but its launcher could not be updated: {exc}")
        return False
    verified = version_tuple(_executable_version(binary))
    if verified < newest_version:
        log("Claude's launcher still reports an older version after updating.")
        return False
    log(f"Activated Claude Code {newest.name} in the launcher.")
    return True


def _drop_codex_server(log: Callable[[str], None]) -> None:
    """Let go of every held Codex app-server before npm replaces its executable.

    Windows will not overwrite an executable that is running. Codex may be held
    per ChatGPT-plan Account, so this reaches every Account's process: leaving
    one behind fails the update exactly as one left behind used to.
    """
    log("Stopping Codex's app-server so its executable can be replaced...")
    backend_pool.pool().drop_backend(BACKEND_CODEX)


def update_backend(backend: str, log: Callable[[str], None]) -> bool:
    """Update an installed provider CLI and stream accessible progress."""
    backend = normalize_backend(backend)
    label = backend_label(backend)
    binary = _find_claude() if backend == BACKEND_CLAUDE else find_backend_cli(backend)
    if binary is None:
        log(f"{label} is not installed yet.")
        return False
    if backend == BACKEND_MUSEAI:
        return install_museai(log, upgrade=True) is not None
    previous_freebuff_model = ""
    if backend == BACKEND_FREEBUFF:
        _models, previous_freebuff_model, _error = freebuff_model_options()
    if backend == BACKEND_CLAUDE:
        argv = [binary, "update"]
    elif backend == BACKEND_HERMES:
        # Not on npm: its official installer is the update path too. It
        # upgrades in place, and success is measured by the launcher
        # afterwards, not the exit code.
        hermes_argv = _hermes_install_argv()
        if hermes_argv is None:
            log(_hermes_missing_prereq_message())
            return False
        # The iex'd PowerShell installer never exits on its own -- install.ps1
        # says so deliberately, so it cannot close the user's window -- so the
        # process exit code is whatever the last native command inside the
        # installer returned, and a negative one arrives unsigned: a friend's
        # "exited with code 4294901760" is plain -65536, measured against this
        # machine's PowerShell. The installer's own failure guard (assigning
        # $global:LASTEXITCODE, not exiting) does not reach the process code
        # either. The exit code says nothing about the update; the version the
        # launcher reports afterwards is the only fact that does.
        before = version_tuple(_executable_version(binary))
        log(f"Running the official {label} installer to update...")
        rc = _run_logged_process(hermes_argv, log, env=_hermes_installer_env(log))
        if rc is None:
            return False
        found = _hermes_binary_after_install()
        if found is None:
            if rc != 0:
                log(f"{label} update exited with code {rc}.")
            log(f"{label} update finished, but `hermes` was not found afterwards.")
            return False
        text = _executable_version(found)
        if version_tuple(text) > before:
            log(f"{label} updated. It now reports {text}.")
        elif rc != 0:
            log(
                f"{label} is still at its current version and the installer did not "
                "finish cleanly, so the update may not have completed. Read the "
                "installer output above, or try the update again -- running it "
                "again is safe."
            )
            return False
        log(f"{label} is up to date.")
        return True
    elif backend == BACKEND_MUSE:
        # Same shape as Hermes: the official script installer upgrades in
        # place, and success is a working launcher afterwards -- inside WSL
        # on Windows.
        muse_argv = _muse_install_argv()
        if muse_argv is None:
            log(_muse_missing_prereq_message())
            return False
        log(f"Running the official {label} installer to update...")
        rc = _run_logged_process(muse_argv, log)
        if rc is None:
            return False
        if not muse_installed():
            log(f"{label} update finished, but `muse` was not found afterwards.")
            return False
        log(f"{label} is up to date.")
        return True
    else:
        if _find_npm() is None and install_portable_node(log) is None:
            log(f"npm could not be installed, so BlindPilot cannot update {label} automatically.")
            return False
        updating = _npm_update_argv(backend)
        if updating is None:
            log(f"npm could not be found, so BlindPilot cannot update {label} automatically.")
            return False
        argv = updating
    if backend == BACKEND_CODEX:
        _drop_codex_server(log)
    if backend == BACKEND_OPENCODE:
        # The server BlindPilot has been talking to *is* the executable npm is
        # about to replace, and Windows will not overwrite one that is running.
        # It is started again by the next thing that needs it.
        log("Stopping opencode's server so its executable can be replaced...")
        stop_opencode_server()
    log(f"Checking for {label} updates...")
    npm = _find_npm() if backend != BACKEND_CLAUDE else None
    rc = _run_logged_process(argv, log, env=_npm_environment(npm) if npm else None)
    if rc != 0:
        log(f"{label} update exited with code {rc}.")
        return False
    if backend == BACKEND_CLAUDE and not _repair_claude_native_update(binary, log):
        return False
    if backend != BACKEND_CLAUDE:
        _add_to_process_path(_managed_npm_bin_dir())
        managed_binary = _managed_backend_binary(backend)
        if managed_binary is None:
            log(f"{label} updated, but its executable was not found afterwards.")
            return False
        try:
            changed = ensure_on_path(Path(managed_binary).parent)
            if changed:
                log(f"Added {Path(managed_binary).parent} to {changed}.")
        except OSError as exc:
            log(f"{label} updated, but adding it to PATH failed: {exc}")
        verify_rc = _run_logged_process(
            [managed_binary, "--version"], log, env=_npm_environment(npm or "")
        )
        if verify_rc is None:
            return False
        if verify_rc != 0:
            log(f"{label} updated but failed its startup check (exit code {verify_rc}).")
            return False
    if backend == BACKEND_FREEBUFF:
        invalidate_backend_cache(BACKEND_FREEBUFF)
        models, _current, _error = freebuff_model_options()
        selected = (
            previous_freebuff_model
            if previous_freebuff_model in models
            else FREEBUFF_PREFERRED_MODEL
            if FREEBUFF_PREFERRED_MODEL in models
            else models[0]
            if models
            else FREEBUFF_PREFERRED_MODEL
        )
        try:
            set_freebuff_model(selected)
        except OSError as exc:
            log(f"{label} updated, but its model selection could not be restored: {exc}")
            return False
    log(f"{label} is up to date.")
    return True
