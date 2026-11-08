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
import subprocess
import sys
import threading
import time
import uuid  # noqa: F401
from pathlib import Path
from typing import Callable, Optional

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
    APPEARANCE_RESTART_NOTE,
    APPEARANCE_SYSTEM,  # noqa: F401 - read as blindpilot_app.APPEARANCE_SYSTEM
    APPEARANCES,  # noqa: F401 - read as blindpilot_app.APPEARANCES
    CUE_LOOP,
    CUE_OFF,
    CUE_PERIODIC,
    CUE_SECONDS_DEFAULT,  # noqa: F401 - read as blindpilot_app.CUE_SECONDS_DEFAULT
    CUE_SECONDS_MAX,
    CUE_SECONDS_MIN,
    NARRATION_EVERYTHING,  # noqa: F401 - read as blindpilot_app.NARRATION_EVERYTHING
    NARRATION_KEEP_UP,  # noqa: F401
    NARRATION_MODES,
    REMOTE_HERMES,
    SETTINGS,
    SOUND_CUES,
    _ALWAYS_SPOKEN,  # noqa: F401
    _RemoteHermes,  # noqa: F401 - read as blindpilot_app._RemoteHermes
    _Settings,  # noqa: F401 - compatibility for callers
    _load_config,
    _save_config,
    _valid_appearance,
    _valid_cue_seconds,
    _valid_model_refresh_minutes,  # noqa: F401 - read as blindpilot_app._valid_model_refresh_minutes
    _valid_progress_cue,
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

import backend_pool
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
from conversation_items import ConversationItem, ConversationItemsDialog
import remote_sessions
from app_updater import (
    ReleaseInfo,
    UpdateError,
    clear_pending_failure,
    fetch_latest_release,
    pending_failure,
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
    BACKEND_HERMES,
    BACKEND_IDS,
    BACKEND_LABELS,
    BACKEND_MUSEAI,
    BACKEND_OPENCODE,
    BACKENDS,
    backend_label,
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
    wsl_path_to_windows,
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
    HistoryEntry,
    claude_project_slug,  # noqa: F401
    list_history,
    load_turns,
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


# Before any setting is read: an install that predates the macOS conventions
# keeps its files in ~/.config and ~/.local/share, and they belong in
# ~/Library/Application Support. The move is on the import path because
# `SETTINGS` immediately below reads config.json to build itself.
migrate_macos_legacy_dirs()


APP_VERSION = "0.55.12"


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


class MainFrame(wx.Frame):
    def __init__(self, initial_cwd: str):
        super().__init__(None, title=APP_NAME)
        self.SetSize(self.FromDIP(wx.Size(900, 760)))
        # Below this the tab strip, prompt and button row start to overlap.
        self.SetMinSize(self.FromDIP(wx.Size(640, 480)))
        # The window icon, which the title bar, the taskbar when run from
        # source, Alt+Tab and every dialog take from the frame.
        icon_path = _app_icon_path()
        if icon_path.exists():
            self.SetIcons(wx.IconBundle(str(icon_path)))

        # Shared audio cues (send / in-progress loop / received).
        self.earcons = Earcons(
            os.path.join(_resource_dir(), "EarCons"),
            enabled=SETTINGS.sounds_enabled,
            cues=SETTINGS.sound_cues,
        )
        self._update_checking = False

        # Remembered "Projects folder" — the parent folder that holds the
        # user's project directories. New Session browses from there.
        cfg = _load_config()
        self._backend = normalize_backend(cfg.get("backend"))
        self._app_mode = APP_MODE_CHAT if cfg.get("app_mode") == APP_MODE_CHAT else APP_MODE_AGENT
        self._chat_model_order = _valid_chat_model_order(cfg.get("chat_model_order"))
        self.chat_panel = None
        pf = cfg.get("projects_folder")
        self._projects_folder: Optional[str] = pf if pf and os.path.isdir(pf) else None

        # ----- Menu bar (gives us standard Cmd+T / Cmd+W on Mac) -----
        # Items that act on the visible session tab. Greyed out in Chat mode,
        # where the notebook is hidden and they would work on a page nobody
        # sees. Compact, Connect and Hermes Conversations have refreshers of
        # their own that already know the mode.
        self._agent_menu_items: list[wx.MenuItem] = []
        # The two sessions in an agent conversation, while one runs.
        self._relay: Optional[dict] = None
        self._find_all_serial = 0
        self._find_all_progress: Optional[wx.ProgressDialog] = None
        self._find_all_cancel: Optional[threading.Event] = None
        self._history_open_serial = 0
        self._history_open_cancel: Optional[threading.Event] = None
        self._history_open_progress: Optional[wx.Dialog] = None
        self._history_open_message: Optional[wx.StaticText] = None
        self._history_open_cancel_button: Optional[wx.Button] = None
        self._agent_files_cache: dict[str, list[AgentFiles]] = {}
        self._agent_files_serial = 0
        # Session links handed over by a second copy; watched only once the
        # window is shown (`watch_for_links`), never in a window built and torn
        # down by a check, where a timer firing mid-teardown crashed macOS.
        self._links_timer: Optional[wx.Timer] = None
        # Re-reads every tab's model catalog on a timer, so a model retired
        # upstream drops out of the picker on a window left open. Watched only
        # once the window is shown, like the links timer above.
        self._model_timer: Optional[wx.Timer] = None
        self._desktop_shortcut_serial = 0
        self._desktop_shortcut_running = False
        menubar = wx.MenuBar()
        menubar.Append(self._build_file_menu(), "&File")
        menubar.Append(self._build_conversation_menu(), "&Conversation")

        menubar.Append(self._build_model_menu(), "&Model")

        # ----- Options: how much of a run is narrated -----
        options_menu = wx.Menu()
        self._rows_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "Show &live activity in the list",
            "Add rows for your message, thinking, tool steps and results while a run is working",
        )
        self._speak_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "&Speak activity aloud",
            "Read each activity row out as it arrives",
        )
        self._thinking_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "Include the backend's &reasoning",
            "Add the backend's own thinking to the activity. Off by default, "
            "so only its actions and its answer are shown",
        )
        self._sounds_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "Play &sound cues",
            "Play sounds when a message is sent, while it is working, when a response "
            "arrives, and when a turn fails",
        )
        options_menu.AppendSubMenu(
            self._build_narration_menu(),
            "&Narration",
            "Choose how much of a run is read out as it happens",
        )
        options_menu.AppendSubMenu(
            self._build_sound_cue_menu(),
            "So&unds",
            "Choose which of the four sounds are played",
        )
        options_menu.AppendSeparator()
        self._text_view_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "Responses as a read-o&nly text field",
            "Show the responses as a read-only edit field, one row per line, "
            "so NVDA can review and select across them",
        )
        options_menu.AppendSeparator()
        silent_response_item = options_menu.Append(
            wx.ID_ANY,
            "&Silent until the response mode",
            "Turn both off: nothing appears or is spoken until the whole response is ready",
        )
        options_menu.AppendSeparator()
        # Radio items rather than a checkbox: three states, and a screen reader
        # announces which one is selected when moving through them.
        self._cue_loop_item = options_menu.AppendRadioItem(
            wx.ID_ANY,
            "Working sound: &continuous",
            "Repeat the working sound for the whole turn, the way earlier versions did",
        )
        self._cue_periodic_item = options_menu.AppendRadioItem(
            wx.ID_ANY,
            "Working sound: e&very few seconds",
            "Play the working sound occasionally, so a long turn still says it is alive",
        )
        self._cue_off_item = options_menu.AppendRadioItem(
            wx.ID_ANY,
            "Working sound: o&ff",
            "No working sound; the send and received sounds still play",
        )
        cue_interval_item = options_menu.Append(
            wx.ID_ANY,
            "Working sound &interval...",
            "How many seconds between working sounds in the every-few-seconds mode",
        )
        options_menu.AppendSeparator()
        remote_hermes_item = options_menu.Append(
            wx.ID_ANY,
            "Re&mote Hermes...",
            "Drive a Hermes running on another computer instead of the one installed here",
        )
        options_menu.AppendSeparator()
        preferences_item = options_menu.Append(
            wx.ID_PREFERENCES,
            "&Preferences…\tCtrl+,",
            "Open every Options-menu setting in one dialog",
        )
        self._rows_item.Check(SETTINGS.live_rows)
        self._speak_item.Check(SETTINGS.speak_live)
        self._thinking_item.Check(SETTINGS.show_thinking)
        self._sounds_item.Check(SETTINGS.sounds_enabled)
        self._text_view_item.Check(SETTINGS.text_view)
        {
            CUE_LOOP: self._cue_loop_item,
            CUE_PERIODIC: self._cue_periodic_item,
            CUE_OFF: self._cue_off_item,
        }[SETTINGS.progress_cue].Check(True)
        menubar.Append(options_menu, "&Options")

        chat_menu = wx.Menu()
        self._chat_accounts_item = chat_menu.Append(
            wx.ID_ANY,
            "&Accounts...",
            "Add, edit, test, or remove Chat provider accounts",
        )
        self._chat_profiles_item = chat_menu.Append(
            wx.ID_ANY,
            "Conversation &profiles...",
            "Manage Chat system prompts and generation defaults",
        )
        # R&ecent rather than &Recent: Refresh models below already has R.
        self._chat_conversations_item = chat_menu.Append(
            wx.ID_ANY,
            "R&ecent conversations...",
            "Open a past Chat conversation and carry on with it",
        )
        chat_menu.AppendSeparator()
        self._chat_refresh_item = chat_menu.Append(
            wx.ID_ANY,
            "&Refresh models",
            "Refresh the model list for the selected Chat account",
        )
        model_order_menu = wx.Menu()
        self._chat_model_order_items: dict[str, wx.MenuItem] = {}
        for order, label, description in CHAT_MODEL_ORDERS:
            item = model_order_menu.AppendRadioItem(wx.ID_ANY, label, description)
            item.Check(order == self._chat_model_order)
            self._chat_model_order_items[order] = item

            def set_order(_event: wx.CommandEvent, selected: str = order) -> None:
                self._set_chat_model_order(selected)

            self.Bind(wx.EVT_MENU, _gui_work("Chat model order", set_order), item)
        chat_menu.AppendSubMenu(
            model_order_menu,
            "Model &order",
            "Choose how the Chat model list is sorted",
        )
        chat_history_menu = wx.Menu()
        self._chat_history_list_item = chat_history_menu.AppendRadioItem(wx.ID_ANY, "&List")
        self._chat_history_text_item = chat_history_menu.AppendRadioItem(
            wx.ID_ANY, "&Read-only text"
        )
        self._chat_history_list_item.Check(True)
        chat_menu.AppendSubMenu(
            chat_history_menu,
            "&History view",
            "Choose how Chat conversation history is presented",
        )
        chat_menu.AppendSeparator()
        self._chat_diagnostics_item = chat_menu.Append(
            wx.ID_ANY,
            "&Diagnostics...",
            "Review the Chat provider diagnostic log",
        )
        self._chat_menu_items = [
            self._chat_accounts_item,
            self._chat_profiles_item,
            self._chat_conversations_item,
            self._chat_refresh_item,
            *self._chat_model_order_items.values(),
            self._chat_history_list_item,
            self._chat_history_text_item,
            self._chat_diagnostics_item,
        ]
        for item in self._chat_menu_items:
            item.Enable(False)
        # Cha&t rather than &Chat: the Conversation menu already has Alt+C, and
        # a second menu claiming it is a menu with no access key at all --
        # Windows opens the first and pressing it again does not move on. T is
        # free once Stop generation gives it up, which it does in chat_panel.
        menubar.Append(chat_menu, "Cha&t")

        help_menu = wx.Menu()
        update_item = help_menu.Append(
            wx.ID_ANY,
            "Check for &Updates...",
            "Check GitHub for a newer BlindPilot release",
        )
        self._automatic_updates_item = help_menu.AppendCheckItem(
            wx.ID_ANY,
            "Check for updates at &startup",
            "Quietly check at startup and report only when a new version is available",
        )
        self._automatic_updates_item.Check(bool(cfg.get("check_for_updates_at_startup", True)))
        help_menu.AppendSeparator()
        self._menu_item(
            help_menu,
            "&Keyboard Shortcuts\tF1",
            "List every keyboard shortcut, grouped by where it works",
            self._show_keyboard_shortcuts,
        )
        help_menu.AppendSeparator()
        logs_item = help_menu.Append(
            wx.ID_ANY,
            "Open &Log Folder",
            "Show the folder BlindPilot writes its diagnostics to",
        )
        self._menu_item(
            help_menu,
            "&Report a Bug…",
            "Describe a problem and file it on GitHub, with the versions BlindPilot runs on",
            self._report_bug,
        )
        help_menu.AppendSeparator()
        about_item = help_menu.Append(
            wx.ID_ABOUT,
            "&About BlindPilot",
            "BlindPilot version, license, and original application credit",
        )
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)
        self._refresh_compact_item()
        self._refresh_connect_item()
        self._refresh_hermes_sessions_item()
        self._refresh_mode_items()
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Show live rows", lambda _e: self._toggle_live_rows()),
            self._rows_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Speak live rows", lambda _e: self._toggle_speak_live()),
            self._speak_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Show thinking", lambda _e: self._toggle_show_thinking()),
            self._thinking_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Toggle sounds", lambda _e: self._toggle_sounds()),
            self._sounds_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Toggle text view", lambda _e: self._toggle_text_view()),
            self._text_view_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Progress cue: loop", lambda _e: self._choose_progress_cue(CUE_LOOP)),
            self._cue_loop_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work(
                "Progress cue: periodic",
                lambda _e: self._choose_progress_cue(CUE_PERIODIC),
            ),
            self._cue_periodic_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Progress cue: off", lambda _e: self._choose_progress_cue(CUE_OFF)),
            self._cue_off_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Configure cue interval", lambda _e: self._configure_cue_interval()),
            cue_interval_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Configure remote Hermes", lambda _e: self._configure_remote_hermes()),
            remote_hermes_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work(
                "Use silent until response",
                lambda _e: self._use_silent_until_response_mode(),
            ),
            silent_response_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Show Chat accounts", lambda _e: self._show_chat_accounts()),
            self._chat_accounts_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Show Chat profiles", lambda _e: self._show_chat_profiles()),
            self._chat_profiles_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Show Chat conversations", lambda _e: self._show_chat_conversations()),
            self._chat_conversations_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Refresh Chat models", lambda _e: self._refresh_chat_models()),
            self._chat_refresh_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Chat history list view", lambda _e: self._set_chat_history_view("list")),
            self._chat_history_list_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Chat history text view", lambda _e: self._set_chat_history_view("text")),
            self._chat_history_text_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Show Chat diagnostics", lambda _e: self._show_chat_diagnostics()),
            self._chat_diagnostics_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Show preferences", lambda _e: self._show_preferences()),
            preferences_item,
        )
        self.Bind(wx.EVT_MENU, _gui_work("Show about", lambda _e: self._show_about()), about_item)
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Open log folder", lambda _e: self._open_log_folder()),
            logs_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Check for updates", lambda _e: self._show_update_dialog()),
            update_item,
        )
        self.Bind(
            wx.EVT_MENU,
            _gui_work("Toggle automatic updates", lambda _e: self._toggle_automatic_updates()),
            self._automatic_updates_item,
        )

        # ----- Top-level layout: mode picker + active experience -----
        root = wx.Panel(self)
        root_sizer = wx.BoxSizer(wx.VERTICAL)
        self._root = root
        self._root_sizer = root_sizer

        picker_row = wx.BoxSizer(wx.HORIZONTAL)
        mode_label = wx.StaticText(root, label="Mode:")
        self.mode_combo = wx.ComboBox(
            root,
            choices=[APP_MODE_LABELS[APP_MODE_AGENT], APP_MODE_LABELS[APP_MODE_CHAT]],
            style=wx.CB_READONLY,
        )
        self.mode_combo.SetName("Mode")
        self.mode_combo.SetSelection(1 if self._app_mode == APP_MODE_CHAT else 0)
        self.mode_combo.SetToolTip("Choose coding-agent sessions or provider chat")
        self.mode_combo.Bind(wx.EVT_COMBOBOX, self._on_app_mode_changed)
        self.mode_combo.Bind(wx.EVT_KEY_DOWN, self._on_mode_combo_key)
        picker_row.Add(mode_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(PAD))
        picker_row.Add(self.mode_combo, 0, wx.ALIGN_CENTER_VERTICAL)

        # This is an intentional, keyboard-focusable native tab strip. Its
        # pages are empty because the real session content lives in the
        # Simplebook below; separating the two prevents Windows from announcing
        # "tab control" merely because focus entered a conversation page.
        self.tab_switcher = wx.Notebook(root, style=wx.NB_TOP)
        self.tab_switcher.SetName("Session tabs")
        # A first guess at the strip's height; _fit_tab_strip measures the
        # real one once a tab exists.
        self.tab_switcher.SetMinSize(wx.Size(-1, root.FromDIP(38)))
        self.tab_switcher.Bind(wx.EVT_BOOKCTRL_PAGE_CHANGED, self._on_tab_switcher_changed)
        self._syncing_tab_switcher = False
        self._strip_keeps_focus = False

        # Session and Ctrl+Tab provide all session navigation. Simplebook has
        # the same page-management API without a native tab strip. A native
        # Notebook announces "tab control" whenever focus enters or leaves one
        # of its pages, even when the strip itself rejects keyboard focus.
        self.notebook = wx.Simplebook(root)
        self.notebook.SetName("Session pages")
        self.notebook.Bind(wx.EVT_BOOKCTRL_PAGE_CHANGED, self._on_tab_changed)

        # A backend left idle for a quarter of an hour is let go, so an
        # abandoned tab is not holding an app-server and its MCP children all
        # afternoon. The reaper is what notices; _announce_reap is what makes
        # it audible, because the next prompt in that tab then pays a cold
        # start, and an unexplained pause is how a hang sounds. The same goes
        # for a process found dead when the next prompt reaches for it, which
        # is the more surprising of the two: nothing warned in advance.
        # CallAfter because the pool speaks from whichever thread noticed --
        # the sweep's, or a turn's -- and narration belongs to the window's.
        backends = backend_pool.pool()
        backends.on_reap = lambda name, why: wx.CallAfter(self._announce_reap, name, why)
        backend_pool.start_reaper()

        pad = self.FromDIP(PAD)
        root_sizer.Add(picker_row, 0, wx.EXPAND | wx.ALL, pad)
        root_sizer.Add(self.tab_switcher, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        root_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, pad)
        root.SetSizer(root_sizer)

        self.statusbar = self.CreateStatusBar()
        self._set_status_text("Ready")

        # Shortcuts. Cmd+L focuses the active tab's prompt, Cmd+Shift+M cycles
        # its permission mode, and Cmd+1..9 jump straight to tab N; none of
        # those has a menu item. Ctrl+Tab and Ctrl+Shift+Tab move between
        # tabs, which is what every other tabbed application does; Cmd+Shift+]
        # and Cmd+Shift+[ do the same and are what a Mac user reaches for.
        # Those four, Cmd+Shift+A and Cmd+R do have menu items, which show the
        # chord in the accelerator column and register it too. They stay in
        # this table as well because the items are greyed out in Chat mode
        # while the chords keep working there (a disabled item's accelerator
        # never fires). A key is translated once, by whichever table sees it
        # first, so nothing fires twice. Slash Command is the menu's alone:
        # its handler does nothing in Chat mode either way.
        id_focus_prompt = wx.NewIdRef()
        id_next_tab = wx.NewIdRef()
        id_prev_tab = wx.NewIdRef()
        id_cycle_mode = wx.NewIdRef()
        id_attach = wx.NewIdRef()
        id_jump_response = wx.NewIdRef()
        id_next_pane = wx.NewIdRef()
        id_prev_pane = wx.NewIdRef()

        self._bind_shortcut(id_next_pane, "F6, next pane", lambda: self._cycle_pane(+1))
        self._bind_shortcut(id_prev_pane, "Shift+F6, previous pane", lambda: self._cycle_pane(-1))
        self._bind_shortcut(
            id_focus_prompt, "Ctrl+L, focus the prompt", lambda: self._focus_active("prompt")
        )
        self._bind_shortcut(id_next_tab, "Ctrl+Tab, next tab", lambda: self._cycle_tab(+1))
        self._bind_shortcut(
            id_prev_tab, "Ctrl+Shift+Tab, previous tab", lambda: self._cycle_tab(-1)
        )
        self._bind_shortcut(
            id_cycle_mode, "Ctrl+Shift+M, cycle mode", lambda: self._cycle_mode_active()
        )
        self._bind_shortcut(id_attach, "Ctrl+Shift+A, attach files", lambda: self._attach_active())
        self._bind_shortcut(
            id_jump_response, "Ctrl+R, latest response", lambda: self._jump_to_latest_response()
        )

        accel_entries = [
            wx.AcceleratorEntry(wx.ACCEL_CMD, ord("L"), id_focus_prompt),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, wx.WXK_TAB, id_next_tab),
            wx.AcceleratorEntry(wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_TAB, id_prev_tab),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("]"), id_next_tab),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("["), id_prev_tab),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("M"), id_cycle_mode),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("A"), id_attach),
            wx.AcceleratorEntry(wx.ACCEL_CMD, ord("R"), id_jump_response),
            wx.AcceleratorEntry(wx.ACCEL_NORMAL, wx.WXK_F6, id_next_pane),
            wx.AcceleratorEntry(wx.ACCEL_SHIFT, wx.WXK_F6, id_prev_pane),
        ]
        self._tab_jump_ids: list[wx.WindowIDRef] = []
        for n in range(1, 10):
            tid = wx.NewIdRef()
            self._tab_jump_ids.append(tid)
            self._bind_shortcut(
                tid, f"Ctrl+{n}, jump to tab {n}", lambda n=n: self._jump_to_tab(n - 1)
            )
            accel_entries.append(wx.AcceleratorEntry(wx.ACCEL_CMD, ord(str(n)), tid))

        self.SetAcceleratorTable(wx.AcceleratorTable(accel_entries))
        # EVT_KEY_DOWN is too late for Tab on native Windows Choice controls:
        # wxWidgets has already performed dialog navigation. A frame-level
        # character hook sees it first and routes only the page boundaries.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_agent_char_hook)

        self._add_session(initial_cwd)
        self._fit_tab_strip()
        self._set_app_mode(self._app_mode, announce_change=False)
        # Setting focus before the frame is on screen does not stick: Windows
        # has no visible window to give it to. Asking again after everything
        # queued during construction has run is what makes the mode that was
        # restored the mode the first keystroke lands in.
        if not _STARTUP_CHECK:
            wx.CallAfter(self.focus_for_mode)

        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _fit_tab_strip(self) -> None:
        """Shrink the tab strip to its tab row, so no empty page shows under it.

        A native notebook always draws a frame around its page, and with
        nothing on the pages that frame was a bordered band under the tabs.
        The page's offset inside the control is the height of the tab row,
        and what lies below the page is the frame; both are measured rather
        than guessed, so the height follows the font and the DPI.
        """
        if self.tab_switcher.GetPageCount() == 0:
            return
        # One layout so the page has its real size to measure against.
        self.SendSizeEvent()
        page = self.tab_switcher.GetPage(0)
        below = (
            self.tab_switcher.GetClientSize().height - page.GetPosition().y - page.GetSize().height
        )
        self.tab_switcher.SetMinSize(wx.Size(-1, page.GetPosition().y + max(below, 0)))
        self._root.Layout()

    def _announce_reap(self, backend: str, reason: str = backend_pool.REAP_IDLE) -> None:
        """Say that a backend was let go, and that the next turn restarts it.

        Two things happen to a held process and they are not the same event to
        somebody listening. One was let go by a rule, before anything was
        asked of it. The other was found already gone -- crashed, run out of
        memory, or killed while the laptop slept -- by the prompt that is
        waiting on it right now, with no warning at all.

        Goes through the visible page's own `_say` rather than adding a
        narration path of its own: that method already decides that only the
        visible tab speaks, and mirrors the line to the status bar either way,
        so nothing is lost when it declines to speak.

        The kind is "notice" -- BlindPilot speaking for itself, which is what
        `_ALWAYS_SPOKEN` exists for. Any other kind is dropped in keep-up
        narration, and keep-up is the mode where an unexplained pause on the
        next message is least likely to be waited out and most likely to be
        read as a hang.
        """
        page = self.notebook.GetCurrentPage()
        say = getattr(page, "_say", None)
        if say is None:
            # Mid-teardown, or a page that is not a session. Nothing to say to.
            return
        label = backend_label(backend)
        if reason == backend_pool.REAP_DIED:
            say(f"{label} had stopped running. Restarting it, which takes a moment.", "notice")
            return
        say(f"{label} was idle and has been closed. The next message will restart it.", "notice")

    # ----- Tab management -----
    def _on_app_mode_changed(self, event: wx.CommandEvent) -> None:
        mode = APP_MODE_CHAT if self.mode_combo.GetSelection() == 1 else APP_MODE_AGENT
        self._set_app_mode(mode)
        event.Skip()

    @staticmethod
    def _focus_is_within(focus: Optional[wx.Window], control: wx.Window) -> bool:
        """Include native child windows used internally by combo controls."""
        current = focus
        while current is not None:
            if current is control:
                return True
            current = current.GetParent()
        return False

    @staticmethod
    def _moved_focus(focus: Optional[wx.Window], move: Callable[[], None]) -> bool:
        """Perform a boundary move, and report whether focus really left.

        Routing that claims a Tab it did not act on is a keyboard trap: the
        key is swallowed, native traversal never runs, and the control the
        user is on is the control they stay on however many times they press
        it. Answering honestly hands the key back to wxWidgets instead.
        """
        move()
        return wx.Window.FindFocus() is not focus

    def _route_agent_tab(self, focus: Optional[wx.Window], shift: bool) -> bool:
        """Route focus across Agent-page boundaries before native traversal."""
        if self._app_mode != APP_MODE_AGENT:
            return False
        page = self.notebook.GetCurrentPage()
        if isinstance(page, ConversationWebViewPanel):
            if self._focus_is_within(focus, self.tab_switcher) and not shift:
                return self._moved_focus(focus, lambda: page.focus_content(from_navigation=True))
            if self._focus_is_within(focus, self.mode_combo) and shift:
                return self._moved_focus(focus, lambda: page.focus_content(from_navigation=True))
            return False
        if not isinstance(page, SessionPanel):
            return False

        if self._focus_is_within(focus, self.mode_combo):
            if shift:
                return self._moved_focus(focus, page.focus_last_control)
            return self._moved_focus(focus, self.tab_switcher.SetFocus)

        if self._focus_is_within(focus, self.tab_switcher):
            if shift:
                return self._moved_focus(focus, self.mode_combo.SetFocus)
            return self._moved_focus(focus, page.focus_first_control)

        if self._focus_is_within(focus, page.mode_picker) and not shift:
            return self._moved_focus(focus, self.mode_combo.SetFocus)

        if self._focus_is_within(focus, page.subagents):
            if shift:
                return self._moved_focus(focus, self.tab_switcher.SetFocus)
            return self._moved_focus(focus, lambda: page.focus_first_control(past_subagents=True))

        responses = page._responses_ctrl()
        if shift and self._focus_is_within(focus, responses):
            return self._moved_focus(focus, page.focus_above_responses)

        if shift and page._row_count() == 0 and self._focus_is_within(focus, page.prompt):
            return self._moved_focus(focus, page.focus_above_responses)
        if not shift and self._focus_is_within(focus, page.prompt):
            # NVDA schedules a formatting query 50 ms after receiving Tab in
            # an edit field. Keep the Prompt alive and focused until that query
            # finishes instead of leaving it with a stale native text range.
            page.focus_first_action_delayed()
            return True
        return False

    def _on_agent_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() != wx.WXK_TAB:
            event.Skip()
            return
        if event.ControlDown() or event.CmdDown():
            # Ctrl+Tab is session navigation from anywhere in the window,
            # including from inside the tab strip, whose own native Ctrl+Tab
            # would otherwise move the strip without moving the page. Handling
            # it here rather than leaving it to the accelerator table keeps the
            # hook from swallowing it as a plain Tab.
            if self._app_mode == APP_MODE_AGENT:
                self._cycle_tab(-1 if event.ShiftDown() else +1)
                return
            event.Skip()
            return
        if self._route_agent_tab(wx.Window.FindFocus(), event.ShiftDown()):
            return
        event.Skip()

    def _on_mode_combo_key(self, event: wx.KeyEvent) -> None:
        """Move backward into the end of the active Agent page."""
        if (
            self._app_mode == APP_MODE_AGENT
            and event.GetKeyCode() == wx.WXK_TAB
            and event.ShiftDown()
        ):
            page = self.notebook.GetCurrentPage()
            if isinstance(page, SessionPanel):
                page.focus_last_control()
                return
        event.Skip()

    def _ensure_chat_panel(self):
        if self.chat_panel is not None:
            return self.chat_panel
        from chat_integration import create_chat_panel

        self.chat_panel = create_chat_panel(
            self._root,
            self._set_status_text,
            announce,
            model_order=self._chat_model_order,
        )
        self.chat_panel.refresh_models_item = self._chat_refresh_item
        self.chat_panel.history_list_view_item = self._chat_history_list_item
        self.chat_panel.history_text_view_item = self._chat_history_text_item
        self._root_sizer.Add(self.chat_panel, 1, wx.EXPAND | wx.ALL, self.FromDIP(PAD))
        self.chat_panel.Hide()
        return self.chat_panel

    def _set_app_mode(self, mode: str, announce_change: bool = True) -> None:
        mode = APP_MODE_CHAT if mode == APP_MODE_CHAT else APP_MODE_AGENT
        chat_panel = self.chat_panel
        if mode == APP_MODE_CHAT:
            try:
                chat_panel = self._ensure_chat_panel()
            except Exception as exc:
                # Carry on as Agent mode, all the way through, so the saved
                # mode, the menus and the focus agree with what is shown.
                mode = APP_MODE_AGENT
                message = f"Chat mode could not be opened: {exc}"
                self._set_status_text(message)
                wx.MessageBox(message, "Chat Mode", wx.OK | wx.ICON_ERROR, self)

        if mode != self._app_mode:
            self._agent_files_serial += 1
        self._app_mode = mode
        show_agent = mode == APP_MODE_AGENT
        self.tab_switcher.Show(show_agent)
        self.notebook.Show(show_agent)
        if chat_panel is not None:
            chat_panel.Show(not show_agent)
        for item in self._chat_menu_items:
            item.Enable(not show_agent)
        for item in self._agent_menu_items:
            # The muse.ai submenu and Permission Mode take turns in the Model
            # menu, so one is always out of it, and wxGTK asserts on enabling
            # an item that is not in its menu. GetMenu() is no test: the muse.ai
            # item names its menu before it is ever appended.
            # _refresh_museai_menu sets the state when it goes back in.
            menu = item.GetMenu()
            if menu is not None and item in menu.GetMenuItems():
                item.Enable(show_agent)
        self.mode_combo.SetSelection(0 if show_agent else 1)
        self._refresh_compact_item()
        self._refresh_connect_item()
        # Chat mode has no backend conversation to reopen, so the Hermes list
        # goes with it rather than sitting in File doing nothing.
        self._refresh_hermes_sessions_item()
        self._root.Layout()
        self._refresh_museai_menu()

        cfg = _load_config()
        cfg["app_mode"] = mode
        saved = _save_config(cfg)
        # A startup check shows no window, so there is nothing here to focus
        # into, and asking for focus would take it from whoever is running it.
        self.focus_for_mode()
        if announce_change:
            self._announce_setting(f"{APP_MODE_LABELS[mode]} mode" if saved else _SAVE_FAILED)

    def focus_for_mode(self) -> None:
        """Put focus where the mode that is showing actually starts.

        One place decides it, so the answer cannot depend on the order two
        deferred calls happen to run in.
        """
        if _STARTUP_CHECK or not self:
            return
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.message_input.SetFocus()
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.focus_prompt()
        elif isinstance(page, ConversationWebViewPanel):
            page.focus_content()

    def _refresh_chat_models(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_refresh_models(wx.CommandEvent())

    def _set_chat_model_order(self, order: str) -> None:
        """Persist and apply the ordering chosen in Chat, without losing its pick."""
        self._chat_model_order = _valid_chat_model_order(order)
        for value, item in self._chat_model_order_items.items():
            item.Check(value == self._chat_model_order)
        if self.chat_panel is not None:
            self.chat_panel.set_model_order(self._chat_model_order)
        cfg = _load_config()
        cfg["chat_model_order"] = self._chat_model_order
        saved = _save_config(cfg)
        label = next(
            label.replace("&", "")
            for value, label, _description in CHAT_MODEL_ORDERS
            if value == self._chat_model_order
        )
        self._announce_setting(f"Chat model order: {label}" if saved else _SAVE_FAILED)

    def _show_chat_accounts(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_accounts(wx.CommandEvent())

    def _show_chat_profiles(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_profiles(wx.CommandEvent())

    def _show_chat_conversations(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_conversations(wx.CommandEvent())

    def _set_chat_history_view(self, view: str) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel._set_history_view(view)

    def _show_chat_diagnostics(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_diagnostics(wx.CommandEvent())

    def current_backend(self) -> str:
        return self._backend

    def _set_backend(self, backend: str) -> None:
        backend = normalize_backend(backend)
        if backend == self._backend:
            return
        page = self.notebook.GetCurrentPage()
        if backend == BACKEND_MUSEAI and isinstance(page, SessionPanel) and page._run_in_progress():
            self._announce_setting("Stop the running task before opening muse.ai's main chat")
            return
        self._backend = backend
        cfg = _load_config()
        cfg["backend"] = backend
        saved = _save_config(cfg)
        # The visible tab only. Every tab used to follow this one setting, so
        # picking a backend for a new tab quietly moved the others to it too
        # and their next message went somewhere the person had not chosen.
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.set_backend(backend)
        self._refresh_backend_items()
        self._relabel_tabs()
        message = (
            f"Backend changed to {backend_label(backend)} for this tab. "
            "It will be used for the next new turn."
        )
        self._announce_setting(message if saved else _SAVE_FAILED)

    def _refresh_backend_items(self) -> None:
        """Point the menu, and what depends on the backend, at ``_backend``."""
        for key, item in self._backend_items.items():
            item.Check(key == self._backend)
        self._refresh_compact_item()
        self._refresh_connect_item()
        self._refresh_hermes_sessions_item()
        self._refresh_museai_menu()

    def _follow_tab_backend(self) -> None:
        """Make the visible tab's backend the one the menu shows.

        It is also what the next new tab starts on, which is the backend the
        person was looking at when they asked for one.
        """
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel) and page.selected_backend() != self._backend:
            self._backend = page.selected_backend()
            self._refresh_backend_items()
        self._relabel_tabs()

    def _backends_mixed(self) -> bool:
        """Whether the open tabs do not all use the same backend."""
        return len({page.selected_backend() for page in self._session_panels()}) > 1

    def _relabel_tabs(self) -> None:
        """Name each tab, with its backend first while tabs use more than one.

        With a single backend the tag would be the same word on every tab, so
        it is left off; the moment a second backend is open, every tab, its
        prompt and its send cue say which one they belong to.
        """
        mixed = self._backends_mixed()
        self.earcons.per_backend_send = mixed
        for index in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(index)
            if not isinstance(page, SessionPanel):
                continue
            label = _tab_label(getattr(page, "tab_title", ""), page.cwd)
            if mixed:
                label = f"{backend_label(page.selected_backend())}: {label}"
            page.show_backend_in_prompt(mixed)
            if self.notebook.GetPageText(index) != label:
                self.notebook.SetPageText(index, label)
        self._sync_tab_switcher()

    def _manage_backends(self) -> None:
        """Open the accessible setup flow for the current provider."""
        dlg = SetupWizard(
            self,
            initial_projects_folder=self._projects_folder,
            initial_backend=self._backend,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            chosen = dlg.backend
            projects_folder = dlg.projects_folder
        finally:
            dlg.Destroy()
        if projects_folder:
            self._projects_folder = projects_folder
            cfg = _load_config()
            cfg["projects_folder"] = projects_folder
            folder_saved = _save_config(cfg)
        else:
            folder_saved = True
        self._set_backend(chosen)
        if not folder_saved:
            # Said after the backend line: the two announcements queue, and
            # ending on the success claim would bury the failure.
            self._announce_setting(_SAVE_FAILED)

    def _show_preferences(self) -> None:
        """Open the Preferences dialog (Cmd+, on macOS).

        wxWidgets relocates a wx.ID_PREFERENCES item into the macOS application
        menu automatically; the dialog itself is the same settings the Options
        menu carries, applied through the same store.
        """
        dialog = PreferencesDialog(self)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            self._apply_preferences(dialog)
        finally:
            dialog.Destroy()

    def _apply_preferences(self, dialog: PreferencesDialog) -> None:
        """Put the dialog's choices into the settings store and the menus.

        The menu items keep their checkmarks in step, because the menu and the
        dialog describe one set of settings, and a menu that lied about what
        was set would undo a screen reader user's trust in what they hear.
        """
        SETTINGS.narration = dialog.narration_selection()
        SETTINGS.live_rows = dialog.live_rows
        SETTINGS.speak_live = dialog.speak_live
        SETTINGS.show_thinking = dialog.show_thinking
        SETTINGS.sounds_enabled = dialog.sounds_enabled
        SETTINGS.sound_cues = dict(dialog.sound_cues)
        SETTINGS.text_view = dialog.text_view
        SETTINGS.ask_written_questions = dialog.ask_written_questions
        SETTINGS.notify_in_background = dialog.notify_in_background
        SETTINGS.read_back_sent = dialog.read_back_sent
        SETTINGS.progress_cue = dialog.progress_cue
        SETTINGS.progress_cue_seconds = dialog.progress_interval
        SETTINGS.model_refresh_minutes = dialog.model_refresh_minutes
        changed_appearance = dialog.appearance != SETTINGS.appearance
        SETTINGS.appearance = dialog.appearance
        changed_text_view = SETTINGS.text_view != self._text_view_item.IsChecked()
        saved = SETTINGS.save()

        self._rows_item.Check(SETTINGS.live_rows)
        self._speak_item.Check(SETTINGS.speak_live)
        self._thinking_item.Check(SETTINGS.show_thinking)
        self._sounds_item.Check(SETTINGS.sounds_enabled)
        self._text_view_item.Check(SETTINGS.text_view)
        for mode, item in self._narration_items.items():
            item.Check(mode == SETTINGS.narration)
        for cue, item in self._sound_cue_items.items():
            item.Check(SETTINGS.sound_cues.get(cue, True))
            item.Enable(SETTINGS.sounds_enabled)
        self._cue_loop_item.Check(SETTINGS.progress_cue == CUE_LOOP)
        self._cue_periodic_item.Check(SETTINGS.progress_cue == CUE_PERIODIC)
        self._cue_off_item.Check(SETTINGS.progress_cue == CUE_OFF)

        self.earcons.set_enabled(SETTINGS.sounds_enabled)
        self.earcons.set_cues(SETTINGS.sound_cues)
        if SETTINGS.progress_cue != CUE_OFF and self._turn_in_flight():
            self.earcons.start_progress()
        else:
            self.earcons.stop_progress()
        if changed_text_view:
            for page in self._session_panels():
                page.apply_view_mode()
        # The catalog timer follows the new interval at once, so a change from
        # 15 minutes to 0, or the other way, is not left until the next start.
        self._sync_model_timer()

        cfg = _load_config()
        cfg["check_for_updates_at_startup"] = dialog.check_updates_startup
        saved = _save_config(cfg) and saved
        self._automatic_updates_item.Check(dialog.check_updates_startup)
        self._announce_setting("Preferences applied" if saved else _SAVE_FAILED)
        if changed_appearance:
            # Nothing on screen changes yet, so without this the new choice
            # would seem to have done nothing.
            announce(APPEARANCE_RESTART_NOTE)

    def _report_bug(self) -> None:
        page = self.notebook.GetCurrentPage()
        backend = page.selected_backend() if isinstance(page, SessionPanel) else self._backend
        with BugReportDialog(self, bug_report_facts(backend, self._app_mode)) as dlg:
            dlg.ShowModal()

    def _show_keyboard_shortcuts(self) -> None:
        dlg = ReadView(self, KEYBOARD_SHORTCUTS, "Keyboard Shortcuts")
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _show_about(self) -> None:
        description = about_description()
        # The native About panel on macOS carries the app icon and the name
        # from the bundle, which is what a Mac user expects. Where the native
        # panel is unavailable it falls back to a plain message box.
        try:
            import wx.adv

            info = wx.adv.AboutDialogInfo()
            info.SetName(APP_NAME)
            info.SetVersion(APP_VERSION)
            info.SetDescription(description)
            info.SetCopyright("Copyright (c) 2026 doubletaponair and BlindPilot contributors")
            # No icon on purpose. Giving AboutDialogInfo one makes wx use its
            # own generic dialog instead of the native message box, and a
            # message box reads its whole text aloud the moment it opens.
            wx.adv.AboutBox(info, self)
        except Exception:
            wx.MessageBox(
                f"{APP_NAME} {APP_VERSION}\n\n{description}",
                f"About {APP_NAME}",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )

    def check_for_updates_silently(self) -> None:
        """Startup entry point: report only an available update, never network noise.

        Help, Check for Updates is `_show_update_dialog`, which does its own
        checking and installing.
        """
        if self._update_checking:
            return
        self._update_checking = True

        def work() -> None:
            release: Optional[ReleaseInfo] = None
            try:
                release = fetch_latest_release(APP_VERSION)
            except UpdateError:
                pass
            wx.CallAfter(self._on_update_checked, release)

        threading.Thread(target=work, daemon=True).start()

    def automatic_update_check_enabled(self) -> bool:
        return bool(_load_config().get("check_for_updates_at_startup", True))

    def _toggle_automatic_updates(self) -> None:
        enabled = self._automatic_updates_item.IsChecked()
        cfg = _load_config()
        cfg["check_for_updates_at_startup"] = enabled
        saved = _save_config(cfg)
        claim = (
            "BlindPilot will check for updates at startup"
            if enabled
            else "BlindPilot will not check for updates at startup"
        )
        self._announce_setting(claim if saved else _SAVE_FAILED)

    def _show_update_dialog(self) -> None:
        from update_dialog import UpdateDialog

        dialog = UpdateDialog(
            self, APP_VERSION, announce, confirm_install=self._confirm_update_install
        )
        try:
            dialog.ShowModal()
            restart = dialog.restart_pending
        finally:
            dialog.Destroy()
        if restart:
            self.Close(force=True)

    def _confirm_update_install(self) -> bool:
        """Installing closes BlindPilot. Say what that would cost, and ask.

        A running turn is stopped by the restart, and text typed in a prompt
        and not sent is gone. With neither, there is nothing to ask about.
        """
        panels = self._session_panels()
        running = sum(1 for p in panels if p._worker is not None)
        unsent = sum(1 for p in panels if p.prompt.GetValue().strip())
        if not running and not unsent:
            return True
        lost = []
        if running:
            lost.append(
                f"A turn is running in {running} {'tab' if running == 1 else 'tabs'}; "
                "installing stops it."
            )
        if unsent:
            lost.append(
                f"{unsent} {'tab has' if unsent == 1 else 'tabs have'} a message you have "
                "not sent, which will be lost."
            )
        message = " ".join(lost) + " Install the update now anyway?"
        with wx.MessageDialog(
            self, message, "BlindPilot Update", style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING
        ) as dialog:
            return dialog.ShowModal() == wx.ID_YES

    def report_failed_update(self) -> None:
        """Say why the last update did not install, if it did not.

        An update finishes after BlindPilot has closed, so a failure has no
        window to report to. The helper writes the reason down and this is the
        one place it gets read out — otherwise a failed update is silent, which
        is exactly how a broken updater went unnoticed for nine releases.
        """
        reason, log = pending_failure()
        clear_pending_failure()
        if not reason:
            return
        message = f"The last update did not install: {reason}"
        if log:
            message += f"\n\nWhat happened is written down in:\n{log}"
        announce(message)
        with wx.MessageDialog(
            self, message, "BlindPilot Update", style=wx.OK | wx.ICON_WARNING
        ) as dialog:
            dialog.ShowModal()

    def _on_update_checked(self, release: Optional[ReleaseInfo]) -> None:
        self._update_checking = False
        if release is None or not release.is_newer_than(APP_VERSION):
            return
        message = (
            f"BlindPilot {release.version} is available. "
            "Open Help, Check for Updates to review and install it."
        )
        self._set_status_text(message)
        announce(message)

    def _session_panels(self) -> list["SessionPanel"]:
        pages = (self.notebook.GetPage(i) for i in range(self.notebook.GetPageCount()))
        return [page for page in pages if isinstance(page, SessionPanel)]

    def _add_session(
        self,
        cwd: str,
        initial_prompt: str = "",
        session_title: str = "",
        open_main_chat: bool = True,
    ) -> "SessionPanel":
        panel = SessionPanel(
            self.notebook,
            cwd,
            on_status=self._panel_status_changed,
            on_title=self._panel_title_changed,
            earcons=self.earcons,
            on_side_chat=self._open_side_chat,
            get_backend=self.current_backend,
            focus_before=lambda: self.tab_switcher.SetFocus(),
            focus_after=lambda: self.mode_combo.SetFocus(),
            session_title=session_title,
        )
        # A named session says its name from the start. Without this the tab
        # falls back to the folder, which for a remote session may be empty --
        # and an unnamed tab is the one thing a screen reader cannot tell from
        # its neighbour.
        self.notebook.AddPage(panel, _tab_label(session_title, cwd), select=True)
        if (
            open_main_chat
            and self._backend == BACKEND_MUSEAI
            and not initial_prompt
            and not session_title
        ):
            panel.open_museai_main()
        self._relabel_tabs()
        if initial_prompt:
            panel.prompt.SetValue(initial_prompt)
            # Defer so the page is shown before the request fires.
            wx.CallAfter(panel.send_now)
        else:
            # Defer initial focus so VoiceOver picks it up after the page is shown.
            wx.CallAfter(panel.focus_prompt)
        return panel

    def _open_side_chat(self, cwd: str, message: str) -> None:
        """Open a /btw side chat as a new tab in the same directory."""
        self._add_session(cwd, initial_prompt=message)
        wx.CallAfter(announce, f"Side chat opened in {_short_label(cwd)}")

    def _add_conversation_webview(
        self,
        entry: HistoryEntry,
        *,
        missing: str = "No messages found in this conversation.",
    ) -> ConversationWebViewPanel:
        """Open a reading tab after the history picker has been destroyed."""
        opener = self.notebook.GetCurrentPage()
        panel = ConversationWebViewPanel(
            self.notebook,
            entry,
            entry.title or "Conversation",
            missing=missing,
            on_close=self._close_conversation_webview,
            on_command=lambda command: self._webview_command(panel, command),
        )
        panel.return_to = opener
        self.notebook.AddPage(panel, f"Read: {entry.title or 'Conversation'}", select=True)
        self._sync_tab_switcher()
        wx.CallAfter(panel.focus_content, from_navigation=True)
        return panel

    def _webview_command(self, page: ConversationWebViewPanel, command: str) -> None:
        if page._closed or self.notebook.GetCurrentPage() is not page:
            return
        if command == "close":
            page._close()
        elif command in ("next-tab", "previous-tab"):
            self._cycle_tab(1 if command == "next-tab" else -1)
        elif command in ("next-pane", "previous-pane"):
            self._cycle_pane(1 if command == "next-pane" else -1)
        elif command == "focus-content":
            page.focus_content()
        elif command.startswith("tab-") and command[4:] in "123456789" and len(command) == 5:
            self._jump_to_tab(int(command[4:]) - 1)

    def _close_conversation_webview(self, page: ConversationWebViewPanel) -> None:
        """Remove the requested reader, returning to its opener when possible."""
        index = next(
            (i for i in range(self.notebook.GetPageCount()) if self.notebook.GetPage(i) is page),
            wx.NOT_FOUND,
        )
        if index == wx.NOT_FOUND or page._closed:
            return
        selected = self.notebook.GetCurrentPage() is page
        opener = getattr(page, "return_to", None)
        page.mark_closed()
        self.notebook.DeletePage(index)
        if not self.notebook.GetPageCount():
            self._add_session(self._history_cwd(), open_main_chat=False)
        elif selected:
            for i in range(self.notebook.GetPageCount()):
                if self.notebook.GetPage(i) is opener:
                    self._select_session(i)
                    break
            _call_after_alive(self.focus_for_mode)
        self._follow_tab_backend()

    def _sync_tab_switcher(self) -> None:
        """Mirror the Simplebook's pages onto the visible tab strip.

        The strip's own pages stay empty placeholders: the conversation lives
        in the Simplebook below, so a placeholder holds no focusable child and
        Tab traversal walks straight past it into the real page.
        """
        self._syncing_tab_switcher = True
        try:
            count = self.notebook.GetPageCount()
            while self.tab_switcher.GetPageCount() > count:
                self.tab_switcher.DeletePage(self.tab_switcher.GetPageCount() - 1)
            added = False
            while self.tab_switcher.GetPageCount() < count:
                self.tab_switcher.AddPage(wx.Panel(self.tab_switcher), "")
                added = True
            if added:
                # A page added after the first sits at its default size in
                # the control's top-left corner until the control is next
                # sized, and with the strip cut down to its tab row that
                # corner is the first tab. A size event puts every page in
                # the (empty) page area straight away.
                self.tab_switcher.SendSizeEvent()
            for index in range(count):
                label = self.notebook.GetPageText(index)
                if self.tab_switcher.GetPageText(index) != label:
                    self.tab_switcher.SetPageText(index, label)
            sel = self.notebook.GetSelection()
            if 0 <= sel < count and self.tab_switcher.GetSelection() != sel:
                # ChangeSelection, not SetSelection: this is the strip catching
                # up with the book, and must not be reported back as a request
                # to change the book.
                self.tab_switcher.ChangeSelection(sel)
        finally:
            self._syncing_tab_switcher = False

    def _on_tab_switcher_changed(self, event: wx.BookCtrlEvent) -> None:
        """Arrowing along the strip, or clicking a tab, switches the session."""
        event.Skip()
        if self._syncing_tab_switcher:
            return
        self._select_session(event.GetSelection())

    # ----- Options menu -----
    def _menu_item(self, menu, label, help_text, action, item_id=wx.ID_ANY):
        """Append one item and bind it, so neither can be added without the other.

        Bound through `_gui_work`, so every menu item's chord is measured
        against the GUI-thread budget. The label names the item in the record
        and in the log, which is the whole point of measuring here rather than
        one handler at a time.
        """
        item = menu.Append(item_id, label, help_text)
        handler = _gui_work(label.replace("&", ""), action)
        self.Bind(wx.EVT_MENU, lambda _event: handler(), item)
        return item

    def _agent_item(self, menu, label, help_text, action, item_id=wx.ID_ANY):
        """A menu item that acts on the visible session tab; see `_set_app_mode`."""
        item = self._menu_item(menu, label, help_text, action, item_id)
        self._agent_menu_items.append(item)
        return item

    def _bind_shortcut(self, bind_id, name: str, action) -> None:
        """Bind an accelerator, measured against the GUI-thread budget.

        Every chord in the table goes through here, not only the ones that are
        slow today, so a handler that grows expensive is named in the record
        rather than felt as a stall. A method rather than a nested function
        because the guard has to be able to reach it.
        """
        handler = _gui_work(name, action)
        self.Bind(wx.EVT_MENU, lambda _event: handler(), id=bind_id)

    def _build_model_menu(self) -> wx.Menu:
        """What answers you, and what it may do.

        Everything the prompt's slash commands reach (/model, /status,
        /connect) has an item here, so it can be found without knowing the
        word. Built as a method so the menu can be read by a test.
        """
        menu = wx.Menu()
        add = self._agent_item
        self._agent_menu_items.append(
            menu.AppendSubMenu(
                self._build_backend_menu(),
                "&Backend",
                "Choose which coding-agent CLI BlindPilot uses",
            )
        )
        add(
            menu,
            # Deliberately not Ctrl+M: on macOS that chord is the system's
            # Minimize, which lives in the Window menu. Ctrl+Shift+E is free.
            "&Model and Effort…	Ctrl+Shift+E",
            "Choose the model and effort level this conversation runs at",
            self._model_active,
        )
        self._permission_menu_item = menu.AppendSubMenu(
            self._build_permission_mode_menu(),
            "&Permission Mode",
            "Choose what the backend may do without asking, for this conversation",
        )
        self._agent_menu_items.append(self._permission_menu_item)
        add(
            menu,
            "Session &Status…",
            "Show the backend, model, and account this conversation is using",
            self._status_active,
        )
        add(
            menu,
            "T&urn Status\tCtrl+Shift+T",
            "Say how long the running turn has worked, its last step, and what is queued",
            self._turn_status_active,
        )
        add(
            menu,
            "Changed &Files…\tCtrl+Shift+D",
            "List the files that differ from the last commit, and read each change",
            self._changed_files_active,
        )
        self._menu_item(
            menu,
            "What Agents &Know…\tCtrl+Shift+I",
            "Read and change the memories, tastes, instructions and skills your coding agents keep",
            self._claude_files_active,
        )
        menu.AppendSeparator()
        add(
            menu,
            "Backend Se&ttings…",
            "Open the file a coding agent reads its own settings from",
            self._settings_files_active,
        )
        add(
            menu,
            "Ma&nage Backends...",
            "Install, update, or sign in to a backend",
            self._manage_backends,
        )
        self._connect_item = self._menu_item(
            menu,
            "&Connect a Provider…",
            "Connect a provider to opencode, or disconnect one",
            self._connect_active,
        )
        self._museai_menu_item = wx.MenuItem(
            menu,
            wx.ID_ANY,
            "muse.ai",
            "Read approvals, schedules, feed, and ideas",
            subMenu=self._build_museai_menu(),
        )
        self._agent_menu_items.append(self._museai_menu_item)
        self._model_menu = menu
        return menu

    def _refresh_museai_menu(self) -> None:
        menu = getattr(self, "_model_menu", None)
        if menu is None:
            return
        museai = self._app_mode == APP_MODE_AGENT and self._backend == BACKEND_MUSEAI
        wanted = self._museai_menu_item if museai else self._permission_menu_item
        other = self._permission_menu_item if museai else self._museai_menu_item
        items = list(menu.GetMenuItems())
        if other in items:
            position = items.index(other)
            menu.Remove(other)
            menu.Insert(position, wanted)
            wanted.Enable(self._app_mode == APP_MODE_AGENT)

    def _build_museai_menu(self) -> wx.Menu:
        menu = wx.Menu()
        for view in ("approvals", "schedules", "feed", "ideas", "goals"):
            self._menu_item(
                menu,
                f"&{view.title()}…",
                f"Read muse.ai {view}",
                lambda chosen=view: self._museai_active(chosen),
            )
        return menu

    def _museai_active(self, view: str) -> None:
        with MuseAiDialog(self, view) as dialog:
            dialog.ShowModal()
            request = dialog.chat_request
        if not request:
            return
        kind, value, title = request
        serial = getattr(self, "_museai_active_serial", 0) + 1
        self._museai_active_serial = serial
        self._announce_setting("Loading muse.ai conversation")

        def work() -> None:
            try:
                if kind in ("draft", "main"):
                    from museai_backend import museai_main_session_id

                    session = museai_main_session_id()
                else:
                    session = value
                if not session:
                    raise ValueError("Could not open the muse.ai main chat.")
                entry = HistoryEntry(
                    backend=BACKEND_MUSEAI,
                    session_id=session,
                    title=f"Discuss: {title}" if kind == "draft" else title,
                    path="",
                    modified=time.time(),
                    folder="muse.ai",
                    cwd="",
                )
                turns = load_turns(entry)
                error = ""
            except Exception as exc:
                entry, turns, session, error = None, [], None, str(exc)
            _call_after_alive(
                self._museai_chat_loaded, serial, kind, value, entry, turns, session, error
            )

        threading.Thread(target=work, daemon=True).start()

    def _museai_chat_loaded(self, serial, kind, value, entry, turns, session, error) -> None:
        if not self or serial != getattr(self, "_museai_active_serial", 0):
            return
        if error or entry is None:
            message = f"Error: Could not open the muse.ai conversation. {error} The discussion was not sent."
            self._announce_setting(message)
            return
        panel = self._add_session(self._history_cwd(), open_main_chat=False)
        panel.restore_history(entry, turns)
        self._follow_tab_backend()
        if kind == "draft":
            panel.prompt.SetValue(value)
            panel.prompt.SetInsertionPointEnd()
            panel.focus_prompt()
            announce("Discussion draft ready. Add your question, then press Send.")
        elif kind == "follow" and not turns:
            wx.CallAfter(panel._follow_museai, session)

    def _build_file_menu(self) -> wx.Menu:
        """Sessions, tabs, and the application itself.

        A chord written in brackets rather than after a tab is one the frame's
        own accelerator table already carries: a tab here would register a
        second menu accelerator for the same key, and Windows will not fire a
        menu accelerator whose key is Tab at all.
        """
        menu = wx.Menu()
        # Kept so the Hermes conversation list can be taken out of this menu and
        # put back as the backend changes.
        self._file_menu = menu
        add = self._agent_item
        add(
            menu,
            "&New Session…	Ctrl+T",
            "Type or browse to a folder and open a session in it",
            self._new_session,
            wx.ID_NEW,
        )
        add(
            menu,
            # Deliberately not Ctrl+H: on macOS that chord is the system's
            # Hide BlindPilot, which the application menu wins.
            # Ctrl+Shift+H is free.
            "&Recent Conversations…	Ctrl+Shift+H",
            "Reopen a past conversation and carry on with it",
            self._open_history,
        )
        self._hermes_sessions_item = self._menu_item(
            menu,
            "Hermes &Conversations…	Ctrl+G",
            "List every conversation Hermes knows, including the ones running right now",
            self._open_hermes_sessions,
        )
        add(
            menu,
            "&Side Chat in This Folder",
            "Open a second conversation in the same folder, without disturbing this one",
            self._side_chat_active,
        )
        menu.AppendSeparator()
        next_chord, prev_chord = _tab_chord_notes()
        add(
            menu,
            f"Ne&xt Session\t{next_chord}",
            "Move to the next conversation tab",
            lambda: self._cycle_tab(+1),
        )
        add(
            menu,
            f"Previo&us Session\t{prev_chord}",
            "Move to the previous conversation tab",
            lambda: self._cycle_tab(-1),
        )
        menu.AppendSeparator()
        self._menu_item(
            menu,
            "Set &Projects Folder…",
            "Choose the folder that contains your projects",
            self._set_projects_folder,
        )
        self._menu_item(
            menu,
            "Create &Desktop Shortcut",
            "Put a BlindPilot shortcut on the desktop",
            self._create_desktop_shortcut,
        )
        menu.AppendSeparator()
        add(
            menu,
            "&Close Session	Ctrl+W",
            "Close the current session tab",
            self._close_current_session,
            wx.ID_CLOSE,
        )
        self._menu_item(menu, "&Quit	Ctrl+Q", "Leave BlindPilot", self.Close, wx.ID_EXIT)
        return menu

    def _build_conversation_menu(self) -> wx.Menu:
        """This conversation, and the message about to be added to it.

        Attach, Slash Command and Jump to Latest Response were reachable by
        chord alone. Two of them have a button beside the prompt as well, which
        is how a sighted application is meant to work - the menu is the
        complete list, the button is the shortcut to a frequent one, and the
        menu item is where the chord is learnt.
        """
        menu = wx.Menu()
        add = self._agent_item
        add(
            menu,
            "S&top Task	Ctrl+.",
            "Stop the task running in this session",
            self._stop_active,
            wx.ID_STOP,
        )
        add(
            menu,
            "&Attach Files…\tCtrl+Shift+A",
            "Attach files to the next message",
            self._attach_active,
        )
        add(
            menu,
            "S&lash Command…\tCtrl+/",
            "Pick one of this backend's slash commands from a list",
            self._slash_active,
        )
        add(
            menu,
            "Saved &Prompts…	Ctrl+Shift+P",
            "Insert, save and arrange messages you send often",
            self._saved_prompts_active,
        )
        menu.AppendSeparator()
        self._compact_item = self._menu_item(
            menu,
            "Co&mpact Conversation	Ctrl+Shift+K",
            "Summarise this conversation so the backend has room to keep going",
            self._compact_active,
        )
        # Not an agent item: the handler routes by mode already, and Chat
        # mode needs this chord just as much. Left in the agent list it was
        # greyed out there, and a Chat conversation with no way to start a
        # fresh one kept taking every message that followed.
        self._menu_item(
            menu,
            "Start N&ew Conversation	Ctrl+Shift+N",
            "Forget this conversation and start a fresh one",
            self._new_conversation_active,
        )
        menu.AppendSeparator()
        browse = wx.Menu()
        add(
            browse,
            "&Find in Responses…	Ctrl+F",
            "Search the responses in this session",
            self._find_active,
            wx.ID_FIND,
        )
        self._menu_item(
            browse,
            "&Code Blocks…\tCtrl+Shift+B",
            "Read and copy every code block in this conversation",
            lambda: self._browse_items_active("code"),
        )
        self._menu_item(
            browse,
            "&Links…\tCtrl+Shift+L",
            "Read, copy or open links in this conversation",
            lambda: self._browse_items_active("links"),
        )
        add(
            browse,
            "Task L&ist…\tCtrl+Shift+J",
            "List the agent's task list: each step, and whether it is to do, in progress or done",
            self._task_list_active,
        )
        add(
            browse,
            "Co&mmands…\tCtrl+Shift+X",
            "List every command the agent ran, with whether it finished and its output",
            lambda: self._panel_action("open_commands"),
        )
        add(
            browse,
            "&Decisions…\tCtrl+Shift+Y",
            "List every question and permission request, with how you answered",
            lambda: self._panel_action("open_decisions"),
        )
        add(
            browse,
            "Find in &All Conversations…\tCtrl+Shift+S",
            "Search the messages of every past conversation, and reopen one at the match",
            self._find_in_all_conversations,
        )
        # Every top-level menu is at its ten-item ceiling; this acts on a
        # response, as the lists above do.
        browse.AppendSeparator()
        add(
            browse,
            "Send Response to An&other Session…\tCtrl+Shift+O",
            "Hand a response to another session, once or back and forth as an agent conversation",
            self._send_to_tab_active,
        )
        add(
            browse,
            "Pass On the Waiting &Reply…",
            "Send on the reply an agent conversation is holding for your approval",
            self._pass_on_waiting_reply,
        )
        add(
            browse,
            "&Properties…\tAlt+Enter",
            "This session's backend, folder, model, counts and progress, one line each",
            lambda: self._panel_action("open_properties"),
        )
        add(
            browse,
            "Chat &Permissions…",
            "What this session may do without asking: its mode, what you allowed for it, "
            "and the rules in Claude Code's own settings",
            lambda: self._panel_action("open_chat_permissions"),
        )
        add(
            browse,
            "Copy Session Lin&k",
            "Copy a link that reopens this conversation in BlindPilot",
            lambda: self._panel_action("copy_session_link"),
        )
        add(
            browse,
            "&Remote Control…",
            "Reach this Claude Code session from claude.ai while it works, or turn that off",
            lambda: self._panel_action("remote_control_menu"),
        )
        add(
            browse,
            "Other &Machines…\tCtrl+Shift+U",
            "List and message your Claude Code sessions on other computers, through Claude",
            lambda: self._panel_action("other_machines"),
        )
        menu.AppendSubMenu(browse, "Find and &Browse")
        add(
            menu,
            "&Jump to Latest Response\tCtrl+R",
            "Move to the newest response, then back through the ones before it",
            self._jump_to_latest_response,
        )
        # Not an agent item: Chat mode speaks too, and losing a line to a
        # keystroke is no less likely there.
        self._menu_item(
            menu,
            "Repeat Last Announ&cement\tCtrl+Shift+R",
            "Say again the last thing BlindPilot announced",
            repeat_last_announcement,
        )
        add(
            menu,
            "E&xport Conversation…\tCtrl+E",
            "Save this conversation to a Markdown or text file",
            self._export_active,
        )
        return menu

    def _side_chat_active(self) -> None:
        """A second conversation in the same folder as the visible tab."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            self._open_side_chat(page.cwd, "")

    def _build_backend_menu(self) -> wx.Menu:
        """The Backend submenu: one radio item per coding-agent CLI."""
        menu = wx.Menu()
        self._backend_items: dict[str, wx.MenuItem] = {}
        for backend in BACKEND_IDS:
            item = menu.AppendRadioItem(wx.ID_ANY, BACKEND_LABELS[backend])
            item.Check(backend == self._backend)
            self._backend_items[backend] = item

            def set_backend(_event: wx.CommandEvent, chosen: str = backend) -> None:
                self._set_backend(chosen)

            self.Bind(
                wx.EVT_MENU, _gui_work(f"Backend: {BACKEND_LABELS[backend]}", set_backend), item
            )
        return menu

    def _build_permission_mode_menu(self) -> wx.Menu:
        """The Permission Mode submenu: one radio item per mode.

        Radio rather than check, because the modes are exclusive and that is
        what a screen reader says about them when they are built this way.
        """
        menu = wx.Menu()
        self._mode_items: dict[str, wx.MenuItem] = {}
        for value, label, description in PERMISSION_MODES:
            item = menu.AppendRadioItem(wx.ID_ANY, label, description)
            self._mode_items[value] = item

            def set_mode(_event: wx.CommandEvent, mode: str = value) -> None:
                self._set_mode_active(mode)

            self.Bind(wx.EVT_MENU, _gui_work(f"Permission mode: {label}", set_mode), item)
        return menu

    def _refresh_mode_items(self) -> None:
        """Point the menu at the visible tab's mode.

        The mode belongs to the conversation, not to the window, so switching
        tabs has to move the mark with it or the menu describes another tab.
        """
        items = getattr(self, "_mode_items", None)
        notebook = getattr(self, "notebook", None)
        if not items or notebook is None:
            # The menu bar is built before the notebook it describes, so this
            # runs once with nothing to point at. Adding the first tab fires a
            # page change, which brings it straight back.
            return
        page = notebook.GetCurrentPage()
        if not isinstance(page, SessionPanel):
            return
        item = items.get(page.mode)
        if item is not None and not item.IsChecked():
            item.Check(True)

    def _refresh_hermes_sessions_item(self) -> None:
        """Offer Hermes' conversation list only while Hermes is the backend.

        Greying it out, the way Compact and Connect are treated, is the wrong
        answer here. Those two are commands for THIS conversation that the
        current backend cannot perform, so a disabled item with a reason tells
        you something worth knowing. This one asks a Hermes for its own
        conversations: with another backend selected there is no Hermes in the
        picture at all, so the item is not "unavailable", it is irrelevant --
        and an irrelevant item still costs an arrow press to read past. The
        File menu is at the ten-item ceiling upstream set for exactly that
        reason, so the item is REMOVED rather than disabled, and File is nine
        items long for everybody who does not use Hermes.

        Removing keeps the accelerator too: Ctrl+G is registered by this menu
        item, so with the item gone the chord does nothing rather than opening a
        dialog that would immediately report there is no Hermes to ask.
        """
        item = getattr(self, "_hermes_sessions_item", None)
        menu = item.GetMenu() if item is not None else None
        wanted = self._app_mode == APP_MODE_AGENT and self._backend == BACKEND_HERMES
        if wanted:
            if item is not None and menu is None:
                # Put it back where it was: immediately after Recent
                # Conversations, so its place in the menu does not depend on
                # how many times the backend has been switched.
                self._insert_hermes_sessions_item()
            return
        if item is not None and menu is not None:
            menu.Remove(item)

    def _insert_hermes_sessions_item(self) -> None:
        """Re-insert the Hermes list after Recent Conversations."""
        menu = getattr(self, "_file_menu", None)
        item = getattr(self, "_hermes_sessions_item", None)
        if menu is None or item is None:
            return
        position = 0
        for index, existing in enumerate(menu.GetMenuItems()):
            if "Recent Conversations" in existing.GetItemLabelText():
                position = index + 1
                break
        menu.Insert(position, item)

    def _refresh_connect_item(self) -> None:
        """Grey out Connect for a backend that has no providers to connect.

        Greyed with a reason rather than offered and then refused, which is how
        Compact already treats a backend that cannot compact.
        """
        item = getattr(self, "_connect_item", None)
        if item is None:
            return
        supported = self._app_mode == APP_MODE_AGENT and self._backend == BACKEND_OPENCODE
        item.Enable(supported)
        if supported:
            item.SetHelp("Connect a provider to opencode, or disconnect one")
        else:
            item.SetHelp("Only opencode connects providers.")

    def _model_active(self) -> None:
        """Pick the model and effort for the active tab (Ctrl+Shift+E)."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_model_dialog()

    def _turn_status_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.turn_status()

    def _changed_files_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_changed_files()

    def _status_active(self) -> None:
        """What the active tab is set to (Model > Session Status, or /status)."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_status_dialog()

    def _claude_files_active(self) -> None:
        """What every backend keeps about you (Model menu, Ctrl+Shift+I).

        A list of each backend's kinds with their counts, then the entries of
        the chosen one; Enter reads one, and Escape steps back a list. Claude
        Code memories, Command Code tastes, Hermes memories and global agents
        can be added and deleted here. Project agents can be read and edited.
        """
        page = self.notebook.GetCurrentPage()
        cwd = page.cwd if isinstance(page, SessionPanel) else None
        self._agent_files_return_focus = wx.Window.FindFocus()
        key = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
        self._agent_files_serial += 1
        serial = self._agent_files_serial
        groups = self._agent_files_cache.get(key)
        if groups is None:
            self._load_agent_files(cwd, page, 0)
            return
        self._show_agent_files(cwd, page, serial, groups, 0)

    def _load_agent_files(
        self,
        cwd: Optional[str],
        page: Optional[wx.Window],
        group_at: int,
        open_group: Optional[tuple[str, str]] = None,
        selected_key: Optional[str] = None,
    ) -> None:
        """Discover agent files off the GUI thread, then show the current request."""
        self._agent_files_serial += 1
        serial = self._agent_files_serial
        key = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
        self._agent_files_cache.pop(key, None)
        self._announce_setting("Looking for what agents know…")

        def work() -> None:
            try:
                groups = agent_files(cwd)
                error = ""
            except Exception as exc:
                groups, error = [], str(exc)
            _call_after_alive(
                self._agent_files_loaded,
                serial,
                cwd,
                page,
                group_at,
                open_group,
                selected_key,
                groups,
                error,
            )

        threading.Thread(target=work, daemon=True).start()

    def _agent_files_loaded(
        self,
        serial: int,
        cwd: Optional[str],
        page: Optional[wx.Window],
        group_at: int,
        open_group: Optional[tuple[str, str]],
        selected_key: Optional[str],
        groups: list[AgentFiles],
        error: str,
    ) -> None:
        if (
            not self
            or serial != self._agent_files_serial
            or self._app_mode != APP_MODE_AGENT
            or self.notebook.GetCurrentPage() is not page
        ):
            return
        if error:
            self._announce_setting(f"Error: could not list what agents know: {error}")
            self._restore_agent_files_focus(page)
            return
        key = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
        self._agent_files_cache[key] = groups
        self._set_status_text("What Agents Know is ready")
        self._show_agent_files(cwd, page, serial, groups, group_at, open_group, selected_key)

    def _restore_agent_files_focus(self, page: Optional[wx.Window]) -> None:
        focus = getattr(self, "_agent_files_return_focus", None)
        if self.notebook.GetCurrentPage() is page and focus and focus.IsShownOnScreen():
            focus.SetFocus()

    def _show_agent_files(
        self,
        cwd: Optional[str],
        page: Optional[wx.Window],
        serial: int,
        groups: list[AgentFiles],
        group_at: int,
        open_group: Optional[tuple[str, str]] = None,
        selected_key: Optional[str] = None,
    ) -> None:
        if open_group:
            selected_group_at = next(
                (
                    at
                    for at, group in enumerate(groups)
                    if (group.backend, group.kind) == open_group
                ),
                None,
            )
            if selected_group_at is not None and self._agent_files_of(
                cwd, page, groups, selected_group_at, selected_key
            ):
                return
            if selected_group_at is not None:
                group_at = selected_group_at
        while True:
            message = "What your coding agents keep about you on this computer. Enter lists one."
            agents = [
                f"{g.backend} {g.kind.lower()} {len(g.files)}"
                for g in groups
                if g.kind in ("Agents", "Project Agents")
            ]
            if agents:
                message += "\n\nAgents installed: " + " · ".join(agents)
            with wx.SingleChoiceDialog(
                self,
                message,
                "What Agents Know",
                [f"{g.backend} {g.kind.lower()}, {len(g.files)}" for g in groups]
                + ["Refresh the list"],
            ) as dlg:
                dlg.SetSelection(min(group_at, len(groups)))
                if dlg.ShowModal() != wx.ID_OK:
                    break
                chosen = dlg.GetSelection()
            if chosen == len(groups):
                self._agent_files_cache.pop(
                    os.path.normcase(os.path.abspath(cwd)) if cwd else "", None
                )
                self._load_agent_files(cwd, page, group_at)
                return
            group_at = chosen
            if self._agent_files_of(cwd, page, groups, group_at):
                return
        self._restore_agent_files_focus(page)

    def _agent_files_of(
        self,
        cwd: Optional[str],
        page: Optional[wx.Window],
        groups: list[AgentFiles],
        group_at: int,
        selected_key: Optional[str] = None,
    ) -> bool:
        add_labels = {
            "memory": "&Add Memory…",
            "taste": "&Add Taste…",
            "hermes": "&Add Memory…",
            "agent": "&Add Agent…",
        }
        group = groups[group_at]
        backend, kind = group.backend, group.kind
        title = f"{backend} {kind.lower()}"
        keys = [key for key, _label in group.files]
        file_at = keys.index(selected_key) if selected_key in keys else 0
        while True:
            files = group.files
            labels = [label for _key, label in files]
            with AgentFileList(
                self, title, labels, file_at, add_labels.get(group.editable, "")
            ) as dlg:
                action = dlg.ShowModal()
                file_at = max(dlg.list.GetSelection(), 0)
            if action == wx.ID_ADD:
                added = self._add_entry(cwd, group)
                if added:
                    self._load_agent_files(cwd, page, group_at, (backend, kind), added)
                    return True
            elif action == wx.ID_DELETE:
                key, label = files[file_at]
                if (
                    wx.MessageBox(
                        f"Delete {label}? This cannot be undone.",
                        f"Delete from {title}",
                        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                        self,
                    )
                    == wx.YES
                ):
                    try:
                        delete_entry(key, group.editable)
                        announce("Deleted")
                        self._load_agent_files(cwd, page, group_at, (backend, kind))
                        return True
                    except (OSError, IndexError) as exc:
                        announce(f"Error: could not delete it: {exc}")
            elif action in (wx.ID_OPEN, wx.ID_EDIT):
                key = files[file_at][0]
                try:
                    path, text = entry_text(key, group.editable)
                except (OSError, IndexError) as exc:
                    announce(f"Error: could not read it: {exc}")
                    continue
                saved = False

                def save(text: str, entry_key: str = key, editable: str = group.editable) -> None:
                    nonlocal saved
                    save_entry(entry_key, editable, text)
                    saved = True

                view = ReadView(
                    self,
                    text,
                    path,
                    edit_path=path,
                    on_save=save,
                    start_editing=action == wx.ID_EDIT,
                )
                try:
                    view.ShowModal()
                finally:
                    view.Destroy()
                if saved:
                    self._load_agent_files(cwd, page, group_at, (backend, kind), key)
                    return True
            else:
                return False

    def _add_entry(self, cwd: Optional[str], group: AgentFiles) -> Optional[str]:
        """Ask for a new entry and save it; its key, or None."""
        if group.editable == "memory":
            return self._add_memory(cwd)
        assert group.home is not None
        home = group.home
        if group.editable == "agent":
            backend = next(key for key, label in BACKEND_LABELS.items() if label == group.backend)
            with AddAgentDialog(self, group.backend) as dlg:
                while dlg.ShowModal() == wx.ID_OK:
                    try:
                        path = add_agent(
                            home,
                            backend,
                            dlg.name.GetValue().strip(),
                            dlg.description.GetValue(),
                            dlg.instructions.GetValue(),
                        )
                    except (OSError, ValueError) as exc:
                        wx.MessageBox(str(exc), "Add Agent", wx.OK | wx.ICON_ERROR, dlg)
                        continue
                    announce("Agent added")
                    return str(path)
            return None
        if group.editable == "taste":
            title, place, editable = "Add Taste", "&Category, or type a new one:", True
            places = sorted(
                {taste_category(p, home) for p in (home / "taste").glob("**/taste.md")}
                | {"general"}
            )
        else:
            title, place, editable = "Add Hermes Memory", "&File:", False
            places = ["MEMORY.md, notes about your work", "USER.md, about you"]
        with AddNoteDialog(self, title, place, places, editable) as dlg:
            while dlg.ShowModal() == wx.ID_OK:
                text = dlg.text.GetValue().strip()
                if not text:
                    continue
                try:
                    if group.editable == "taste":
                        path = add_taste(home, dlg.place.GetValue(), text)
                        at = len(path.read_text(encoding="utf-8").splitlines()) - 1
                    else:
                        name = ("MEMORY.md", "USER.md")[max(dlg.place.GetSelection(), 0)]
                        path = home / "memories" / name
                        add_hermes_memory(path, text)
                        at = len(hermes_entries(path)) - 1
                except OSError as exc:
                    wx.MessageBox(str(exc), title, wx.OK | wx.ICON_ERROR, dlg)
                    continue
                announce("Added")
                return f"{path}\n{at}"
        return None

    def _add_memory(self, cwd: Optional[str]) -> Optional[str]:
        """Ask for a new memory and save it; the path saved, or None."""
        folders = memory_folders(cwd)
        if not folders:
            announce("Open a session tab first, so the memory has a project to belong to")
            return None
        with AddMemoryDialog(self, folders) as dlg:
            while dlg.ShowModal() == wx.ID_OK:
                folder, name, description, kind, body = dlg.values()
                if not (name and description and body.strip()):
                    wx.MessageBox(
                        "A memory needs a name, a description and its text.",
                        "Add Memory",
                        wx.OK | wx.ICON_INFORMATION,
                        dlg,
                    )
                    continue
                try:
                    path = add_memory(folder, name, description, kind, body)
                except OSError as exc:
                    wx.MessageBox(str(exc), "Add Memory", wx.OK | wx.ICON_ERROR, dlg)
                    continue
                announce("Memory added")
                return str(path)
        return None

    def _settings_files_active(self) -> None:
        """Where the backends keep their settings (Model > Backend Settings).

        The active tab's folder decides the project-level entries, so it is
        built when opened rather than being a submenu that would go stale the
        moment somebody switched tabs.
        """
        page = self.notebook.GetCurrentPage()
        cwd = page.cwd if isinstance(page, SessionPanel) else None
        with SettingsFilesDialog(self, cwd) as dlg:
            dlg.ShowModal()

    def _connect_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_connect_dialog()

    def _set_mode_active(self, value: str) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page._set_mode(value)

    def _toggle_setting(self, item: wx.MenuItem, attr: str, on_text: str, off_text: str) -> None:
        """One checkable setting: store what the menu now says, and say it."""
        value = item.IsChecked()
        setattr(SETTINGS, attr, value)
        saved = SETTINGS.save()
        self._announce_setting((on_text if value else off_text) if saved else _SAVE_FAILED)

    def _toggle_live_rows(self) -> None:
        self._toggle_setting(
            self._rows_item,
            "live_rows",
            "Live activity in the list on",
            "Live activity in the list off",
        )

    def _toggle_speak_live(self) -> None:
        self._toggle_setting(
            self._speak_item,
            "speak_live",
            "Speaking activity aloud on",
            "Speaking activity aloud off",
        )

    def _turn_in_flight(self) -> bool:
        """Whether the tab in front is in the middle of a turn.

        Used to decide if a cue change should be applied now: the sounds belong
        to the frame, but only a running turn has one playing.
        """
        page = self.notebook.GetCurrentPage()
        worker = getattr(page, "_worker", None) if isinstance(page, SessionPanel) else None
        return worker is not None and worker.is_alive()

    def _choose_progress_cue(self, mode: str) -> None:
        """Pick how the working sound behaves, and apply it to a running turn.

        Applied at once rather than from the next turn: the setting exists
        because the sound is intrusive, and someone reaching for it mid-turn
        wants it to stop now.
        """
        SETTINGS.progress_cue = _valid_progress_cue(mode)
        saved = SETTINGS.save()
        if mode == CUE_OFF:
            self.earcons.stop_progress()
            spoken = "off"
        elif mode == CUE_PERIODIC:
            spoken = f"every {SETTINGS.progress_cue_seconds} seconds"
        else:
            spoken = "continuous"
        if mode != CUE_OFF and self._turn_in_flight():
            # Restart under the new mode so a turn in flight follows the choice
            # instead of keeping the behaviour it started with.
            self.earcons.start_progress()
        self._announce_setting(f"Working sound {spoken}" if saved else _SAVE_FAILED)

    def _configure_cue_interval(self) -> None:
        """Ask how many seconds between working sounds.

        A number entry rather than a set of fixed choices: what counts as too
        often is a matter of hearing and of how long the runs are.
        """
        dialog = wx.NumberEntryDialog(
            self,
            "How many seconds between working sounds?",
            f"Seconds ({CUE_SECONDS_MIN} to {CUE_SECONDS_MAX})",
            "Working sound interval",
            SETTINGS.progress_cue_seconds,
            CUE_SECONDS_MIN,
            CUE_SECONDS_MAX,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            seconds = _valid_cue_seconds(dialog.GetValue())
        finally:
            dialog.Destroy()
        SETTINGS.progress_cue_seconds = seconds
        # Choosing an interval says what the sound should do, so the mode
        # follows: setting an interval while the sound is off or continuous
        # would otherwise change nothing anyone can hear.
        SETTINGS.progress_cue = CUE_PERIODIC
        saved = SETTINGS.save()
        self._cue_periodic_item.Check(True)
        if self._turn_in_flight():
            self.earcons.start_progress()
        self._announce_setting(f"Working sound every {seconds} seconds" if saved else _SAVE_FAILED)

    def _toggle_show_thinking(self) -> None:
        self._toggle_setting(
            self._thinking_item,
            "show_thinking",
            "The backend's reasoning is shown",
            "The backend's reasoning is hidden",
        )

    def _open_log_folder(self) -> None:
        """Show where the diagnostics go, rather than reading out a path."""
        if diagnostics.open_log_folder():
            self._announce_setting("Log folder opened")
            return
        self._announce_setting(f"Error: could not open {diagnostics.log_dir()}")

    def _build_narration_menu(self) -> wx.Menu:
        """How much of a run is spoken. Radio items: the modes are exclusive."""
        menu = wx.Menu()
        self._narration_items: dict[str, wx.MenuItem] = {}
        for mode, label, help_text in NARRATION_MODES:
            item = menu.AppendRadioItem(wx.ID_ANY, label, help_text)
            item.Check(mode == SETTINGS.narration)
            self._narration_items[mode] = item

            def set_narration(_event: wx.CommandEvent, chosen: str = mode) -> None:
                self._set_narration(chosen)

            self.Bind(wx.EVT_MENU, _gui_work(f"Narration: {label}", set_narration), item)
        return menu

    def _set_narration(self, mode: str) -> None:
        SETTINGS.narration = mode
        saved = SETTINGS.save()
        label = next(text for key, text, _help in NARRATION_MODES if key == mode)
        self._announce_setting(f"Narration: {label.replace('&', '')}" if saved else _SAVE_FAILED)

    def _build_sound_cue_menu(self) -> wx.Menu:
        """One check item per cue, under the master switch that governs them.

        Greyed out while the master switch is off, because three live
        switches beneath something that mutes all three would be describing
        a choice that is not there.
        """
        menu = wx.Menu()
        self._sound_cue_items: dict[str, wx.MenuItem] = {}
        for cue, label, help_text in SOUND_CUES:
            item = menu.AppendCheckItem(wx.ID_ANY, label, help_text)
            item.Check(SETTINGS.sound_cues.get(cue, True))
            item.Enable(SETTINGS.sounds_enabled)
            self._sound_cue_items[cue] = item

            def toggle_cue(_event: wx.CommandEvent, key: str = cue) -> None:
                self._toggle_sound_cue(key)

            self.Bind(wx.EVT_MENU, _gui_work(f"Sound cue: {label}", toggle_cue), item)
        return menu

    def _toggle_sound_cue(self, cue: str) -> None:
        item = self._sound_cue_items[cue]
        SETTINGS.sound_cues[cue] = item.IsChecked()
        saved = SETTINGS.save()
        self.earcons.set_cues(SETTINGS.sound_cues)
        label = next(text for key, text, _help in SOUND_CUES if key == cue).replace("&", "")
        state = "on" if SETTINGS.sound_cues[cue] else "off"
        self._announce_setting(f"{label} sound {state}" if saved else _SAVE_FAILED)

    def _toggle_sounds(self) -> None:
        SETTINGS.sounds_enabled = self._sounds_item.IsChecked()
        saved = SETTINGS.save()
        self.earcons.set_enabled(SETTINGS.sounds_enabled)
        # The cues below it describe a choice that is not available while
        # everything is muted.
        for item in getattr(self, "_sound_cue_items", {}).values():
            item.Enable(SETTINGS.sounds_enabled)
        state = "on" if SETTINGS.sounds_enabled else "off"
        self._announce_setting(f"Sound cues {state}" if saved else _SAVE_FAILED)

    def _toggle_text_view(self) -> None:
        SETTINGS.text_view = self._text_view_item.IsChecked()
        saved = SETTINGS.save()
        for page in self._session_panels():
            page.apply_view_mode()
        claim = (
            "Responses are now a read-only text field, one row per line"
            if SETTINGS.text_view
            else "Responses are now a list"
        )
        self._announce_setting(claim if saved else _SAVE_FAILED)

    def _use_silent_until_response_mode(self) -> None:
        """One action to remain silent until the complete response arrives."""
        SETTINGS.live_rows = False
        SETTINGS.speak_live = False
        saved = SETTINGS.save()
        self._rows_item.Check(False)
        self._speak_item.Check(False)
        claim = (
            "Silent until the response is on. Nothing is shown or spoken until "
            "the whole answer arrives."
        )
        self._announce_setting(claim if saved else _SAVE_FAILED)

    def _announce_setting(self, text: str) -> None:
        announce(text)
        self._set_status_text(text)

    def _select_session(self, index: int) -> None:
        """Show session ``index``, leaving focus on the tab strip if it is there.

        Showing a page focuses it. wxSimplebook does that from C++ on every
        selection change, unconditionally, so neither the page nor the book
        can decline it — putting focus back afterwards is the only way to
        refuse. Without that, the first arrow press along the strip drops the
        user into the prompt and the second never reaches the strip at all,
        which makes a tab strip you can enter and cannot use.
        """
        if not 0 <= index < self.notebook.GetPageCount():
            return
        if index == self.notebook.GetSelection():
            return
        keep = self._focus_is_within(wx.Window.FindFocus(), self.tab_switcher)
        # Read by `_on_tab_changed`, which runs inside SetSelection below and
        # by then can no longer see where focus started out.
        self._strip_keeps_focus = keep
        page = self.notebook.GetPage(index)
        # A disabled window cannot be given focus, which is the only way to
        # decline the book's offer. Windows answers the refused SetFocus with
        # an error wxWidgets would otherwise log, hence LogNull. Enable()
        # restores each child's own state, so a button that was disabled on
        # its own account stays that way.
        if keep:
            page.Disable()
        try:
            with wx.LogNull():
                self.notebook.SetSelection(index)
        finally:
            if keep:
                page.Enable()
            self._strip_keeps_focus = False
        if keep:
            self.tab_switcher.SetFocus()

    def _cycle_tab(self, direction: int) -> None:
        count = self.notebook.GetPageCount()
        if count <= 1:
            return
        cur = self.notebook.GetSelection()
        self._select_session((cur + direction) % count)

    def _jump_to_tab(self, idx: int) -> None:
        self._select_session(idx)

    def _new_session(self) -> None:
        """Open a session: a folder locally, a name when Hermes is elsewhere."""
        # Only the Hermes backend can run somewhere else, so only it changes the
        # question. Asked here rather than inside the dialog so the dialog stays
        # a dialog and this stays the place that knows what is selected.
        remote_label = ""
        if self._backend == BACKEND_HERMES and REMOTE_HERMES.url():
            # The address without the credential: the URL is built from a host
            # and a port here, and the key only joins it as a query parameter
            # deeper down, so splitting on "?" keeps a token out of a label a
            # screen reader will read aloud.
            remote_label = REMOTE_HERMES.url().split("?", 1)[0]
        dlg = NewSessionDialog(self, default_dir=self._projects_folder, remote_label=remote_label)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            cwd = dlg.path
            title = dlg.title_text
        finally:
            dlg.Destroy()
        if not cwd and not remote_label:
            # Locally a folder is required; remotely an empty one is normal and
            # means "wherever that Hermes runs".
            return
        if not remote_label:
            _remember_folder(cwd)
        self._add_session(cwd, session_title=title)
        if title:
            spoken = f"New session: {title}"
        elif cwd:
            spoken = f"New session: {_short_label(cwd)}"
        else:
            spoken = f"New session on {remote_label}"
        wx.CallAfter(announce, spoken)

    def _history_cwd(self) -> str:
        """The directory the history picker starts out scoped to."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            return page.cwd
        return self._projects_folder or os.getcwd()

    def _open_history(self) -> None:
        """Reopen a past conversation in a new tab (Ctrl+Shift+H)."""
        dlg = HistoryDialog(self, backend=self._backend, cwd=self._history_cwd())
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            entry = dlg.entry
            webview_entry = dlg.webview_entry
        finally:
            dlg.Destroy()
        if webview_entry is not None:
            self._add_conversation_webview(webview_entry)
            return
        if entry is None:
            return
        self._resume_history(entry)

    def _open_hermes_sessions(self) -> None:
        """List Hermes' own conversations and open one in a new tab (Ctrl+G).

        Recent Conversations answers "what is on this disk". This answers "what
        does that Hermes know", which for a Hermes on another machine is the
        only question with a useful answer: the transcripts are over there, and
        so are the conversations that are running right now.
        """
        dlg = HermesSessionsDialog(self, cwd=self._history_cwd())
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            entry = dlg.entry
            attaching = dlg.attaching
            webview_entry = dlg.webview_entry
        finally:
            dlg.Destroy()
        if webview_entry is not None:
            self._add_conversation_webview(
                webview_entry,
                missing="This conversation is not stored on this computer, so it cannot be shown here.",
            )
            return
        if not entry:
            return
        session_id = str(entry.get("id") or "")
        if not session_id:
            announce("Error: that conversation has no id to reopen")
            return
        title = str(entry.get("title") or "").strip() or str(entry.get("preview") or "").strip()
        panel = self._add_session(self._history_cwd(), open_main_chat=False)
        panel.open_hermes_session(session_id, title, attaching)
        self._follow_tab_backend()
        if attaching:
            wx.CallAfter(
                announce,
                f"Attaching to {title or session_id}. This window now receives its output; "
                "steer to send it guidance.",
            )
        else:
            wx.CallAfter(announce, f"Reopening {title or session_id}")

    def _resume_history(self, entry: HistoryEntry, on_open=None) -> None:
        """Load one past conversation without holding the GUI callback."""
        old_cancel = getattr(self, "_history_open_cancel", None)
        if old_cancel is not None:
            old_cancel.set()
        old_progress = getattr(self, "_history_open_progress", None)
        if old_progress is not None:
            old_progress.Destroy()
            self._history_open_progress = None
        serial = getattr(self, "_history_open_serial", 0) + 1
        self._history_open_serial = serial
        cancel = threading.Event()
        self._history_open_cancel = cancel
        announce(f"Loading conversation {entry.title}")
        wx.CallAfter(self._show_history_open_progress, serial, entry.title)

        def work() -> None:
            try:
                turns = load_turns(entry)
                error = ""
            except Exception as exc:
                turns, error = [], str(exc)
            _call_after_alive(
                self._history_open_loaded, serial, cancel, entry, turns, error, on_open
            )

        threading.Thread(target=work, daemon=True).start()

    def _show_history_open_progress(self, serial: int, title: str) -> None:
        if not self or serial != self._history_open_serial or self._history_open_cancel is None:
            return
        progress = wx.Dialog(self, title="Open Conversation")
        message = wx.StaticText(progress, label=f"Loading {title}")
        message.SetName("Loading conversation")
        cancel_button = wx.Button(progress, label="Cancel opening")
        cancel_button.Bind(wx.EVT_BUTTON, lambda _event: self._cancel_history_open(serial))
        self._history_open_message = message
        self._history_open_cancel_button = cancel_button
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(message, 0, wx.ALL, progress.FromDIP(PAD_DIALOG))
        sizer.Add(
            cancel_button,
            0,
            wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            progress.FromDIP(PAD_DIALOG),
        )
        progress.SetSizerAndFit(sizer)
        progress.Bind(
            wx.EVT_CHAR_HOOK,
            lambda event: (
                self._cancel_history_open(serial)
                if event.GetKeyCode() == wx.WXK_ESCAPE
                else event.Skip()
            ),
        )

        def close(event) -> None:
            self._cancel_history_open(serial)
            if self._history_open_progress is progress:
                self._history_open_progress = None
            event.Skip()

        progress.Bind(wx.EVT_CLOSE, close)
        self._history_open_progress = progress
        progress.CentreOnParent()
        progress.Show()

    def _cancel_history_open(self, serial: int) -> None:
        if not self or serial != self._history_open_serial:
            return
        cancel = self._history_open_cancel
        if cancel is not None:
            cancel.set()
        if self._history_open_message is not None:
            self._history_open_message.SetLabel(
                "Cancelling; the loaded conversation will be discarded"
            )
        if self._history_open_cancel_button is not None:
            self._history_open_cancel_button.Disable()

    def _history_open_loaded(self, serial, cancel, entry, turns, error, on_open) -> None:
        if not self or serial != self._history_open_serial:
            return
        self._history_open_cancel = None
        progress = self._history_open_progress
        self._history_open_progress = None
        self._history_open_message = None
        self._history_open_cancel_button = None
        cancelled = cancel.is_set()
        if progress is not None:
            progress.Destroy()
        if cancelled:
            announce("Opening conversation cancelled")
            return
        if error:
            announce(f"Error: Could not read {entry.title}: {error}")
            return
        if not turns:
            announce(f"Error: {entry.title} could not be read back")
            return
        if not entry.resumable:
            text = "\n\n".join(
                f"{label}: {value}"
                for turn in turns
                for label, value in (("You", turn.prompt), ("Answer", turn.response))
                if value.strip()
            )
            dialog = ReadView(self, text, f"{entry.title} — FreeBuff Desktop, read only")
            try:
                dialog.ShowModal()
            finally:
                dialog.Destroy()
            return
        cwd = self._history_cwd()
        if entry.cwd:
            candidate = entry.cwd
            if not os.path.isdir(candidate):
                # A Hermes in WSL records its own form of the path, which
                # Windows cannot open. Translate it back before giving up,
                # otherwise a resumed conversation quietly reopens in whatever
                # folder happened to be current.
                translated = wsl_path_to_windows(candidate)
                candidate = translated if os.path.isdir(translated) else ""
            if candidate:
                cwd = candidate
        panel = self._add_session(cwd, open_main_chat=False)
        # restore_history reports the conversation's name, which is what
        # renames the tab: that title is what tells this conversation apart
        # from the others open in the same folder.
        panel.restore_history(entry, turns)
        # The resumed tab took the conversation's own backend.
        self._follow_tab_backend()
        responses = "1 response" if len(turns) == 1 else f"{len(turns)} responses"
        wx.CallAfter(announce, f"Resumed {entry.title}, {responses}")
        if on_open:
            on_open(panel)

    def _find_in_all_conversations(self) -> None:
        """Search every past conversation's messages (Ctrl+Shift+S).

        Enter on a result reopens that conversation on the matching response.
        Scanning up to 1,000 transcripts is far past the GUI-thread budget, so
        the work runs on a worker and only progress and the finished list come
        back through the event loop.
        """
        with wx.TextEntryDialog(
            self, "Find in all conversations:", "Find in All Conversations",
            getattr(self, "_find_all_term", ""),
        ) as ask:  # fmt: skip
            if ask.ShowModal() != wx.ID_OK or not ask.GetValue().strip():
                return
            term = ask.GetValue().strip()
        self._find_all_term = term
        old_cancel = getattr(self, "_find_all_cancel", None)
        if old_cancel is not None:
            old_cancel.set()
        old_progress = getattr(self, "_find_all_progress", None)
        if old_progress is not None:
            old_progress.Destroy()
        serial = getattr(self, "_find_all_serial", 0) + 1
        cancel = threading.Event()
        self._find_all_serial = serial
        self._find_all_cancel = cancel
        progress = wx.ProgressDialog(
            "Find in All Conversations", f"Searching for {term}", maximum=1000,
            parent=self, style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE,
        )  # fmt: skip
        self._find_all_progress = progress

        def work() -> None:
            results: list[tuple[ConversationItem, HistoryEntry, int]] = []
            error = ""
            try:
                entries = list_history(None, None, limit=1000)
                _call_after_alive(self._find_all_range, serial, progress, max(1, len(entries)))
                rows = search_all_conversations(
                    entries,
                    term,
                    on_progress=lambda done, title: _call_after_alive(
                        self._find_all_progress_update,
                        serial,
                        progress,
                        cancel,
                        done,
                        title,
                    ),
                    is_cancelled=cancel.is_set,
                )
                results = [
                    (ConversationItem(label, whole), entry, index)
                    for label, entry, index, whole in rows
                ]
            except Exception as exc:
                error = f"Could not search conversations: {exc}"
            _call_after_alive(self._find_all_loaded, serial, progress, cancel, term, results, error)

        threading.Thread(target=work, daemon=True).start()

    def _find_all_range(self, serial: int, progress: wx.ProgressDialog, total: int) -> None:
        """Give the progress bar the real transcript count once it is known."""
        if not self or serial != getattr(self, "_find_all_serial", None):
            return
        if progress is getattr(self, "_find_all_progress", None):
            progress.SetRange(total)

    def _find_all_progress_update(
        self,
        serial: int,
        progress: wx.ProgressDialog,
        cancel: threading.Event,
        done: int,
        title: str,
    ) -> None:
        """Show one transcript's progress, and notice the user cancelling."""
        if not self or serial != getattr(self, "_find_all_serial", None):
            cancel.set()
            return
        if progress is not getattr(self, "_find_all_progress", None):
            cancel.set()
            return
        if not progress.Update(done, f"Searching {title}")[0]:
            cancel.set()

    def _find_all_loaded(
        self,
        serial: int,
        progress: wx.ProgressDialog,
        cancel: threading.Event,
        term: str,
        results: list[tuple[ConversationItem, HistoryEntry, int]],
        error: str,
    ) -> None:
        """Fill the result list, unless this search is no longer the current one."""
        if not self or serial != getattr(self, "_find_all_serial", None):
            return
        self._find_all_progress = None
        self._find_all_cancel = None
        cancelled = cancel.is_set() or progress.WasCancelled()
        progress.Destroy()
        if error:
            announce(f"Error: {error}")
            return
        if cancelled:
            announce("Search cancelled")
            return
        if not results:
            announce(f"No conversation mentions {term}")
            return
        chosen: list[tuple[HistoryEntry, int]] = []
        found = {id(item): (entry, index) for item, entry, index in results}

        def open_conversation(item: ConversationItem) -> None:
            chosen.append(found[id(item)])
            dialog.EndModal(wx.ID_OK)

        dialog = ConversationItemsDialog(
            self,
            [item for item, _entry, _index in results],
            f"{len(results)} matches for {term}",
            links=False,
            copy=_copy_to_clipboard,
            read=open_conversation,
            announce=announce,
            read_label="&Open Conversation",
        )
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        if not chosen:
            return
        entry, index = chosen[0]

        def focus_result(panel) -> None:
            for position, row in enumerate(panel._displayed):
                if row.kind == "header" and row.response_number == index + 1:
                    wx.CallAfter(panel._focus_row, position)
                    break

        self._resume_history(entry, on_open=focus_result)

    def _send_to_tab_active(self) -> None:
        """Hand this tab's response to another tab (Ctrl+Shift+O)."""
        page = self.notebook.GetCurrentPage()
        if not isinstance(page, SessionPanel):
            return
        others = [panel for panel in self._session_panels() if panel is not page]
        if not others:
            announce("Open another session first, with Ctrl+T, to send a response to it.")
            return
        text = page.response_text()
        if not text.strip():
            announce("Error: No response to send yet")
            return
        dlg = SendToTabDialog(
            self,
            [self._relay_speaker(panel) for panel in others],
            relay_message(self._relay_speaker(page), text),
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            target = others[dlg.target.GetSelection()]
            message = dlg.message.GetValue()
            relay = dlg.relay.IsChecked()
            manual = dlg.manual.IsChecked()
            turns = dlg.turns.GetValue()
            pick = dlg.third.GetSelection()
            third = others[pick - 1] if pick > 0 else None
            roles = [dlg.first_role.GetValue(), dlg.second_role.GetValue()]
            third_role = dlg.third_role.GetValue()
        finally:
            dlg.Destroy()
        if relay and third is target:
            announce("Error: The third agent has to be a different session.")
            return
        tabs = (page, target, third) if relay and third else (page, target)
        if relay:
            self._end_relay("")
            roles.append(third_role)
            relay_state: dict = {
                "tabs": tabs,
                "left": turns,
                "count": 0,
                "last": text,
                "roles": {id(panel): role for panel, role in zip(tabs, roles, strict=False)},
                "told": set(),
                "manual": manual,
                "waiting": None,
            }
            message = self._relay_preface(relay_state, target) + message
        if not target.deliver(message):
            announce("Error: That session is busy. Try again when its turn ends.")
            return
        self._select_session(self.notebook.FindPage(target))
        if relay:
            self._relay = relay_state
            for panel in tabs:
                panel.on_turn_answer = self._relay_answer
            who = f"{len(tabs)} agents" if len(tabs) > 2 else "two agents"
            wx.CallAfter(
                announce,
                f"Agent conversation between {who} started, up to {turns} hand-offs. "
                + ("Each reply waits for you to pass it on. " if manual else "")
                + "Stop in any of its sessions ends it.",
            )
        else:
            wx.CallAfter(announce, f"Sent to {self._relay_speaker(target)}")

    def _relay_speaker(self, panel: "SessionPanel") -> str:
        """'Codex (my-project)': who a relayed reply came from."""
        name = _tab_label(getattr(panel, "tab_title", ""), panel.cwd)
        return f"{backend_label(panel.selected_backend())} ({name})"

    def _relay_answer(self, panel: "SessionPanel", answer: Optional[str]) -> None:
        """One tab in the agent conversation finished a turn: pass it on, or stop."""
        relay = getattr(self, "_relay", None)
        if not relay or panel not in relay["tabs"]:
            return
        # Replies go round in order: first to second, second to the third if
        # there is one, and back to the first.
        tabs = relay["tabs"]
        other = tabs[(tabs.index(panel) + 1) % len(tabs)]
        if answer is None:
            self._end_relay("a turn was stopped or failed")
        elif not answer.strip():
            self._end_relay("a reply was empty")
        elif said_done(answer):
            self._end_relay(f"{self._relay_speaker(panel)} said DONE")
        elif relay["left"] <= 0:
            self._end_relay(f"{relay['count']} hand-offs, the limit")
        elif _flatten(answer) == _flatten(relay["last"]):
            self._end_relay("the replies started repeating")
        elif not other:
            self._end_relay("the other session was closed")
        elif relay.get("manual"):
            # The reply waits for the person: Pass On the Waiting Reply
            # shows it to them, lets them change it, and sends it. The
            # hand-off is only counted once it actually goes.
            relay["waiting"] = (panel, other, answer)
            announce(
                f"{self._relay_speaker(panel)} answered. The reply is waiting to be "
                f"passed to {self._relay_speaker(other)}: Pass On the Waiting Reply, "
                "in Conversation, Find and Browse, sends it."
            )
        elif not other.deliver(
            self._relay_preface(relay, other) + relay_message(self._relay_speaker(panel), answer)
        ):
            self._end_relay("the other session could not take the reply")
        else:
            relay["left"] -= 1
            relay["count"] += 1
            relay["last"] = answer
            announce(
                f"Hand-off {relay['count']}: {self._relay_speaker(panel)} passed to "
                f"{self._relay_speaker(other)}"
            )

    def _pass_on_waiting_reply(self) -> None:
        """Send on a reply an agent conversation is holding for approval."""
        relay = getattr(self, "_relay", None)
        if not relay or not relay.get("waiting"):
            announce("No reply is waiting to be passed on.")
            return
        panel, other, answer = relay["waiting"]
        dlg = SendToTabDialog(
            self,
            [self._relay_speaker(other)],
            relay_message(self._relay_speaker(panel), answer),
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            message = dlg.message.GetValue()
        finally:
            dlg.Destroy()
        relay["waiting"] = None
        if not other or not other.deliver(self._relay_preface(relay, other) + message):
            self._end_relay("the other session could not take the reply")
            return
        relay["left"] -= 1
        relay["count"] += 1
        relay["last"] = answer
        self._select_session(self.notebook.FindPage(other))
        announce(
            f"Hand-off {relay['count']}: {self._relay_speaker(panel)} passed to "
            f"{self._relay_speaker(other)}"
        )

    def _relay_preface(self, relay: dict, panel: "SessionPanel") -> str:
        """The setup an agent is told before its first message, once; "" after that."""
        told = relay.get("told")
        if told is None or id(panel) in told:
            return ""
        told.add(id(panel))
        others = [self._relay_speaker(tab) for tab in relay["tabs"] if tab is not panel]
        role = relay.get("roles", {}).get(id(panel), "")
        return relay_setup(self._relay_speaker(panel), others, role)

    def _end_relay(self, reason: str) -> None:
        relay = getattr(self, "_relay", None)
        self._relay = None
        if not relay:
            return
        for panel in relay["tabs"]:
            if panel:
                panel.on_turn_answer = None
        if reason:
            announce(f"Agent conversation ended: {reason}.")

    def _compact_active(self) -> None:
        """Compact the conversation in the active tab (Ctrl+Shift+K)."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.compact_conversation()

    def _configure_remote_hermes(self) -> None:
        """Point the Hermes backend at another computer, or back at this one."""
        dlg = RemoteHermesDialog(self)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            saved = dlg.apply()
        finally:
            dlg.Destroy()
        # A conversation already open belongs to whichever Hermes started it,
        # so the change applies from the next new conversation rather than
        # silently moving this one to a different machine. The cached model
        # catalog belonged to the old address and would be wrong for the new.
        invalidate_model_options(BACKEND_HERMES)
        if not saved:
            # The fields are live for this session; the file does not hold
            # them, so the description of the new connection is not said.
            announce(_SAVE_FAILED)
            return
        announce(f"Remote Hermes: {REMOTE_HERMES.describe()}. Applies to new conversations.")

    def _new_conversation_active(self) -> None:
        """Start a fresh conversation in the active tab (Ctrl+Shift+N)."""
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_new_conversation(None)
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.clear_conversation()

    def _refresh_compact_item(self) -> None:
        """Grey out Compact for a provider whose CLI has no such command."""
        item = getattr(self, "_compact_item", None)
        if item is None:
            return
        supported = self._app_mode == APP_MODE_AGENT and BACKENDS[self._backend].supports_compaction
        item.Enable(supported)
        if supported:
            item.SetHelp("Summarise this conversation so the backend has room to keep going")
        else:
            item.SetHelp(
                f"{backend_label(self._backend)} cannot compact. Start a new conversation instead."
            )

    def _set_projects_folder(self) -> Optional[str]:
        """Choose and remember the parent folder that holds the projects."""
        with wx.DirDialog(
            self,
            "Choose your Projects folder (the folder that contains your project directories)",
            defaultPath=self._projects_folder or os.path.expanduser("~"),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            path = dlg.GetPath()
        self._projects_folder = path
        cfg = _load_config()
        cfg["projects_folder"] = path
        saved = _save_config(cfg)
        wx.CallAfter(
            announce,
            f"Projects folder set to {_short_label(path)}" if saved else _SAVE_FAILED,
        )
        return path

    def _close_current_session(self) -> None:
        sel = self.notebook.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        page = self.notebook.GetPage(sel)
        if isinstance(page, ConversationWebViewPanel):
            self._close_conversation_webview(page)
            return
        if self.notebook.GetPageCount() <= 1:
            self._set_status_text("Cannot close the last session")
            return
        if isinstance(page, SessionPanel):
            # Not waited on: this is a menu handler, and the panel is about to
            # be destroyed, so there is nothing its worker can still tell us.
            page.cancel_worker(wait=False)
        self.notebook.DeletePage(sel)
        # Closing the last tab of a backend can leave them all on one again.
        self._follow_tab_backend()

    def _on_tab_changed(self, event: wx.BookCtrlEvent) -> None:
        self._agent_files_serial += 1
        event.Skip()
        self._sync_tab_switcher()
        sel = self.notebook.GetSelection()
        page = self.notebook.GetCurrentPage()
        if isinstance(page, ConversationWebViewPanel):
            self._set_status_text(page.last_status)
            if not self._strip_keeps_focus and not self._focus_is_within(
                wx.Window.FindFocus(), self.tab_switcher
            ):
                wx.CallAfter(page._announce_selected, sel, self.notebook.GetPageCount())
                wx.CallAfter(page.focus_content)
            return
        if not isinstance(page, SessionPanel) or sel == wx.NOT_FOUND:
            return
        self._set_status_text(page.last_status)
        # Before the early return below: arrowing the tab strip is exactly
        # when the tab changes, and the mode and backend belong to the tab.
        self._refresh_mode_items()
        self._follow_tab_backend()
        # Arrowing along the tab strip changes the page on every keypress. The
        # strip has to keep focus through that, or the second arrow press never
        # reaches it, and the native tab control has already said which tab is
        # selected — repeating it here would say everything twice. Focus has
        # already been taken off the strip by the time this runs, so the flag
        # `_select_session` sets is what says where the keypress came from.
        if self._strip_keeps_focus or self._focus_is_within(
            wx.Window.FindFocus(), self.tab_switcher
        ):
            return
        # Which tab of how many, its backend when tabs use more than one, then
        # its own name — the conversation, which is what tells two tabs in the
        # same folder apart — then the folder it runs in.
        name = _tab_label(getattr(page, "tab_title", ""), page.cwd)
        folder = _short_label(page.cwd)
        spoken = name if name and name != folder else folder
        where = f"Session {sel + 1} of {self.notebook.GetPageCount()}"
        if self._backends_mixed():
            where += f", {backend_label(page.selected_backend())}"
        wx.CallAfter(announce, f"{where}: {spoken}, in {folder}")
        wx.CallAfter(page.focus_prompt)

    # ----- Status routing -----
    def _set_status_text(self, text: str) -> None:
        self.statusbar.SetStatusText(text)

    def _panel_status_changed(self, panel: SessionPanel, text: str) -> None:
        # Only show the status bar message for the currently visible tab.
        if self.notebook.GetCurrentPage() is panel:
            self._set_status_text(text)

    def _panel_title_changed(self, panel: SessionPanel, title: str) -> None:
        """Name a tab after the conversation in it.

        An empty title means the conversation has no name yet, which is when
        the folder is the most useful thing the tab can say. The page is found
        by identity rather than by the current selection: a background tab can
        finish restoring, or be sent a side chat, while another one is in front.
        """
        panel.tab_title = title
        self._relabel_tabs()

    # ----- Focus delegation -----
    def _focus_active(self, which: str) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            if which == "prompt":
                self.chat_panel.message_input.SetFocus()
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, ConversationWebViewPanel):
            page.focus_content(from_navigation=True)
            return
        if not isinstance(page, SessionPanel):
            return
        if which == "prompt":
            page.focus_prompt()

    def _cycle_mode_active(self) -> None:
        if self._app_mode == APP_MODE_CHAT:
            self.mode_combo.SetFocus()
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.cycle_mode()

    def _cycle_pane(self, step: int) -> None:
        """F6 and Shift+F6: the tab strip, the responses, the prompt, the status.

        The way every Windows application with panes moves between them. The
        status bar cannot take focus, so its turn reads it aloud instead, which
        is the one place its text was otherwise only reachable by the screen
        reader's own command.
        """
        page = self.notebook.GetCurrentPage()
        if self._app_mode == APP_MODE_AGENT and isinstance(page, ConversationWebViewPanel):
            if self._focus_is_within(wx.Window.FindFocus(), self.tab_switcher):
                page.focus_content(from_navigation=True)
            else:
                self.tab_switcher.SetFocus()
            return
        if self._app_mode != APP_MODE_AGENT or not isinstance(page, SessionPanel):
            announce(self.statusbar.GetStatusText() or "Status bar is empty")
            return
        focused = wx.Window.FindFocus()

        def within(window: Optional[wx.Window]) -> bool:
            node = focused
            while node is not None:
                if node is window:
                    return True
                node = node.GetParent()
            return False

        if within(self.tab_switcher):
            current = 0
        elif within(page._responses_ctrl()) or within(page.subagents):
            current = 1
        elif within(page.prompt):
            current = 2
        else:
            current = 3 if step > 0 else 0
        if focused is not None and focused is getattr(self, "_status_stop_from", None):
            # The status bar takes no focus, so focus never left the part it
            # was read from; this press moves on from the status bar instead.
            current = 3
        self._status_stop_from = None
        target = (current + step) % 4
        if target == 0:
            self.tab_switcher.SetFocus()
        elif target == 1:
            page.focus_first_control()
        elif target == 2:
            page.focus_prompt()
        else:
            announce(f"Status: {self.statusbar.GetStatusText() or 'empty'}")
            self._status_stop_from = focused

    def _find_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_find()

    def _browse_items_active(self, kind: str) -> None:
        if self._app_mode == APP_MODE_CHAT:
            panel = self.chat_panel
            if panel is not None:
                messages = [
                    (f"{panel._history_role_label(entry.role)} {index}", entry.content)
                    for index, entry in enumerate(panel.history_entries, 1)
                    if entry.role in {"user", "assistant"}
                ]
                _show_conversation_items(panel, messages, kind)
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_items(kind)

    def _export_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.export_conversation()

    def _create_desktop_shortcut(self) -> None:
        if self._desktop_shortcut_running:
            return
        self._desktop_shortcut_running = True
        self._desktop_shortcut_serial += 1
        serial = self._desktop_shortcut_serial
        self._announce_setting("Creating desktop shortcut…")

        def work() -> None:
            try:
                link = create_desktop_shortcut()
            except (OSError, subprocess.SubprocessError) as exc:
                _call_after_alive(self._desktop_shortcut_finished, serial, None, str(exc))
                return
            _call_after_alive(self._desktop_shortcut_finished, serial, link, "")

        threading.Thread(target=work, daemon=True).start()

    def _desktop_shortcut_finished(self, serial: int, link: Optional[str], error: str) -> None:
        if not self or serial != self._desktop_shortcut_serial:
            return
        self._desktop_shortcut_running = False
        if error:
            self._announce_setting(f"The desktop shortcut could not be created: {error}")
        elif link is not None:
            self._announce_setting(f"Desktop shortcut created at {link}")

    def _stop_active(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_stop(None)
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page._on_stop()

    def _attach_active(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            self.chat_panel.on_add_files(wx.CommandEvent())
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.attach_files()

    def _jump_to_latest_response(self) -> None:
        if self._app_mode == APP_MODE_CHAT and self.chat_panel is not None:
            history = (
                self.chat_panel.history_list
                if self.chat_panel.history_view == "list"
                else self.chat_panel.transcript
            )
            history.SetFocus()
            if history is self.chat_panel.history_list and history.GetCount():
                history.SetSelection(history.GetCount() - 1)
            elif history is self.chat_panel.transcript:
                history.SetInsertionPointEnd()
            return
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.jump_to_latest_response()

    def _slash_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page._pick_slash_command()

    def _saved_prompts_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page._pick_saved_prompt()

    def _task_list_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_task_list()

    def _panel_action(self, name: str) -> None:
        """Run one of the current agent tab's list commands by name."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            getattr(page, name)()

    # ----- Cleanup -----
    def _take_links(self) -> None:
        """Open each session link a second copy left in the links folder."""
        for link in take_handed_links():
            self.open_session_link(link)

    def open_session_link(self, link: str) -> None:
        """Reopen the conversation a blindpilot:// link names. Nothing else, ever."""
        parsed = remote_sessions.parse_session_link(link)
        if parsed is None or parsed[0] not in BACKENDS:
            announce("That link isn't one BlindPilot knows.")
            return
        backend, session_id = parsed
        for panel in self._session_panels():
            if panel._session_id == session_id:
                self._select_session(self.notebook.FindPage(panel))
                announce(f"{_tab_label(panel.tab_title, panel.cwd)} is already open")
                return

        serial = getattr(self, "_linked_lookup_serial", 0) + 1
        self._linked_lookup_serial = serial

        def work() -> None:
            try:
                entries = list_history(backend, None, limit=1000)
            except Exception:
                entries = []
            entry = next((e for e in entries if e.session_id == session_id), None)
            _call_after_alive(self._open_linked, serial, entry)

        threading.Thread(target=work, daemon=True).start()

    def _open_linked(self, serial: int, entry: Optional[HistoryEntry]) -> None:
        if not self or serial != getattr(self, "_linked_lookup_serial", 0):
            return
        if entry is None:
            announce("The conversation that link names was not found on this computer.")
            return
        self._resume_history(entry)

    def watch_for_links(self) -> None:
        """Check every second for session links a second copy handed over."""
        if self._links_timer is None:
            self._links_timer = wx.Timer(self)
            self.Bind(
                wx.EVT_TIMER,
                _gui_work("Timer: refresh links", lambda _e: self._take_links()),
                self._links_timer,
            )
            self._links_timer.Start(1000)

    def watch_model_catalogs(self) -> None:
        """Re-read every tab's model catalog on the interval Preferences sets.

        Started with the window rather than at import, so a window built and
        torn down by a startup check never arms a timer that would fire during
        teardown.
        """
        self._sync_model_timer()

    def _sync_model_timer(self) -> None:
        """Arm, re-arm or disarm the catalog timer to match the setting.

        One timer is kept and restarted rather than a new one made each time,
        so no second binding is left for a timer that is no longer watched. A
        setting of 0 stops it: the refresh is then not merely slower, it is off.
        """
        minutes = SETTINGS.model_refresh_minutes
        if minutes <= 0:
            if self._model_timer is not None:
                self._model_timer.Stop()
                self._model_timer = None
            return
        if self._model_timer is None:
            self._model_timer = wx.Timer(self)
            self.Bind(
                wx.EVT_TIMER,
                _gui_work(
                    "Timer: refresh model catalogs", lambda _e: self._refresh_model_catalogs()
                ),
                self._model_timer,
            )
        self._model_timer.Start(minutes * 60 * 1000)

    def _refresh_model_catalogs(self) -> None:
        """Ask each open tab to re-read its catalog; a fresh one is a no-op."""
        for page in self._session_panels():
            refresh = getattr(page, "refresh_models_in_background", None)
            if refresh is not None:
                refresh()

    def _on_close(self, event: wx.CloseEvent) -> None:
        notebook = getattr(self, "notebook", None)
        if notebook is not None:
            for index in range(notebook.GetPageCount()):
                page = notebook.GetPage(index)
                if isinstance(page, ConversationWebViewPanel):
                    page.mark_closed()
        self._agent_files_serial = getattr(self, "_agent_files_serial", 0) + 1
        self._desktop_shortcut_serial = getattr(self, "_desktop_shortcut_serial", 0) + 1
        timer = getattr(self, "_links_timer", None)
        if timer is not None:
            timer.Stop()
            self._links_timer = None
        timer = getattr(self, "_model_timer", None)
        if timer is not None:
            timer.Stop()
            self._model_timer = None
        if self.chat_panel is not None:
            self.chat_panel.shutdown()
        # Forget each tab's held process before the window closes. The waits
        # belong to the shutdown thread, after the UI has returned to wx.
        cancelling = []
        for page in self._session_panels():
            thread = page.cancel_worker(wait=False)
            if thread is not None:
                cancelling.append(thread)

        def finish_shutdown() -> None:
            deadline = time.monotonic() + _CANCEL_JOIN_SECONDS
            for thread in cancelling:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            # A shared app-server belongs to no single tab. This also waits
            # for pending per-tab drops without blocking the close callback.
            backend_pool.stop_all_held_processes()

        threading.Thread(target=finish_shutdown, name="backend-shutdown").start()
        # Closing this window is meant to end the process, and wx ends the
        # event loop only when the last top-level window is destroyed. Any
        # other one left standing — a dialog parented to nothing, a window a
        # platform component kept — leaves BlindPilot running in the
        # background with no window to show for it, which is the report this
        # answers. Quitting has no use for any of them, so quitting takes
        # them all down, and asks the loop to end outright as well rather
        # than trusting the window count alone.
        _destroy_other_top_level_windows(self)
        event.Skip()
        app = wx.GetApp()
        if app is not None:
            wx.CallAfter(app.ExitMainLoop)


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
