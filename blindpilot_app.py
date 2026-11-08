"""BlindPilot, an accessible wxPython frontend for coding-agent CLIs.

Based on the original Claude Code Reader application. BlindPilot retains the
original application's accessibility-first design while adding pluggable
Claude Code, Codex, FreeBuff, opencode, Hermes, Muse Code, and Command Code
backends.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
SPDX-License-Identifier: MIT

Uses wxPython so the UI is built from native widgets per platform — on macOS
the responses list is a real NSTableView (the same widget Finder uses), which
VoiceOver reads cleanly with no interaction quirks. On Windows the same code
uses Win32 widgets that NVDA / JAWS handle natively.

v2 segments each assistant turn into navigable *rows* (a header, one row per
paragraph / heading / list / quote, and one pristine row per fenced code block)
via the keystone parser in ``markdown_rows``. The flat list of rows sits above
the prompt box; Ctrl+Up from the prompt enters the newest row, while arrow
keys at either end of the list stay in the list. Tab is the only navigation key
that moves from the responses into the prompt.

Multi-session: a tab strip across the top selects one of the window's
switchable conversation pages.
Each tab owns its own conversation (session_id, prompt, rows) and its subprocess
runs with that directory as cwd, mirroring how a user would open multiple
terminal sessions in different project folders.
"""

from __future__ import annotations

import json  # noqa: F401
import logging
import re  # noqa: F401
import shutil  # noqa: F401
import tempfile  # noqa: F401
import os
import platform
import subprocess  # noqa: F401
import sys
import threading
import time
import uuid  # noqa: F401
from pathlib import Path
from typing import Callable, Optional  # noqa: F401

import wx
import wx.adv
from accessible_ai.storage.paths import bundle_dir as _mac_bundle_dir  # noqa: F401

# The screen reader is spoken to through this module. The two functions are
# imported by name, so a caller here can still replace them. The state that
# goes with them is read as screen_speech.<name>, never imported, because
# announce() reassigns it and an imported copy would go stale.
import screen_speech
from screen_speech import _speak_windows_announcement, announce, repeat_last_announcement  # noqa: F401

# How things are worded when they are read aloud or shown as a one-line
# label. Imported by name, so a caller here can still replace them; the
# helpers themselves are pure and hold no state of their own.
from spoken_text import (  # noqa: F401
    _FENCED_BLOCKS,
    _context_line,
    _result_label,
    _sent_readback,
    _spoken_duration,
)

# Where the settings live and how they are read and written. `_load_config`
# and `_save_config` are imported by name, so a caller here can still replace
# them; the path helpers they use are patched on app_config itself, because
# that is where those calls are made from.
from app_config import (
    APPEARANCE_DARK,  # noqa: F401
    APPEARANCE_LIGHT,  # noqa: F401
    APPEARANCE_RESTART_NOTE,  # noqa: F401
    APPEARANCE_SYSTEM,  # noqa: F401 - read as blindpilot_app.APPEARANCE_SYSTEM
    APPEARANCES,  # noqa: F401 - read as blindpilot_app.APPEARANCES
    CUE_LOOP,  # noqa: F401
    CUE_OFF,  # noqa: F401
    CUE_PERIODIC,  # noqa: F401
    CUE_SECONDS_DEFAULT,  # noqa: F401 - read as blindpilot_app.CUE_SECONDS_DEFAULT
    CUE_SECONDS_MAX,  # noqa: F401
    CUE_SECONDS_MIN,  # noqa: F401
    NARRATION_EVERYTHING,  # noqa: F401 - read as blindpilot_app.NARRATION_EVERYTHING
    NARRATION_KEEP_UP,  # noqa: F401
    NARRATION_MODES,  # noqa: F401
    REMOTE_HERMES,  # noqa: F401
    SETTINGS,  # noqa: F401
    SOUND_CUES,  # noqa: F401
    _ALWAYS_SPOKEN,  # noqa: F401
    _RemoteHermes,  # noqa: F401 - read as blindpilot_app._RemoteHermes
    _Settings,  # noqa: F401 - compatibility for callers
    _load_config,
    _save_config,
    _valid_appearance,
    _valid_cue_seconds,  # noqa: F401
    _valid_model_refresh_minutes,  # noqa: F401 - read as blindpilot_app._valid_model_refresh_minutes
    _valid_progress_cue,  # noqa: F401
)

# The installer names are re-exported so the rest of the application and
# the tests keep reaching them as blindpilot_app.<name>. A name patched
# there is only seen by callers in this module: the installer calls its
# own helpers, and those are patched on cli_install instead.
from cli_install import (
    MUSE_INSTALL_SH_URL,  # noqa: F401 - read as blindpilot_app.MUSE_INSTALL_SH_URL
    PATH_STANZA_MARKER,  # noqa: F401 - read as blindpilot_app.PATH_STANZA_MARKER
    UV_PYTHON_DIR_ENV,  # noqa: F401 - read as blindpilot_app.UV_PYTHON_DIR_ENV
    POSIX_INSTALL_SH_URL,  # noqa: F401 - read as blindpilot_app.POSIX_INSTALL_SH_URL
    WINDOWS_INSTALL_PS1_URL,  # noqa: F401 - read as blindpilot_app.WINDOWS_INSTALL_PS1_URL
    _automatic_npm_install_available,  # noqa: F401 - read as blindpilot_app._automatic_npm_install_available
    _backend_installs_with_npm,  # noqa: F401 - read as blindpilot_app._backend_installs_with_npm
    _find_npm,  # noqa: F401 - read as blindpilot_app._find_npm
    _hermes_missing_prereq_message,  # noqa: F401 - read as blindpilot_app._hermes_missing_prereq_message
    _install_argv,  # noqa: F401 - read as blindpilot_app._install_argv
    _install_failure_message,  # noqa: F401 - read as blindpilot_app._install_failure_message
    _is_on_persistent_path,  # noqa: F401 - read as blindpilot_app._is_on_persistent_path
    _missing_prereq_message,  # noqa: F401 - read as blindpilot_app._missing_prereq_message
    _muse_missing_prereq_message,  # noqa: F401 - read as blindpilot_app._muse_missing_prereq_message
    _npm_install_argv,  # noqa: F401 - read as blindpilot_app._npm_install_argv
    _path_shells,  # noqa: F401 - read as blindpilot_app._path_shells
    ensure_on_path,  # noqa: F401 - read as blindpilot_app.ensure_on_path
    _fallback_claude_paths,  # noqa: F401 - read as blindpilot_app._fallback_claude_paths
    _fetch_url_bytes,  # noqa: F401 - read as blindpilot_app._fetch_url_bytes
    _find_claude,  # noqa: F401
    _folder_is_cloud_synced,  # noqa: F401 - read as blindpilot_app._folder_is_cloud_synced
    _hermes_binary_after_install,  # noqa: F401 - read as blindpilot_app._hermes_binary_after_install
    _hermes_bootstrap_failure,  # noqa: F401 - read as blindpilot_app._hermes_bootstrap_failure
    _hermes_install_argv,  # noqa: F401
    _hermes_installer_env,  # noqa: F401 - read as blindpilot_app._hermes_installer_env
    _muse_install_argv,  # noqa: F401
    _native_bin_dir,  # noqa: F401 - read as blindpilot_app._native_bin_dir
    _node_archive_spec,  # noqa: F401 - read as blindpilot_app._node_archive_spec
    _npm_environment,  # noqa: F401 - read as blindpilot_app._npm_environment
    _npm_update_argv,  # noqa: F401 - read as blindpilot_app._npm_update_argv
    _path_export_line,  # noqa: F401 - read as blindpilot_app._path_export_line
    _path_with_entry,  # noqa: F401 - read as blindpilot_app._path_with_entry
    _posix_persistent_path_dirs,  # noqa: F401 - read as blindpilot_app._posix_persistent_path_dirs
    _repair_claude_native_update,  # noqa: F401 - read as blindpilot_app._repair_claude_native_update
    _run_logged_process,  # noqa: F401 - read as blindpilot_app._run_logged_process
    _same_dir,  # noqa: F401 - read as blindpilot_app._same_dir
    _shell_profile_file,  # noqa: F401 - read as blindpilot_app._shell_profile_file
    _uv_local_python_dir,  # noqa: F401 - read as blindpilot_app._uv_local_python_dir
    _uv_roaming_python_dir,  # noqa: F401 - read as blindpilot_app._uv_roaming_python_dir
    activate_managed_cli_paths,
    ensure_on_posix_path,  # noqa: F401 - read as blindpilot_app.ensure_on_posix_path
    install_backend,  # noqa: F401
    install_claude,  # noqa: F401 - read as blindpilot_app.install_claude
    install_hermes,  # noqa: F401 - read as blindpilot_app.install_hermes
    install_museai,  # noqa: F401 - read as blindpilot_app.install_museai
    keep_bundle_off_child_path,
    update_backend,  # noqa: F401
)

import backend_pool  # noqa: F401
import diagnostics
from dialog_tasks import TaskSlot, _call_after_alive  # noqa: F401
from hermes_sessions_dialog import HermesSessionsDialog, hermes_session_label  # noqa: F401
from new_session_dialog import NewSessionDialog, _recent_folders, create_worktree  # noqa: F401
from connect_dialog import ConnectDialog, _open_web_page  # noqa: F401
from agent_file_list import AgentFileList  # noqa: F401
from conversation_search import (  # noqa: F401
    relay_message,
    relay_setup,
    said_done,
    search_all_conversations,
    search_turns,
)
from main_frame import MainFrame  # noqa: F401
from app_shell import (  # noqa: F401
    APP_MODE_AGENT,
    APP_MODE_CHAT,
    APP_MODE_LABELS,
    APP_NAME,
    CHAT_MODEL_ORDERS,
    KEYBOARD_SHORTCUTS,
    MODEL_REFRESH_MINUTES_DEFAULT,
    ORIGINAL_APP_CREDIT,
    PAD,
    PAD_DIALOG,
    _app_icon_path,
    _remember_folder,
    adopt_full_auto_default,
    bug_report_facts,
    _appearance_for,
    _bring_to_front,
    _destroy_other_top_level_windows,
    _links_dir,
    _raise_running_copy,
    _record_setup_complete,
    _resource_dir,
    _speech_route,
    _tab_chord_notes,
    _valid_chat_model_order,
    about_description,
    create_desktop_shortcut,
    hand_over_link,
    take_handed_links,
)
from agent_files import (  # noqa: F401
    AgentFiles,
    CLAUDE_FILE_KINDS,
    _front_matter,
    add_agent,
    add_hermes_memory,
    add_memory,
    add_taste,
    agent_files,
    claude_files,
    delete_entry,
    delete_memory,
    entry_text,
    hermes_entries,
    hermes_memories,
    memory_folders,
    save_entry,
    taste_category,
    tastes,
)


from earcons import Earcons, _faster_wav  # noqa: F401
from backend_login import _LOOPBACK_PROGRESS, BackendLogin, _first_login_url, _login_speech  # noqa: F401
from conversation_webview import ConversationWebViewPanel, Turn  # noqa: F401
from museai_dialog import MuseAiDialog  # noqa: F401
import main_frame
import session_panel
from session_panel import (  # noqa: F401
    _over_budget_work,
    GUI_BUDGET_MS,
    over_budget_work,
    _BLINDPILOT_SLASH_COMMANDS,
    _CLAUDE_SLASH_COMMANDS,
    _OPENCODE_SLASH_COMMANDS,
    _HERMES_SLASH_COMMANDS,
    _FREEBUFF_SLASH_COMMANDS,
    _COMMANDCODE_SLASH_COMMANDS,
    _SLASH_COMMANDS_REFRESH,
    _slash_commands_cache,
    _slash_commands_cache_key,
    _changed_files_cache,
    _changed_files_cache_key,
    _DIFF_HEADERS,
    PERMISSION_MODES,
    DEFAULT_PERMISSION_MODE,
    _CANCEL_JOIN_SECONDS,
    PROBE_TTL_SECONDS,
    SUBAGENT_ENDED,
    SUBAGENT_LOG_LINES,
    _CYCLE_VALUES,
    _DICTATION_PAUSE_MS,
    _LANG_EXT,
    _MODE_DESCRIPTIONS,
    _MODE_LABELS,
    _MODE_LABEL_BY_VALUE,
    _MODE_VALUES,
    _WORKER_EVENT_BATCH_SIZE,
    _WORKER_EVENT_BUDGET_SECONDS,
    _claude_settings_rule_lines,
    _default_permission_mode,
    _flatten,
    _forget_remote,
    _gui_work,
    _notify_if_away,
    _permission_update_lines,
    _remember_permission_mode,
    _remember_remote,
    _remember_session_rules,
    _remote_sessions,
    _saved_session_rules,
    _show_conversation_items,
    _slash_command_labels,
    _slash_commands_for_backend,
    _static_slash_commands_for_backend,
    _tab_label,
    cached_changed_files,
    cached_slash_commands,
    changed_files,
    forget_changed_files,
    forget_slash_commands,
    question_left_in_the_answer,
    readable_diff,
    remember_changed_files,
    remember_slash_commands,
    _tab_title,
    _short_label,
    SessionPanel,
)
from setup_wizard import (
    SetupWizard,
    _clear_stale_login,  # noqa: F401
    _terminal_running,  # noqa: F401
)  # noqa: F401
from history_dialog import (  # noqa: F401
    _HISTORY_ANY_BACKEND,
    _HISTORY_ANY_GROUP,
    _HISTORY_SCOPES,
    _HISTORY_SCOPE_LABELS,
    _HISTORY_SORTS,
    _SAVE_FAILED,
    HistoryDialog,
    _history_key,
    _last_exchange,
    _save_or_announce,
    conversation_groups,
    group_names,
    sort_history,
)
from view_dialogs import FormattedView, WrappedText  # noqa: F401
from permission_dialog import PermissionDialog, _permission_text  # noqa: F401
from slash_command_dialog import SlashCommandDialog  # noqa: F401
from question_dialog import QuestionDialog  # noqa: F401
from muse_backend import muse_skills  # noqa: F401
from read_view import ReadView, _monospace_font, _open_path  # noqa: F401
from subagent_dialog import SubagentDialog, SubagentEntry  # noqa: F401
from send_to_tab_dialog import RELAY_KINDS, SendToTabDialog  # noqa: F401
from bug_report_dialog import BugReportDialog, _copy_to_clipboard, bug_report_body  # noqa: F401
from settings_files_dialog import SettingsFilesDialog, _reach_settings_file, _settings_label  # noqa: F401
from add_dialogs import MEMORY_TYPES, AddAgentDialog, AddMemoryDialog, AddNoteDialog  # noqa: F401
from saved_prompts_dialog import SavedPromptsDialog, _saved_prompts, _store_saved_prompts  # noqa: F401
from remote_hermes_dialog import RemoteHermesDialog  # noqa: F401
from preferences_dialog import (  # noqa: F401
    MODEL_REFRESH_MINUTES_MAX,
    MODEL_REFRESH_MINUTES_MIN,
    PreferencesDialog,
)
from model_dialog import (  # noqa: F401
    AUTO_MODEL,
    AUTO_MODEL_CHOICE,
    DEFAULT_CHOICE,
    ModelDialog,
    ModelOptions,
    _FALLBACK_EFFORTS,
    _FALLBACK_MODELS,
    _cli_stamp,
    _keep_choice,
    _model_choice,
    _parse_alias_target,
    _parse_current_model,
    _parse_effort_levels,
    _parse_model_aliases,
    _plain,
    _probe_cache,
    _probe_lock,
    _remember_model_options,
    _resolve_claude_aliases,
    _run_claude,
    cached_model_options,
    invalidate_model_options,
    probe_model_options,
)

# The Claude Code turn worker lives in claude_worker; it is re-exported so
# the rest of the application and the tests keep reaching it as
# blindpilot_app.<name>. A name patched here is only seen by callers in
# this module: the worker calls its own helpers, and those are patched
# on claude_worker instead.
from claude_worker import (  # noqa: F401
    ASK_USER_QUESTION_TOOL,
    AUTH_ERROR_MARKERS,
    AUTH_HINT,
    ClaudeWorker,
    _CLAUDE_AGENT_TOOLS,
    _CLAUDE_BYPASS_GAP_NOTE,
    _CLAUDE_PERMISSION_PROMPT_TOOL,
    _CLAUDE_REFUSAL_MARKERS,
    _CANCEL_DRAIN_SECONDS,
    _CANCEL_WAKE,
    _claude_questions,
    _looks_like_auth_error,
    _looks_like_refusal,
    _tool_result_text,
)
from conversation_items import ConversationItem, ConversationItemsDialog  # noqa: F401
import remote_sessions
from app_updater import (
    ReleaseInfo,  # noqa: F401
    UpdateError,  # noqa: F401
    clear_pending_failure,  # noqa: F401
    fetch_latest_release,  # noqa: F401
    pending_failure,  # noqa: F401
    sweep_temporary_files,
    version_tuple,
)
from agent_backends import (
    _tool_use_label,  # noqa: F401 - read as blindpilot_app._tool_use_label
    BACKEND_CLAUDE,  # noqa: F401
    BACKEND_CODEX,  # noqa: F401
    BACKEND_COMMANDCODE,  # noqa: F401
    BACKEND_COPILOT,  # noqa: F401
    BACKEND_FREEBUFF,  # noqa: F401
    BACKEND_GEMINI,  # noqa: F401
    BACKEND_HERMES,  # noqa: F401
    BACKEND_IDS,  # noqa: F401
    BACKEND_LABELS,  # noqa: F401
    BACKEND_MUSEAI,  # noqa: F401
    BACKEND_OPENCODE,  # noqa: F401
    BACKENDS,  # noqa: F401
    backend_label,  # noqa: F401
    blindpilot_data_dir,  # noqa: F401
    migrate_macos_legacy_dirs,
    find_backend_cli,  # noqa: F401 - read as blindpilot_app.find_backend_cli
    normalize_backend,
    settings_files,  # noqa: F401
    worker_class,  # noqa: F401
)
from process_utils import (
    _git,  # noqa: F401
    no_window_kwargs,  # noqa: F401
)
from hermes_backend import (
    wsl_path_to_windows,  # noqa: F401
)

from markdown_rows import (
    Row,  # noqa: F401
    _one_line,  # noqa: F401
    _row_at,  # noqa: F401
    _starts_of,  # noqa: F401
    _last_block_start,  # noqa: F401
    _search_key,  # noqa: F401
    parse_response,  # noqa: F401
    reassemble_all,  # noqa: F401
)
from session_history import (
    HistoryEntry,  # noqa: F401
    claude_project_slug,  # noqa: F401
    list_history,  # noqa: F401
    load_turns,  # noqa: F401
)


# The two spacings every sizer border goes through, always as
# window.FromDIP(PAD). PAD is the gap between controls and the edge of the
# main window; PAD_DIALOG is the edge of a dialog and its button row. One
# pair rather than a value per screen, so a label and the control under it
# share a left edge.


# Newest last, and bounded: a stalled window produces one entry per keystroke,
# and the record must not grow without limit on a machine that is having a bad
# day. Read by the tests that guard a handler, written only by `_gui_work`.


_ORIGINAL_CALL_AFTER = wx.CallAfter
_ORIGINAL_CALL_LATER = wx.CallLater


def _measured_call_after(callback, *args, **kwargs):
    name = getattr(callback, "__qualname__", getattr(callback, "__name__", "callback"))
    return _ORIGINAL_CALL_AFTER(_gui_work(f"CallAfter: {name}", lambda: callback(*args, **kwargs)))


wx.CallAfter = _measured_call_after  # type: ignore[assignment]


def _measured_call_later(milliseconds, callback, *args, **kwargs):
    name = getattr(callback, "__qualname__", getattr(callback, "__name__", "callback"))
    return _ORIGINAL_CALL_LATER(
        milliseconds,
        _gui_work(f"CallLater: {name}", lambda: callback(*args, **kwargs)),
    )


wx.CallLater = _measured_call_later  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# Installing Claude Code, and making it visible to every shell.
#
# Both official native installers are documented at code.claude.com/docs/en/setup.
# Neither needs administrator rights or Node.js: they drop a real binary in
# ~/.local/bin and self-update from then on.
# ---------------------------------------------------------------------------


# How often a background timer re-reads a tab's model catalog, in minutes, and
# the range Preferences offers. The probe costs a provider CLI start each time
# -- and on opencode that can mean starting its server -- so 0 turns the
# refresh off for anyone who would rather not pay that, and the default is the
# probe's own TTL so a cached answer is refreshed just as it would go stale.


# Slash commands the user can pick from the slash-command picker. Commands
# marked [BlindPilot] are handled by the frontend; the rest are provider-only.


# opencode's own commands are not a fixed list: a project can define its own,
# and BlindPilot reads whichever ones this directory has. Only /connect is
# always there, because that one is BlindPilot's.


# These have actual frontend or prompt equivalents; terminal-only commands
# are explained locally instead of accidentally sent to the model as text.


# Hermes' own commands. Curated rather than complete: it ships about 120, and
# the ones left out are terminal drawing and input controls (/redraw, /mouse,
# /density, the status-bar toggles) that do nothing in a window read by a
# screen reader, plus the aliases of commands already listed under their real
# name. The picker is a discovery aid, not the limit -- the Hermes worker asks
# Hermes itself what it recognises, so a command missing from this list, a
# skill, a bundle, or one a plugin adds can still be typed and will run.


# Before any setting is read: an install that predates the macOS conventions
# keeps its files in ~/.config and ~/.local/share, and they belong in
# ~/Library/Application Support. The move is on the import path because
# `SETTINGS` immediately below reads config.json to build itself.
migrate_macos_legacy_dirs()


APP_VERSION = "0.55.12"


# Every chord BlindPilot has, grouped by where it works, for Help, Keyboard
# Shortcuts (F1). On macOS the Ctrl chords are Cmd.


# True for the length of a packaged startup check. Nothing a check does may
# take the focus of whoever is running it: they are working in another window,
# and on this application's users that means moving their screen reader too.
_STARTUP_CHECK = False


# How long the process may take to die of its own accord once its window
# has closed, before it is ended outright. Every shutdown step the
# application owns is bounded in seconds, so this only ever fires when
# something outside them — a thread a library started and never made a
# daemon of, which Python waits on before it will exit — would otherwise
# keep BlindPilot alive indefinitely with no window open.
_EXIT_WATCHDOG_SECONDS = 15.0


def _arm_exit_watchdog(seconds: float = _EXIT_WATCHDOG_SECONDS) -> None:
    """End the process if it is still alive `seconds` from now.

    Armed once the event loop has returned, by which point everything the
    application had to save or stop has already happened: a process still
    alive after the grace is one that is stuck, not one that is finishing.
    """
    # Taken now, not at firing time: what ends the process is the exit that
    # was in force when quitting was decided.
    exit_now = os._exit

    def force_exit() -> None:
        time.sleep(seconds)
        exit_now(0)

    threading.Thread(target=force_exit, name="exit-watchdog", daemon=True).start()


def main() -> int:
    if "--startup-smoke" in sys.argv:
        # Importing this module has already loaded wxPython, every backend,
        # updater support, and platform accessibility dependencies. Verify the
        # packaged resources without opening a window so CI can test startup.
        required = ("send.wav", "in-progress.wav", "received.wav")
        earcons = Path(_resource_dir()) / "EarCons"
        if not all((earcons / name).is_file() for name in required):
            return 2
        if not APP_NAME or not version_tuple(APP_VERSION):
            return 3
        # AppKit is how anything is said to VoiceOver. A build that packaged
        # everything else and dropped it starts, runs, and is silent, which on
        # this application is the same as not working at all.
        if platform.system() == "Darwin" and not screen_speech._MAC_ANNOUNCE:
            return 4
        return 0
    chat_gui_startup_smoke = "--startup-chat-gui-smoke" in sys.argv
    gui_startup_smoke = "--startup-gui-smoke" in sys.argv or chat_gui_startup_smoke
    # First, so that anything below which goes wrong leaves a trace behind.
    # The packaged build is windowed and has no stderr to fall back on.
    diagnostics.start_logging()
    # Before anything is started: nothing BlindPilot launches may inherit a
    # PATH that points back into its own install folder, or the files there
    # stay open long after BlindPilot has closed and cannot be updated.
    keep_bundle_off_child_path()
    activate_managed_cli_paths()
    app = wx.App(False)
    # The application menu on macOS reads the display name; without this it
    # says "Python" when BlindPilot is run from source.
    app.SetAppName(APP_NAME)
    app.SetAppDisplayName(APP_NAME)
    # One copy per user. A second one shares the config, the history and
    # Codex's app server with the first and fights it over all three; starting
    # it again is almost always somebody looking for the window they lost.
    # Startup checks run beside a working copy and are exempt.
    instance = (
        None if gui_startup_smoke else wx.SingleInstanceChecker(f"{APP_NAME}-{wx.GetUserId()}")
    )
    # A blindpilot:// session link, when Windows started this copy for one.
    link = next(
        (arg for arg in sys.argv[1:] if arg.startswith(f"{remote_sessions.LINK_SCHEME}:")), ""
    )
    if instance is not None and instance.IsAnotherRunning():
        logging.getLogger("blindpilot").info("another BlindPilot is running; raising it")
        if link:
            hand_over_link(link)
        if not _raise_running_copy():
            wx.MessageBox("BlindPilot is already running.", APP_NAME, wx.OK | wx.ICON_INFORMATION)
        return 0

    cfg = _load_config()
    # Chosen here, before the first window, because wxWidgets cannot change
    # it later; that is why Preferences says the choice waits for a restart.
    # Dark mode on Windows is experimental in wxWidgets 3.3, so a refusal is
    # noted and the app carries on with whatever it gets.
    appearance = _valid_appearance(cfg.get("appearance"))
    set_appearance = getattr(app, "SetAppearance", None)
    if set_appearance is None:
        # wxPython before 4.3 has no appearance API. requirements.txt allows
        # 4.2, so the setting is simply not applied there.
        logging.getLogger("blindpilot").info(
            "appearance %s was not applied, this wxPython cannot set it", appearance
        )
    else:
        result = set_appearance(_appearance_for(appearance))
        if result != wx.App.AppearanceResult.Ok:
            logging.getLogger("blindpilot").info(
                "appearance %s was not applied, wx returned %s", appearance, result
            )
    # Installs that predate full-auto still carry the mode an older BlindPilot
    # saved for them. Moving them over here is what makes "nothing stops to
    # ask" true of an upgrade as well as of a fresh install.
    if adopt_full_auto_default(cfg):
        _save_config(cfg)
    if chat_gui_startup_smoke:
        cfg["app_mode"] = APP_MODE_CHAT
    # A packaged GUI smoke test runs with a clean temporary profile in CI. It
    # must exercise the real main window without waiting in the interactive
    # first-run wizard.
    if not cfg.get("setup_complete") and not gui_startup_smoke:
        wizard = SetupWizard(
            None,
            cfg.get("projects_folder"),
            normalize_backend(cfg.get("backend")),
        )
        result = wizard.ShowModal()
        if result == wx.ID_OK:
            if wizard.projects_folder:
                cfg["projects_folder"] = wizard.projects_folder
            cfg["backend"] = wizard.backend
        # Finishing or deliberately dismissing the optional Claude setup both
        # count as handled. Users choosing Codex or FreeBuff should not be sent
        # back through the Claude wizard on every launch.
        cfg["setup_complete"] = True
        _record_setup_complete(cfg)
        wizard.Destroy()
        # Even if cancelled, open the app — user may know what they're doing.

    global _STARTUP_CHECK
    _STARTUP_CHECK = gui_startup_smoke
    session_panel._STARTUP_CHECK = gui_startup_smoke
    main_frame._STARTUP_CHECK = gui_startup_smoke
    frame = MainFrame(initial_cwd=os.getcwd())
    if gui_startup_smoke:
        # Never shown. What this checks is that the window can be *built* -
        # every menu, control and binding made, and the sizers able to lay
        # them out - and none of that needs it on screen. Showing it put a
        # window in front of whoever was running the checks for a second and
        # a half, and for somebody who navigates by ear that is not a
        # harmless flicker.
        frame.Layout()
        wx.CallLater(1500, frame.Close)
    else:
        frame.Show()
        frame.watch_for_links()
        frame.watch_model_catalogs()
        if link:
            wx.CallAfter(frame.open_session_link, link)
        frame.Raise()
        _bring_to_front()
        # An update that failed did so with no window to report to, so its
        # reason is read out here, before anything else competes for attention.
        wx.CallLater(1200, frame.report_failed_update)
        if frame.automatic_update_check_enabled():
            wx.CallLater(5000, frame.check_for_updates_silently)
        # Abandoned downloads are tens of megabytes each.
        wx.CallLater(8000, sweep_temporary_files)
    app.MainLoop()
    # The main window, destroyed as the loop ended, is only queued for
    # deletion. Left queued, wxMSW tears the app down around it and prints
    # "UnregisterClass failed ... Class still has open windows". One more idle
    # pass frees it. Only this window: others belong to whoever called main().
    if any(window is frame for window in wx.GetTopLevelWindows()):
        frame.Destroy()
        wx.CallAfter(app.ExitMainLoop)
        app.MainLoop()
    return 0


if __name__ == "__main__":
    _result = main()
    # Armed here, at the entry point, and not in main(): main() returning is
    # the end of the application only when the application is the process.
    # Anything that calls main() and carries on — a test — keeps its own
    # lifetime, and no watchdog.
    _arm_exit_watchdog()
    sys.exit(_result)
