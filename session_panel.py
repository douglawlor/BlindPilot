"""One session in one tab: prompt, responses, and everything between.

Extracted from ``blindpilot_app.py`` as the last large piece of the
monolith, so the panel's behaviour can be changed and reviewed
independently. MainFrame builds these and owns the window around
them. Assistive-technology verification of the panel is covered by
the automated behaviour tests; per issue #136's agreed exception,
no real screen-reader verification is represented as completed for
this extraction.

Names the panel calls are patched here, where the calls are made
from -- for example ``session_panel.probe_model_options``.
_STARTUP_CHECK is this module's own copy of the startup-smoke flag;
main() sets it here as well as blindpilot_app's, because the two
modules read it independently.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import weakref
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from collections.abc import Callable, Sequence

import wx

import backend_pool
import remote_sessions
import screen_speech
from screen_speech import announce
from agent_backends import (
    BACKENDS,
    opencode_commands,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_COMMANDCODE,
    BACKEND_FREEBUFF,
    BACKEND_HERMES,
    BACKEND_MUSE,
    BACKEND_MUSEAI,
    BACKEND_OPENCODE,
    SUBAGENT_RUNNING,
    SUBAGENT_STOPPED,
    UNBOUND,
    AgentWorker,
    backend_label,
    backend_status,
    codex_plan_account,
    compaction_request,
    discard_freebuff_prewarm,
    normalize_backend,
    prewarm_freebuff,
    worker_class,
)
from app_config import (
    NARRATION_KEEP_UP,
    REMOTE_HERMES,
    SETTINGS,
    _ALWAYS_SPOKEN,
    _load_config,
    _save_config,
)
from bug_report_dialog import _copy_to_clipboard
from claude_worker import ClaudeWorker
from cli_install import _QUEUEING_BACKENDS
from connect_dialog import ConnectDialog
from conversation_items import ConversationItem, ConversationItemsDialog, collect_items
from conversation_list import make_conversation_list
from conversation_webview import Turn
from dialog_tasks import _call_after_alive
from history_dialog import _save_or_announce
from markdown_rows import (
    Row,
    _last_block_start,
    _one_line,
    _row_at,
    _search_key,
    _starts_of,
    _strip_noise,
    markdown_page,
    parse_response,
    reassemble,
    reassemble_all,
)
from muse_backend import muse_skills
from model_dialog import (
    AUTO_MODEL,
    ModelOptions,
    ModelDialog,
    cached_model_options,
    invalidate_model_options,
    probe_model_options,
)
from permission_dialog import PermissionDialog
from process_utils import _git
from question_dialog import QuestionDialog
from questions import (
    Question,
    is_an_offer_to_carry_on,
    questions_written_in_the_answer,
    trailing_question,
)
from read_view import ReadView
from saved_prompts_dialog import SavedPromptsDialog, _saved_prompts, _store_saved_prompts
from session_history import HistoryEntry, HistoryTurn, load_turns, make_title, restore_tasks
from slash_command_dialog import SlashCommandDialog
from spoken_text import _context_line, _result_label, _sent_readback, _spoken_duration
from subagent_dialog import SubagentDialog, SubagentEntry
from task_list import TaskList
from view_dialogs import FormattedView

if TYPE_CHECKING:
    from blindpilot_app import MainFrame
    from earcons import Earcons

# The mode the frame starts in, matching blindpilot_app.APP_MODE_AGENT.
# ----- GUI-thread budget -----
#
# Nothing may block the GUI thread. That rule has been in this project's
# reviews for a while and nothing enforced it, so a handler that reads a
# provider CLI or waits on a process still runs to completion before the
# window repaints, and the keystrokes behind it are lost.
#
# A shortcut handler is expected to return inside this budget. Work that can
# exceed it belongs on a worker, with the handler returning at once and the
# answer filled in when it arrives. `_gui_work` measures what each handler
# actually spends and records anything over the line, so the offending handler
# is named rather than guessed at.
GUI_BUDGET_MS = 50.0

_over_budget_work: deque[tuple[str, float]] = deque(maxlen=200)


def over_budget_work() -> list[tuple[str, float]]:
    """(name, milliseconds) for every GUI-thread callback that ran long.

    Newest last, and a copy, so nothing a caller does can disturb what the
    window recorded.
    """
    return list(_over_budget_work)


# Slash commands the user can pick from the slash-command picker. Commands
# marked [BlindPilot] are handled by the frontend; the rest are provider-only.
_BLINDPILOT_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/btw [message]", "Open a side-chat tab in this directory [BlindPilot]"),
    ("/clear", "Start a fresh conversation in this tab [BlindPilot]"),
    ("/compact", "Summarise this conversation to free up context [BlindPilot]"),
    ("/exit", "Close this session tab [BlindPilot]"),
    ("/model", "Pick the model and effort level in a dialog [BlindPilot]"),
    ("/models", "Refresh and pick a model in a dialog [BlindPilot]"),
    ("/model [model-id]", "Switch straight to a model [BlindPilot]"),
    ("/resume", "Reopen a past conversation in a new tab [BlindPilot]"),
    ("/status", "Show the backend, model, and account this tab uses [BlindPilot]"),
]

_CLAUDE_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/compact [instructions]", "Compact with custom summary instructions"),
    ("/cost", "Show token usage and cost for this session"),
    ("/init", "Create or update CLAUDE.md in the current directory"),
    ("/login", "Switch Claude account or re-authenticate"),
    ("/logout", "Sign out of Claude"),
    ("/memory", "Open memory files in the editor"),
    ("/pr_comments", "View pull request comments"),
    ("/release-notes", "Show Claude Code release notes"),
    ("/review", "Review a file or directory"),
]

# opencode's own commands are not a fixed list: a project can define its own,
# and BlindPilot reads whichever ones this directory has. Only /connect is
# always there, because that one is BlindPilot's.
_OPENCODE_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/connect", "Connect a provider to opencode, or disconnect one [BlindPilot]"),
]

# Hermes' own commands. Curated rather than complete: it ships about 120, and
# the ones left out are terminal drawing and input controls (/redraw, /mouse,
# /density, the status-bar toggles) that do nothing in a window read by a
# screen reader, plus the aliases of commands already listed under their real
# name. The picker is a discovery aid, not the limit -- the Hermes worker asks
# Hermes itself what it recognises, so a command missing from this list, a
# skill, a bundle, or one a plugin adds can still be typed and will run.
_HERMES_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/agents", "Show active agents and running tasks"),
    ("/approvals", "Show or set the persistent dangerous-command approval mode"),
    ("/bg", "Run a prompt in a separate background session"),
    ("/blueprint", "Set up an automation from a blueprint template"),
    ("/branch", "Branch the current session (explore a different path)"),
    (
        "/browser",
        "Connect browser tools to your live Chromium-family browser via CDP, or switch to Browser Use mode",
    ),
    ("/bundles", "List skill bundles (aliases /<name> for multiple skills)"),
    ("/codex-runtime", "Toggle codex app-server runtime for OpenAI/Codex models"),
    ("/config", "Show current configuration"),
    (
        "/context",
        "Show detailed context window view with usage gauge, category breakdown, compression stats, and throughput",
    ),
    ("/cron", "Manage scheduled tasks"),
    ("/curator", "Background skill maintenance (status, run, pin, archive, list-archived)"),
    ("/debug", "Upload debug report (system info + logs) and get shareable links"),
    ("/diff", "Show git changes in the working directory"),
    ("/egress", "Show Docker egress proxy status"),
    ("/export", "Export a profile (config, skills, theme) to a shareable archive"),
    (
        "/fast",
        "Fast mode. OpenAI Priority Processing or Anthropic Fast Mode (normal/fast/auto/cold)",
    ),
    ("/gateway", "Show gateway/messaging platform status"),
    ("/goal", "Set a standing goal Hermes works on across turns until achieved"),
    ("/handoff", "Hand off this session to a messaging platform (Telegram, Discord, etc.)"),
    ("/help", "Show available commands (/help skills lists skill commands, /help <text> filters)"),
    ("/history", "Show conversation history"),
    ("/import", "Import a shared profile archive as a new profile"),
    ("/init", "Generate or update AGENTS.md project instructions from a repo scan"),
    ("/insights", "Show usage insights and analytics"),
    ("/journey", "Open the learning journey timeline"),
    ("/kanban", "Multi-profile collaboration board (tasks, links, comments)"),
    ("/learn", "Learn a reusable skill from anything you describe (dirs, URLs, this chat, notes)"),
    ("/logs", "Show recent gateway log lines"),
    ("/loop", "Re-run a prompt on a recurring interval in this session"),
    ("/memory", "Review pending memory writes / toggle the approval gate"),
    (
        "/moa",
        "Run one prompt through the default Mixture of Agents preset, then restore your model",
    ),
    ("/personality", "Set a predefined personality"),
    ("/plan", "Write a markdown implementation plan to .hermes/plans/ without executing anything"),
    ("/platforms", "Show gateway/messaging platform status"),
    ("/plugins", "List installed plugins and their status"),
    ("/profile", "Show active profile name and home directory"),
    ("/reasoning", "Manage reasoning effort and display"),
    ("/refine", "Review this conversation now and save lessons to memory/skills"),
    ("/reload-mcp", "Reload MCP servers from config"),
    ("/reload-skills", "Re-scan ~/.hermes/skills/ for newly installed or removed skills"),
    ("/retry", "Retry the last message (resend to agent)"),
    ("/review", "Spawn an independent subagent to review the work just discussed (PR, code, docs)"),
    (
        "/rollback",
        "List or restore filesystem checkpoints (restores keep your hand-edits; --all overrides)",
    ),
    ("/save", "Export the current conversation (bare /save shows usage)"),
    ("/sessions", "Browse and resume previous sessions"),
    ("/skills", "Search, install, inspect, or manage skills"),
    ("/snapshot", "Create or restore state snapshots of Hermes config/state"),
    ("/steer", "Inject a message after the next tool call without interrupting"),
    ("/stop", "Kill all running background processes"),
    ("/subscription", "View your Nous plan and change it in the browser"),
    ("/title", "Set a title for the current session"),
    ("/tools", "Manage tools: /tools [list|disable|enable] [name...]"),
    ("/toolsets", "List available toolsets"),
    ("/topup", "Show your Nous balance and manage billing on the portal"),
    ("/undo", "Back up N user turns and re-prompt (default 1)"),
    ("/update", "Update Hermes Agent to the latest version"),
    ("/usage", "Show token usage and rate limits; `reset` redeems a banked Codex limit reset"),
    ("/version", "Show Hermes Agent version"),
    ("/whoami", "Show your slash command access (admin / user)"),
    ("/worktree", "Show, list, create, or prune isolated git worktrees"),
    ("/yolo", "Toggle YOLO mode (skip all dangerous command approvals)"),
]

_FREEBUFF_SLASH_COMMANDS: list[tuple[str, str]] = [
    (
        "/queue [message|list|clear|resume]",
        "Queue a follow-up, inspect, clear, or resume the queue [BlindPilot]",
    ),
    ("/new", "Start a new FreeBuff conversation [BlindPilot]"),
    ("/history", "Open FreeBuff conversation history [BlindPilot]"),
    ("/diagnostics", "Show FreeBuff's resource usage and tool processes"),
    ("/init", "Create project instructions"),
    ("/usage", "Show FreeBuff credit usage [BlindPilot]"),
    ("/review", "Review the current changes"),
    ("/plan", "Plan before making changes"),
    ("/theme:toggle", "Toggle FreeBuff's terminal theme"),
    ("/logout", "Sign out of FreeBuff"),
]

# These have actual frontend or prompt equivalents; terminal-only commands
# are explained locally instead of accidentally sent to the model as text.
_COMMANDCODE_SLASH_COMMANDS: list[tuple[str, str]] = [
    (
        "/queue [message|list|clear|resume]",
        "Queue a follow-up, inspect, clear, or resume the queue [BlindPilot]",
    ),
    ("/steer [message]", "Stop this turn and resume with a new instruction [BlindPilot]"),
    ("/stop", "Stop this turn and pause queued messages [BlindPilot]"),
    ("/help", "Show commands and how queuing and steering work [BlindPilot]"),
    ("/new", "Start a fresh conversation [BlindPilot]"),
    ("/sessions", "Reopen a past conversation [BlindPilot]"),
    ("/quit", "Close this session tab [BlindPilot]"),
    (
        "/add-dir [directory]",
        "Add context for subsequent messages; bare command lists directories [BlindPilot]",
    ),
    ("/config", "Open backend settings files [BlindPilot]"),
    ("/connect", "Open backend sign-in and setup [BlindPilot]"),
    ("/effort [level]", "Show or set reasoning effort [BlindPilot]"),
    ("/init", "Ask the agent to create project instructions [BlindPilot]"),
    ("/login", "Open backend sign-in and setup [BlindPilot]"),
    ("/mode [name]", "Show or switch permission mode [BlindPilot]"),
    ("/plan [task]", "Enter plan mode, optionally sending a planning task [BlindPilot]"),
    ("/pr-comments", "Ask the agent to fetch comments for this branch's PR [BlindPilot]"),
    ("/review [pr]", "Ask the agent to review changes or a pull request [BlindPilot]"),
    ("/copy", "Copy the last response [BlindPilot]"),
    ("/update", "Open backend management to update Command Code [BlindPilot]"),
]

_SLASH_COMMANDS_REFRESH = "Refresh the command list (checks the backend)"

_slash_commands_cache: dict[tuple[str, str], list[tuple[str, str]]] = {}


def _slash_commands_cache_key(backend: str, cwd: str | None) -> tuple[str, str]:
    """The same backend and folder spelled differently still share their warm list."""
    folder = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    return normalize_backend(backend), folder


_changed_files_cache: dict[str, tuple[str, list[tuple[str, str, bool]]]] = {}


def _changed_files_cache_key(cwd: str) -> str:
    """The same folder spelled differently still shares its warm listing."""
    return os.path.normcase(os.path.abspath(cwd))


# Lines of a unified diff that describe the files rather than change them.
_DIFF_HEADERS = (
    "diff --git",
    "index ",
    "--- ",
    "+++ ",
    "\\",
    "new file mode",
    "deleted file mode",
    "similarity ",
    "rename ",
    "old mode",
    "new mode",
)

# BlindPilot's provider-neutral permission choices. Adapters translate these
# values to each backend's native approval and sandbox controls.
PERMISSION_MODES = [
    (
        "default",
        "Default",
        "Default mode. The selected backend uses its normal approval policy.",
    ),
    (
        "acceptEdits",
        "Accept edits",
        "Accept edits mode. File edits are accepted, while other actions keep "
        "the backend's normal safeguards.",
    ),
    (
        "plan",
        "Plan",
        "Plan mode. The backend can read and explore, but cannot edit your code.",
    ),
    (
        "auto",
        "Auto",
        "Auto mode. The backend works inside its workspace sandbox without "
        "stopping for routine approvals. In Codex, its automatic approval review "
        "judges anything outside the sandbox; in GitHub Copilot CLI, its assisted "
        "approval does.",
    ),
    (
        "dontAsk",
        "Don't ask",
        "Don't ask mode. Approval prompts are declined instead of interrupting the run.",
    ),
    (
        "bypassPermissions",
        "Bypass permissions",
        "Bypass permissions mode. The backend runs without approval or sandbox "
        "checks. Use only in an isolated environment.",
    ),
]

# BlindPilot runs its backends hands-off: a run that stops to ask a question
# nobody is watching for is a run that never finishes. "bypassPermissions" is
# what "never stop to ask" means to every provider that has such a mode, so it
# is where a new tab starts and where the quick-cycle chord returns to.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# How long the application waits, in total, for the turns still running when it
# quits. They are cancelled at the same time and share this, rather than each
# being given the whole of it.
_CANCEL_JOIN_SECONDS = 3.0

APP_MODE_AGENT = "agent"
# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG.
PAD = 8
PAD_DIALOG = 12
# Set by main() during a startup smoke check, beside blindpilot_app's copy.
_STARTUP_CHECK = False

# Shown first in both combo boxes: leave the flag off and let the CLI decide.
# Probing costs a CLI start-up, so results are reused for a while. Catalogs are
# deliberately loaded only when /model or /models is opened, keeping normal
# application startup fast and quiet.
PROBE_TTL_SECONDS = 900


# Said for an agent that was running when its turn ended without the backend
# reporting how it finished.
SUBAGENT_ENDED = "ended"


# What a subagent the list shows is doing: its name, where it stands, and its
# log. Kept to the newest lines, because a subagent that runs for an hour can
# say far more than anyone will read back.
SUBAGENT_LOG_LINES = 2000


# The quick-cycle chord steps through the everyday subset; the rest stay
# reachable via the dropdown.
_CYCLE_VALUES = [DEFAULT_PERMISSION_MODE, "acceptEdits", "plan"]


# How long the prompt has to stop changing before dictated or pasted text is
# read back. Long enough that the pauses inside one utterance do not split
# it, short enough to be a read-back rather than an interruption.
_DICTATION_PAUSE_MS = 1500


# File extension to suggest when saving a code row, keyed by its display name.
_LANG_EXT = {
    "Python": ".py",
    "JavaScript": ".js",
    "TypeScript": ".ts",
    "Shell": ".sh",
    "Bash": ".sh",
    "Zsh": ".sh",
    "JSON": ".json",
    "YAML": ".yaml",
    "HTML": ".html",
    "CSS": ".css",
    "SQL": ".sql",
    "C": ".c",
    "C++": ".cpp",
    "C#": ".cs",
    "Go": ".go",
    "Rust": ".rs",
    "Java": ".java",
    "Ruby": ".rb",
    "PHP": ".php",
    "Swift": ".swift",
    "Kotlin": ".kt",
    "Markdown": ".md",
    "XML": ".xml",
    "TOML": ".toml",
    "Diff": ".diff",
    "Plain text": ".txt",
}


_MODE_DESCRIPTIONS = {value: desc for value, _l, desc in PERMISSION_MODES}


_MODE_LABELS = [label for _v, label, _d in PERMISSION_MODES]


_MODE_LABEL_BY_VALUE = {value: label for value, label, _d in PERMISSION_MODES}


_MODE_VALUES = [value for value, _l, _d in PERMISSION_MODES]


# Streamed coding-agent output can arrive much faster than a native list and a
# screen reader can consume it. Process a bounded number of events per GUI turn
# so keyboard and accessibility events always get a chance to run, and redraw
# the responses control only once for each batch.
_WORKER_EVENT_BATCH_SIZE = 16


_WORKER_EVENT_BUDGET_SECONDS = 0.02


def _claude_settings_rule_lines(cwd: str) -> list[str]:
    """The permission rules in Claude Code's own settings files, with sources.

    Your settings, then the working folder's shared and local settings --
    the files Claude Code reads for a session in this folder. Read only:
    BlindPilot never writes to them. A file that cannot be read or parsed
    is skipped rather than guessed at.
    """
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = [home / "settings.json"]
    if cwd:
        files += [
            Path(cwd) / ".claude" / "settings.json",
            Path(cwd) / ".claude" / "settings.local.json",
        ]
    lines: list[str] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        permissions = data.get("permissions") if isinstance(data, dict) else None
        if not isinstance(permissions, dict):
            continue
        for key, verb in (("allow", "Allow"), ("ask", "Ask about"), ("deny", "Never allow")):
            for rule in permissions.get(key) or []:
                lines.append(f"{verb}: {rule} (from {path})")
        for folder in permissions.get("additionalDirectories") or []:
            lines.append(f"Work in the folder {folder} (from {path})")
    return lines


def _default_permission_mode(cwd: str, backend: str = BACKEND_CLAUDE) -> str:
    """The mode a new session tab starts in.

    Your last choice in this app wins, because it was made deliberately.
    Failing that every backend starts fully automatic: BlindPilot is driven by
    ear, and a backend that stops mid-run to ask permission stops a run its
    user cannot see is waiting. ``cwd`` and ``backend`` do not change the
    answer yet.
    """
    saved = _load_config().get("permission_mode")
    if isinstance(saved, str) and saved in _MODE_VALUES:
        return saved
    return DEFAULT_PERMISSION_MODE


def _flatten(text: str) -> str:
    """Reduce to letters and digits, for comparing two copies of one answer.

    Backends assemble their final text from the same pieces they streamed, but
    not always with the same joins, and one that streams from a rendered
    terminal streams the text without its Markdown, so neither the whitespace
    nor the punctuation can be relied on to match.
    """
    return "".join(character.casefold() for character in text if character.isalnum())


def _forget_remote(session_id: str) -> None:
    cfg = _load_config()
    saved = _remote_sessions()
    if saved.pop(session_id, None) is not None:
        cfg["remote_control"] = saved
        _save_config(cfg)


def _gui_work(name: str, action: Callable[..., object]) -> Callable[..., None]:
    """Wrap a GUI-thread callback so what it spends is measured and recorded.

    Event and CallAfter arguments pass through unchanged. Over-budget work is
    logged at DEBUG and filed in `over_budget_work`.
    """
    logger = logging.getLogger("blindpilot.timing")

    def run(*args: object, **kwargs: object) -> None:
        started = time.perf_counter()
        try:
            action(*args, **kwargs)
        finally:
            spent = (time.perf_counter() - started) * 1000.0
            if spent > GUI_BUDGET_MS:
                _over_budget_work.append((name, spent))
                logger.debug("%s spent %.0f ms on the GUI thread", name, spent)

    return run


def _notify_if_away(panel: SessionPanel, message: str) -> None:
    """A system notification, only while another application has focus.

    A turn that ends while you are in your editor or browser was announced
    to a window nobody is looking at. Choosing the notification brings
    BlindPilot forward on this tab.
    """
    app = wx.GetApp()
    if not SETTINGS.notify_in_background or app is None or cast(wx.App, app).IsActive():
        return
    title = _tab_label(getattr(panel, "tab_title", ""), getattr(panel, "cwd", ""))
    note = wx.adv.NotificationMessage(f"BlindPilot: {title}", message, panel)
    # Every one is held until it is chosen or dismissed: dropping the wrapper
    # drops its click handler, and choosing it would then do nothing.
    held = getattr(panel, "_notifications", None)
    if held is None:
        held = panel._notifications = []
    held.append(note)

    def done(chosen: bool) -> None:
        if note in held:
            held.remove(note)
        if chosen:
            panel._come_forward()

    # wxPython names the event types but ships no binders for them.
    for event_type, chosen in (
        (wx.adv.wxEVT_NOTIFICATION_MESSAGE_CLICK, True),
        (wx.adv.wxEVT_NOTIFICATION_MESSAGE_DISMISSED, False),
    ):

        def on_notification(_event: wx.Event, selected: bool = chosen) -> None:
            done(selected)

        note.Bind(wx.PyEventBinder(event_type), on_notification)
    note.Show()


def _permission_update_lines(suggestions: list) -> list[str]:
    """What a batch of permission updates grants, as lines to read back.

    Claude Code suggests the updates when it asks; "Allow for this session"
    forwards them with their destination rewritten to the session. The
    shapes are Claude Code's own: addRules/replaceRules carry rules with a
    tool name and an optional rule content, addDirectories carries folders,
    setMode carries a mode.
    """
    lines: list[str] = []
    for update in suggestions:
        if not isinstance(update, dict):
            continue
        kind = update.get("type")
        if kind in ("addRules", "replaceRules"):
            behavior = str(update.get("behavior") or "allow")
            verb = {"allow": "Allow", "ask": "Ask about", "deny": "Never allow"}.get(
                behavior, behavior.capitalize()
            )
            for rule in update.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                tool = str(rule.get("toolName") or "a tool")
                content = str(rule.get("ruleContent") or "")
                lines.append(f"{verb}: {tool}({content})" if content else f"{verb}: {tool}")
        elif kind == "addDirectories":
            for folder in update.get("directories") or []:
                lines.append(f"Work in the folder {folder}")
        elif kind == "setMode":
            lines.append(f"Permission mode set to {update.get('mode')}")
    return lines


def _remember_permission_mode(value: str) -> None:
    """Persist a mode change so it survives restarts and new tabs."""
    if value not in _MODE_VALUES:
        return
    cfg = _load_config()
    if cfg.get("permission_mode") == value:
        return
    cfg["permission_mode"] = value
    _save_or_announce(cfg)


def _remember_remote(session_id: str, bridge: str, url: str) -> None:
    cfg = _load_config()
    cfg["remote_control"] = {**_remote_sessions(), session_id: {"bridge": bridge, "url": url}}
    _save_config(cfg)


def _remember_session_rules(session_id: str, new_lines: list[str]) -> None:
    """Keep what "Allow for this session" granted, so it can be listed later.

    The grants themselves live in the backend's own session and are never
    written to a settings file; this record is only so Chat Permissions can
    say what was allowed, including after the conversation is reopened.
    """
    if not session_id or not new_lines:
        return
    saved = _saved_session_rules()
    lines = list(saved.get(session_id) or [])
    for line in new_lines:
        if line not in lines:
            lines.append(line)
    cfg = _load_config()
    cfg["session_permissions"] = {**saved, session_id: lines[-200:]}
    _save_config(cfg)


def _remote_sessions() -> dict:
    """Claude Code conversations with Remote Control on: session id -> bridge and url."""
    saved = _load_config().get("remote_control")
    return dict(saved) if isinstance(saved, dict) else {}


def _saved_session_rules() -> dict:
    """Rules each conversation has been allowed for its session, by id."""
    saved = _load_config().get("session_permissions")
    return saved if isinstance(saved, dict) else {}


def _show_conversation_items(parent: wx.Window, messages: list[tuple[str, str]], kind: str) -> None:
    title = "Links" if kind == "links" else "Code Blocks"
    items = collect_items(messages, kind)
    if not items:
        announce(f"No {title.lower()} in this conversation or response")
        return
    focused = wx.Window.FindFocus()

    def read(item: ConversationItem) -> None:
        viewer = ReadView(dialog, item.payload, item.label, monospace=kind == "code")
        try:
            viewer.ShowModal()
        finally:
            viewer.Destroy()

    dialog = ConversationItemsDialog(
        parent,
        items,
        title,
        links=kind == "links",
        copy=_copy_to_clipboard,
        read=read,
        announce=announce,
    )
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()
        if focused and not focused.IsBeingDeleted():
            focused.SetFocus()


def _slash_command_labels(commands: list[tuple[str, str]]) -> list[str]:
    return [f"{cmd}. {desc}" for cmd, desc in commands] + [_SLASH_COMMANDS_REFRESH]


def _slash_commands_for_backend(backend: str, cwd: str | None = None) -> list[tuple[str, str]]:
    """The commands this backend offers, including the ones it is asked for."""
    commands = _static_slash_commands_for_backend(backend)
    backend = normalize_backend(backend)
    if backend == BACKEND_OPENCODE:
        # Whatever this directory's opencode actually offers, which is its two
        # built-in commands plus any the project defines for itself.
        commands.extend(
            (f"/{name}", description or f"Run opencode's {name} command")
            for name, description in opencode_commands(cwd)
        )
    elif backend == BACKEND_COMMANDCODE:
        # Then whatever Command Code itself offers: its agent's commands
        # (skills, mods, /loop), sent as typed, and its console's built-ins,
        # run in its console off screen. Cached; the first look in a folder
        # fills the agent's list in the background.
        from commandcode_backend import commandcode_agent_commands, commandcode_console_commands

        have = {name.split()[0].lower() for name, _ in commands}
        for name, description in commandcode_agent_commands(cwd):
            if name.lower() not in have:
                have.add(name.lower())
                commands.append((name, description or f"Run Command Code's {name[1:]}"))
        for name, description in commandcode_console_commands():
            if name.lower() not in have:
                have.add(name.lower())
                commands.append((name, f"{description} [Command Code console]"))
    elif backend == BACKEND_MUSE:
        # Whatever this directory's Muse offers: its bundled, user, and
        # project skills, invoked the way the TUI invokes them. A skill the
        # CLI does not list can still be typed, but the worker only treats a
        # leading /token as a call when it is one of these.
        commands.extend(
            (f"/{name}", description or f"Run Muse's {name} skill")
            for name, description in muse_skills(cwd)
        )
    return commands


def _static_slash_commands_for_backend(backend: str) -> list[tuple[str, str]]:
    """The commands that need no backend probe, and so can be shown at once."""
    commands = list(_BLINDPILOT_SLASH_COMMANDS)
    backend = normalize_backend(backend)
    if backend == BACKEND_CLAUDE:
        commands.extend(_CLAUDE_SLASH_COMMANDS)
    elif backend == BACKEND_FREEBUFF:
        commands.extend(_FREEBUFF_SLASH_COMMANDS)
    elif backend == BACKEND_HERMES:
        commands.extend(_HERMES_SLASH_COMMANDS)
    elif backend == BACKEND_OPENCODE:
        commands.extend(_OPENCODE_SLASH_COMMANDS)
    elif backend == BACKEND_COMMANDCODE:
        commands.extend(_COMMANDCODE_SLASH_COMMANDS)
    return commands


def _tab_label(title: str, cwd: str) -> str:
    """What a tab is called.

    The conversation in it, which is the one thing that tells two tabs in the
    same folder apart. A conversation has no name until its first message, so
    until then the folder is the most useful thing the tab can say.

    A remote session may have NEITHER: no name was typed and the folder belongs
    to another machine, so ``cwd`` is empty. Both parts empty used to leave the
    tab labelled with nothing at all, which is the one label a screen reader
    cannot tell from its neighbour -- hence the placeholder, replaced by the
    real name as soon as Hermes titles the conversation.
    """
    return _tab_title(title) or (_short_label(cwd) if cwd else "New session")


def cached_changed_files(cwd: str) -> tuple[str, list[tuple[str, str, bool]]] | None:
    """A folder's warm changed-file listing, or None when nothing is warm.

    Only a listing with entries is kept: an empty answer and "not a repository"
    both have to be asked again, so a folder that has just gained its first
    change is never hidden behind a remembered empty list.
    """
    return _changed_files_cache.get(_changed_files_cache_key(cwd))


def cached_slash_commands(backend: str, cwd: str | None) -> list[tuple[str, str]] | None:
    """The warm command list for this backend and folder, or None."""
    return _slash_commands_cache.get(_slash_commands_cache_key(backend, cwd))


def changed_files(cwd: str) -> tuple[str, list[tuple[str, str, bool]]] | None:
    """What differs from the last commit in `cwd`'s repository, or None.

    Returns the repository root and (path, spoken description, is new)
    triples, paths relative to that root. Read from git rather than from the transcript, so it
    is the same for every backend and counts what a subagent or a shell
    command changed too.
    """
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode:
        return None
    root = top.stdout.strip()
    files: list[tuple[str, str]] = []
    # -z: paths exactly as they are, not C-quoted, and a rename as old and new.
    fields = iter(_git(root, "diff", "--numstat", "-z", "HEAD").stdout.split("\0"))
    for entry in fields:
        parts = entry.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path:
            next(fields, "")  # the old name
            path = next(fields, "")
        if added == "-":
            files.append((path, "binary file changed"))
        else:
            lines = "line" if added == "1" else "lines"
            files.append((path, f"{added} {lines} added, {removed} removed"))
    listed = _git(root, "ls-files", "-z", "--others", "--exclude-standard").stdout
    new = [path for path in listed.split("\0") if path]
    files += [(path, "new file") for path in new]
    described = []
    for path, what in files:
        folder = os.path.dirname(path)
        where = f", in {folder}" if folder else ""
        label = f"{os.path.basename(path)}, {what}{where}"
        described.append((path, label, path in new))
    return root, described


def forget_changed_files(cwd: str) -> None:
    """Drop a folder's warm listing, so the next open asks Git again."""
    _changed_files_cache.pop(_changed_files_cache_key(cwd), None)


def forget_slash_commands(backend: str, cwd: str | None) -> None:
    """Drop a warm list, so Refresh asks the backend again."""
    _slash_commands_cache.pop(_slash_commands_cache_key(backend, cwd), None)


def question_left_in_the_answer(text: str, *, asked_properly: bool, replaying: bool) -> str:
    """The question a finished turn ended on without ever asking it properly.

    A question the backend put through its own question tool has already been
    announced and answered in a dialog, and the answer that follows usually
    repeats it back — so a turn that asked properly is left alone, whatever
    its text then reads like.

    A replay is the transcript of a conversation being reopened. The question
    in it was asked, and answered, whenever it was first asked.

    A turn that finished the work and signed off by offering the next step --
    "Want me to run the tests too?", "Anything else?" -- is not waiting on an
    answer either, and it is how most turns end on most backends. Opening a
    modal over every one of those is what this asks about last, because the
    dialog is for the turn that stopped and cannot go on until it is told
    something.
    """
    if asked_properly or replaying:
        return ""
    question = trailing_question(text)
    if not question or is_an_offer_to_carry_on(question):
        return ""
    return question


def readable_diff(diff: str) -> str:
    """A unified diff as lines that say what they are when read aloud."""
    out = []
    for line in diff.splitlines():
        if line.startswith(_DIFF_HEADERS):
            continue
        if line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            out.append(f"At line {match.group(1) if match else '?'}:")
        elif line.startswith("+"):
            out.append(f"Added: {line[1:]}")
        elif line.startswith("-"):
            out.append(f"Removed: {line[1:]}")
        elif line.startswith("Binary files "):
            out.append("Binary file changed; its contents cannot be read as text.")
        else:
            out.append(f"Unchanged: {line[1:]}")
    return "\n".join(out)


def remember_changed_files(cwd: str, found: tuple[str, list[tuple[str, str, bool]]] | None) -> None:
    """Keep a folder's listing warm, and forget an answer with nothing to list."""
    key = _changed_files_cache_key(cwd)
    if found is not None and found[1]:
        _changed_files_cache[key] = found
    else:
        _changed_files_cache.pop(key, None)


def remember_slash_commands(backend: str, cwd: str | None, commands: list[tuple[str, str]]) -> None:
    """Keep a completed probe warm, so the next open does not ask again."""
    _slash_commands_cache[_slash_commands_cache_key(backend, cwd)] = list(commands)


def _tab_title(text: str) -> str:
    """A conversation's name, cut to something a tab strip can show."""
    flat = " ".join((text or "").split())
    if len(flat) <= 32:
        return flat
    return flat[:31].rstrip() + "…"


def _short_label(path: str) -> str:
    """Tab label: directory basename, or full path if at the filesystem root."""
    name = Path(path).name
    return name or path


class SessionPanel(wx.Panel):
    """One conversation tab. Owns its session_id, rows, and worker.

    Layout, top to bottom: working-directory label, search box, the flat list of
    rows (oldest at top, newest at bottom), the multi-line prompt box, the Send
    button. Focus starts in the prompt; Ctrl+Up (or Alt+Up) from the prompt
    enters the newest row. Arrow keys remain within the responses, including at
    the first and last rows; Tab is the way to move between the list and the
    prompt.

    `on_status(panel, text)` lets the frame show only the active tab's status,
    and `on_title(panel, text)` names the tab after the conversation in it.
    """

    def __init__(
        self,
        parent: wx.Window,
        cwd: str,
        on_status: Callable[[SessionPanel, str], None],
        on_title: Callable[[SessionPanel, str], None],
        earcons: Earcons,
        on_side_chat: Callable[[str, str], None],
        get_backend: Callable[[], str],
        focus_before: Callable[[], None],
        focus_after: Callable[[], None],
        session_title: str = "",
    ):
        super().__init__(parent)
        self.cwd = cwd
        # A name given when the session was created. Sent on the first turn's
        # session.create and then done with: from that point Hermes owns the
        # conversation's title, so keeping it would make a rename here lie.
        self._session_title = (session_title or "").strip()
        self._on_status = on_status
        self._on_title = on_title
        self._earcons = earcons
        self._on_side_chat = on_side_chat
        self._get_backend = get_backend
        self._focus_before = focus_before
        self._focus_after = focus_after
        self.last_status = "Ready"
        self._notifications: list[wx.adv.NotificationMessage] = []

        # A question this turn asked through its backend's own question tool,
        # so a question left in the answer as well is not asked over again.
        self._structured_question_asked = False
        # A question the finished turn only wrote into its answer, waiting to
        # be put to the person once the turn is fully done: a string when the
        # question was read out of prose, or the block's own Question list,
        # which is what carries options for the dialog to offer.
        self._question_in_the_answer: str | Sequence[Question] = ""

        self._turns: list[Turn] = []
        # The agent's own task list, as its task tools last left it.
        self.task_list = TaskList()
        # Each question and permission request this conversation put to the
        # person, with what they answered: (what was asked, the answer).
        self.decisions: list[tuple[str, str]] = []
        # Remote Control for this tab's Claude Code conversation: wanted or not,
        # and the claude.ai session it joins every turn once Claude named it.
        self.remote_control = False
        self.remote_bridge = ""
        self.remote_url = ""
        # Told of each finished turn's answer (None for a stopped or failed
        # one) while this tab is in an agent conversation.
        self.on_turn_answer: Callable[[SessionPanel, str | None], None] | None = None
        self._rows: list[Row] = []  # every row across every response, in order
        self._displayed: list[Row] = []  # rows currently shown (after search)
        # Start offset of each displayed row's line in the text view, kept in
        # step with _displayed so a caret position can be mapped to a row.
        self._row_starts: list[int] = []
        self._search_term = ""
        self._response_count = 0
        # Response number of the turn currently streaming in (None between turns).
        self._stream_response: int | None = None
        self._assistant_narrated_this_turn = False
        # A Hermes conversation being read back; its rows bypass the live gate.
        self._replaying = False
        # Answer text already put into the list for the turn in flight, so the
        # finished answer can be checked against it rather than assumed shown.
        self._streamed_assistant = ""
        # The message being streamed a sentence at a time: [response number,
        # speaker, its text so far, parse offset, rows added so far]. See
        # _stream_answer_rows.
        self._live_answer: list | None = None
        # Set while the user's Stop is being carried out, so the backend's own
        # "cancelled" report is not announced to them as an error.
        self._stopping = False
        # The backend's own question, while it is on screen. Held so stopping
        # the run can close it: the worker thread is blocked on the answer, and
        # the thread that would stop it is the one the dialog is running on.
        self._question_dialog: QuestionDialog | None = None
        # Which conversation the tab's held Claude process belongs to. Bumped
        # whenever those processes are let go, so a wake-up queued before that
        # cannot open a late turn on whatever the tab holds now.
        self._claude_generation = 0
        # The generation of a wake-up that arrived while the last turn's `done`
        # was still in the mailbox, or None. The late turn starts once it
        # drains, if the conversation it belongs to is still the one here.
        self._late_turn_waiting: int | None = None
        self._session_id: str | None = None
        # The Account this tab's Codex conversation is bound to, or UNBOUND
        # until its first turn takes one from the saved choice. A conversation
        # stays on the Account it began on, so choosing another one affects the
        # next conversation rather than moving the thread somebody is in.
        self._codex_plan_account: object = UNBOUND
        self._backend = normalize_backend(self._get_backend())
        self._session_backend = self._backend
        self._museai_main_required = False
        self._museai_main_loading = False
        self._museai_main_serial = 0
        self._museai_pending_send = False
        # The conversation's name as the tab shows it; the frame keeps it.
        self.tab_title = self._session_title
        self._worker: AgentWorker | None = None
        self._pending_messages: list[tuple[str, list[str], str]] = []
        self._queue_paused = False
        self._steering_context = ""
        self._commandcode_dirs: tuple[str, ...] = ()
        # One Hermes connection per tab, reused by each turn of this
        # conversation. Created on first use rather than here, so a machine
        # without Hermes never imports its adapter -- the same reason
        # ``worker_class`` defers that import.
        self._held_hermes: object | None = None
        # Worker callbacks arrive on a background thread. Keep them in one
        # ordered mailbox with at most one pending GUI callback; otherwise a
        # long, chatty job can flood wx's event queue and starve NVDA/key input.
        self._worker_event_lock = threading.Lock()
        self._worker_events: deque[tuple[str, tuple[object, ...]]] = deque()
        self._worker_events_scheduled = False
        # Starts at your remembered choice, or the active provider's default
        # mode for this directory.
        self.mode = _default_permission_mode(cwd, self._session_backend)
        # Empty means "don't pass the flag" — the CLI picks its own default.
        self.model = ""
        self.effort = ""
        self._auto_route = ""
        self._auto_check_announced = False
        # What the CLI last reported it is using, for when we pass no flag.
        self._cli_model = ""
        self._cli_effort = ""
        # The model catalog this tab last read, and whether a background
        # refresh is already out. The list is kept so a change can be told from
        # the same list being read again; None means nothing has been read yet,
        # which is not a change worth announcing.
        self._known_models: list[str] | None = None
        self._model_refresh_running = False
        # Which changed-file discovery is current for this tab, so a worker
        # that finishes after a refresh cannot fill the list it replaced.
        self._changed_files_serial = 0
        # The same for slash-command probes, and the dialog they fill.
        self._slash_commands_serial = 0
        self._slash_dialog: SlashCommandDialog | None = None
        self._attachments: list[str] = []

        self.backend_status = wx.StaticText(
            self, label=f"Backend: {backend_label(self._session_backend)}"
        )
        self.backend_status.SetName("Backend")

        cwd_label = wx.StaticText(self, label=f"Working directory: {cwd}")
        cwd_label.SetName("Working directory")

        # The subagents the backend has running. Created ahead of the
        # responses so native Tab order puts the list between the tab strip and
        # the conversation, and hidden -- which takes it out of that order --
        # whenever none is running.
        self._subagents: dict[str, SubagentEntry] = {}
        self._subagent_ids: list[str] = []
        self._subagent_view: SubagentDialog | None = None
        self._subagents_outlive_turn = False
        self.subagents_label = wx.StaticText(self, label="Subagents running:")
        self.subagents = wx.ListBox(self, style=wx.LB_SINGLE)
        self.subagents.SetName("Subagents running")
        self.subagents.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._open_subagent())
        self.subagents.Bind(wx.EVT_KEY_DOWN, self._on_subagents_key)
        self.subagents.SetMinSize(wx.Size(-1, self.subagents.GetCharHeight() * 5))
        self.subagents_label.Hide()
        self.subagents.Hide()

        responses_label = wx.StaticText(self, label="Responses:")
        self.responses = make_conversation_list(self)
        self.responses.SetName("Responses")
        self.responses.Bind(wx.EVT_LISTBOX_DCLICK, self._on_list_activate)
        self.responses.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.responses.Bind(wx.EVT_CONTEXT_MENU, lambda _e: self._show_row_menu())

        # Same rows, one per line, in a read-only edit field — NVDA (and any
        # screen reader) can then browse them with its own review/say-all
        # commands, select across rows, and copy with Ctrl+C. Options decides
        # which of the two controls is shown; only the visible one is filled.
        self.responses_text = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.responses_text.SetName("Responses")
        self.responses_text.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.responses_text.Bind(wx.EVT_CONTEXT_MENU, lambda _e: self._show_row_menu())
        self.responses_text.Bind(wx.EVT_SET_FOCUS, self._on_text_view_focus)

        prompt_label = self._prompt_label = wx.StaticText(self, label="Prompt:")
        self.prompt = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER | wx.TE_RICH2,
        )
        self.prompt.SetName("Prompt")
        self.prompt.SetHint(
            "Type your prompt. Enter to send, Shift+Enter for newline, Ctrl+Up to enter "
            "responses; Tab returns here from responses."
        )
        self.prompt.Bind(wx.EVT_KEY_DOWN, self._on_prompt_key)
        self.prompt.Bind(wx.EVT_SET_FOCUS, self._on_prompt_focus)
        self.prompt.Bind(wx.EVT_TEXT, self._on_prompt_text_changed)
        self._dictation_timer: wx.CallLater | None = None
        # What the prompt held at the last change, so the next one can be
        # told apart from a keystroke, and what is waiting to be read back.
        self._prompt_text = ""
        self._dictation_pending = ""
        char_h = self.prompt.GetCharHeight()
        self.prompt.SetMinSize(wx.Size(-1, char_h * 5 + self.FromDIP(PAD)))

        # Bottom row: Send, Attach, then the Permission mode picker — one line.
        self.send_btn = wx.Button(self, label="Send")
        self.send_btn.SetName("Send")
        self.send_btn.Bind(wx.EVT_BUTTON, lambda _e: self._on_send())
        self.send_btn.Bind(wx.EVT_KEY_DOWN, self._on_send_key)

        # Steer sits right after Send in the tab order, so during a run you can
        # type a correction, press Tab once, and press it. Enabled only while a
        # run is actually listening.
        self.steer_btn = wx.Button(self, label="Steer")
        self.steer_btn.SetName("Steer the running task")
        self.steer_btn.SetToolTip("Send this message into the task that is already running")
        self.steer_btn.Bind(wx.EVT_BUTTON, lambda _e: self._on_steer())
        self.steer_btn.Disable()

        # Stop follows Steer: the two things you can do to a run in progress sit
        # together, one Tab apart from the prompt. Enabled only while one is.
        self.stop_btn = wx.Button(self, label="Stop")
        self.stop_btn.SetName("Stop the running task")
        self.stop_btn.SetToolTip("Stop the task that is running now")
        self.stop_btn.Bind(wx.EVT_BUTTON, lambda _e: self._on_stop())
        self.stop_btn.Disable()

        # Sighted only. It is never focusable and has no name for the reader;
        # the earcon and the status line already say a turn is running.
        self.working = wx.ActivityIndicator(self)
        self.working.Hide()

        self.attach_btn = wx.Button(self, label="Attach")
        self.attach_btn.SetName("Attach files")
        self.attach_btn.Bind(wx.EVT_BUTTON, lambda _e: self.attach_files())

        self.slash_btn = wx.Button(self, label="Slash…")
        self.slash_btn.SetName("Slash command picker")
        self.slash_btn.Bind(wx.EVT_BUTTON, lambda _e: self._pick_slash_command())

        mode_label = wx.StaticText(self, label="Permission mode:")
        self.mode_picker = wx.Choice(self, choices=_MODE_LABELS)
        self.mode_picker.SetName("Permission mode")
        self.mode_picker.SetSelection(_MODE_VALUES.index(self.mode))
        self.mode_picker.Bind(wx.EVT_CHOICE, self._on_mode_choice)
        self.mode_picker.Bind(wx.EVT_KEY_DOWN, self._on_mode_key)

        pad = self.FromDIP(PAD)
        # Buttons that act on the same thing sit a control gap apart; the
        # groups (run, message, mode) sit a dialog margin apart.
        group_gap = self.FromDIP(PAD_DIALOG)
        bottom_row = wx.BoxSizer(wx.HORIZONTAL)
        bottom_row.Add(self.send_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        bottom_row.Add(self.steer_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        bottom_row.Add(self.stop_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, group_gap)
        bottom_row.Add(self.working, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, group_gap)
        bottom_row.Add(self.attach_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        bottom_row.Add(self.slash_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, group_gap)
        bottom_row.Add(mode_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        bottom_row.Add(self.mode_picker, 0, wx.ALIGN_CENTER_VERTICAL)

        # Labels and the controls under them share one border, so they share
        # one left edge.
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.backend_status, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(cwd_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.subagents_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.subagents, 0, wx.EXPAND | wx.ALL, pad)
        sizer.Add(responses_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.responses, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(self.responses_text, 1, wx.EXPAND | wx.ALL, pad)
        sizer.Add(prompt_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        sizer.Add(self.prompt, 0, wx.EXPAND | wx.ALL, pad)
        sizer.Add(bottom_row, 0, wx.ALL, pad)
        self.SetSizer(sizer)
        self.apply_view_mode()
        self.backend_changed()

    def _show_working(self) -> None:
        if not self.working.IsRunning():
            self.working.Show()
            self.working.Start()
            self.Layout()

    def _hide_working(self) -> None:
        if self.working.IsRunning():
            self.working.Stop()
            self.working.Hide()
            self.Layout()

    # ----- Responses view (list box or read-only edit field) -----
    def apply_view_mode(self) -> None:
        """Show whichever responses control Options currently asks for.

        Keeps the row the user was on, so flipping the setting mid-read does not
        lose their place, and hands focus to the new control if the old one had
        it.
        """
        was_on = self._selected_row()
        had_focus = self._responses_ctrl().HasFocus()
        text_mode = SETTINGS.text_view
        sizer = self.GetSizer()
        sizer.Show(self.responses, not text_mode)
        sizer.Show(self.responses_text, text_mode)
        self.Layout()
        self._refresh_list()
        if was_on == wx.NOT_FOUND:
            return
        if had_focus:
            self._focus_row(was_on)
        else:
            self._select_row(was_on)

    def _responses_ctrl(self) -> wx.Window:
        return self.responses_text if SETTINGS.text_view else self.responses

    def _row_count(self) -> int:
        return len(self._displayed)

    def _selected_row(self) -> int:
        """Index into ``self._displayed`` of the row the user is on."""
        if not self._displayed:
            return wx.NOT_FOUND
        if SETTINGS.text_view:
            line = _row_at(self._row_starts, self.responses_text.GetInsertionPoint())
            return line if 0 <= line < len(self._displayed) else wx.NOT_FOUND
        sel = self.responses.GetSelection()
        return sel if 0 <= sel < len(self._displayed) else wx.NOT_FOUND

    def _select_row(self, index: int) -> None:
        """Move to a row without stealing focus."""
        count = self._row_count()
        if count == 0:
            return
        index = max(0, min(index, count - 1))
        if SETTINGS.text_view:
            self.responses_text.SetInsertionPoint(self._row_starts[index])
        else:
            self.responses.SetSelection(index)

    # ----- Focus helpers -----
    def focus_prompt(self) -> None:
        if _STARTUP_CHECK:
            # A startup check shows no window. Asking for focus anyway takes
            # it from whoever is running the check, and Windows has to show a
            # window to give it focus - which drags the hidden one onto their
            # screen. Guarded here rather than at the four call sites, because
            # a fifth would not know to guard itself.
            return
        if not self.IsShownOnScreen():
            # Chat mode hides the whole notebook, and this is called from a
            # CallAfter queued when the session was made. A control nobody can
            # see must not take focus: Tab and Shift+Tab would walk a page that
            # is not on screen, and the window would look like it had lost
            # focus altogether until something moved it back.
            return
        self.prompt.SetFocus()

    def focus_first_control(self, past_subagents: bool = False) -> None:
        if not past_subagents and self._subagents_shown():
            self.subagents.SetFocus()
            if self.subagents.GetSelection() == wx.NOT_FOUND:
                self.subagents.SetSelection(0)
            return
        control = self._responses_ctrl() if self._row_count() else None
        # The view the settings ask for is the one that is shown, but a
        # hidden or disabled control accepts SetFocus by doing nothing at
        # all — which would leave the Tab that asked for this move with
        # nowhere to have gone. Fall through to the Prompt instead.
        if control is None or not (control.IsShown() and control.IsEnabled()):
            self.prompt.SetFocus()
            return
        control.SetFocus()
        if (
            control is self.responses
            and control.GetCount()
            and control.GetSelection() == wx.NOT_FOUND
        ):
            control.SetSelection(0)

    def focus_above_responses(self) -> None:
        """Shift+Tab out of the conversation: the subagents, or the tab strip."""
        if self._subagents_shown():
            self.subagents.SetFocus()
            return
        self._focus_before()

    def focus_last_control(self) -> None:
        for control in (
            self.mode_picker,
            self.slash_btn,
            self.attach_btn,
            self.stop_btn,
            self.steer_btn,
            self.send_btn,
            self.prompt,
            self._responses_ctrl(),
        ):
            if control.IsShown() and control.IsEnabled():
                control.SetFocus()
                return

    def focus_first_action(self) -> None:
        """Focus the first available control after Prompt."""
        for control in (
            self.send_btn,
            self.steer_btn,
            self.stop_btn,
            self.attach_btn,
            self.slash_btn,
            self.mode_picker,
        ):
            if control.IsShown() and control.IsEnabled():
                control.SetFocus()
                return

    def focus_first_action_delayed(self) -> None:
        """Let NVDA finish its edit-field inspection before leaving Prompt."""

        def move() -> None:
            if self and self.prompt.HasFocus():
                self.focus_first_action()

        wx.CallLater(75, move)

    def _focus_row(self, index: int) -> None:
        if self._row_count() == 0:
            return
        self._select_row(index)
        self._responses_ctrl().SetFocus()

    def _on_text_view_focus(self, event: wx.FocusEvent) -> None:
        event.Skip()
        if screen_speech._MAC_ANNOUNCE:
            wx.CallAfter(announce, "Responses, read only edit")

    # ----- Permission mode -----
    def _set_mode(self, value: str) -> None:
        if value not in _MODE_VALUES:
            return
        self.mode = value
        self.mode_picker.SetSelection(_MODE_VALUES.index(value))
        self._announce(_MODE_DESCRIPTIONS[value])
        # Remembered globally, so new tabs and the next launch start here.
        # After the description: a refusal queues behind it, so the failure is
        # the last thing said rather than the claim sounding like its repair.
        _remember_permission_mode(value)

    def _on_mode_choice(self, event: wx.CommandEvent) -> None:
        self._set_mode(_MODE_VALUES[self.mode_picker.GetSelection()])

    def _on_mode_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_TAB and not event.ShiftDown():
            self._focus_after()
            return
        event.Skip()

    # ----- Backend -----
    def selected_backend(self) -> str:
        """This tab's backend. Each tab keeps its own, so two tabs can differ."""
        return self._backend

    def set_backend(self, backend: str) -> None:
        """Point this tab's next turn at ``backend``; other tabs keep theirs."""
        self._backend = normalize_backend(backend)
        if self._backend != BACKEND_MUSEAI:
            self._museai_main_serial += 1
            self._museai_main_loading = False
            self._museai_main_required = False
            self._museai_pending_send = False
        self.backend_changed()
        if self._backend == BACKEND_MUSEAI:
            self.open_museai_main()

    def open_museai_main(self, resume_pending: bool = False) -> None:
        """Selecting muse.ai resumes its main conversation, without sending anything."""
        from museai_backend import museai_config_path, museai_main_session_id

        self._museai_main_required = True
        if self._run_in_progress():
            self._announce("Stop the running task before opening muse.ai's main chat")
            return
        if self._museai_main_loading:
            self._museai_pending_send |= resume_pending
            return
        self._museai_main_loading = True
        self._museai_pending_send = resume_pending
        self._museai_main_serial += 1
        serial = self._museai_main_serial
        self._announce("Loading muse.ai's main chat")

        def work() -> None:
            try:
                session = museai_main_session_id() if museai_config_path().is_file() else None
                if not session:
                    raise ValueError("Check your sign-in.")
                entry = HistoryEntry(BACKEND_MUSEAI, session, "Main chat", "", 0, folder="muse.ai")
                turns = load_turns(entry)
                error = ""
            except Exception as exc:
                entry, turns, error = None, [], str(exc)
            _call_after_alive(self._museai_main_loaded, serial, entry, turns, error)

        threading.Thread(target=work, daemon=True).start()

    def _museai_main_loaded(self, serial: int, entry, turns, error: str) -> None:
        if not self or serial != self._museai_main_serial:
            return
        self._museai_main_loading = False
        if self._backend != BACKEND_MUSEAI:
            return
        if error or entry is None:
            self._museai_pending_send = False
            self._announce(
                f"Error: muse.ai's main chat could not be opened. {error or 'Check your sign-in.'}"
            )
            return
        resume_pending = self._museai_pending_send
        self._museai_pending_send = False
        self._museai_main_required = False
        self.model = self.effort = self._cli_model = self._cli_effort = ""
        self.restore_history(entry, turns)
        if resume_pending:
            _call_after_alive(self._send_museai_pending, self._museai_main_serial)

    def _send_museai_pending(self, serial: int) -> None:
        if (
            not self
            or serial != self._museai_main_serial
            or self._backend != BACKEND_MUSEAI
            or self._session_backend != BACKEND_MUSEAI
        ):
            return
        self.send_now()

    def show_backend_in_prompt(self, shown: bool) -> None:
        """Name the prompt after the backend it sends to, while tabs are mixed.

        The mistake this prevents happens at Enter, not at the tab switch, so
        the box itself says where the message goes. Both the visible label and
        the control's name change: screen readers take the label from either.
        """
        name = f"{backend_label(self._backend)} prompt" if shown else "Prompt"
        if self.prompt.GetName() != name:
            self.prompt.SetName(name)
            self._prompt_label.SetLabel(f"{name}:")

    def backend_changed(self) -> None:
        """Refresh the visible provider label after File → Backend changes."""
        selected = self.selected_backend()
        suffix = ""
        if selected != self._session_backend and self._session_id:
            suffix = ". New conversation on next send"
        self.backend_status.SetLabel(f"Backend: {backend_label(selected)}{suffix}")
        if selected == BACKEND_FREEBUFF:
            # FreeBuff's terminal takes seconds to reach the point where it can
            # be given a message. Start one now, so the first message of the
            # conversation does not spend that wait in silence.
            prewarm_freebuff(self.cwd, self._session_id, self.model)
        if selected == BACKEND_OPENCODE:
            # Same idea: opencode's server takes a few seconds to come up, and
            # the model list and the first message both wait on it. Starting it
            # now spends that wait while the user is still typing. The probe is
            # what starts it, and it caches its answer for /model as well.
            self.warm_model_probe()
        # Ask the backend what it supports rather than naming the one that does
        # not: a further backend arriving is otherwise silently given a control
        # its protocol has no answer for.
        supports_permissions = BACKENDS[selected].supports_permissions
        self.mode_picker.Enable(supports_permissions)
        if supports_permissions:
            self.mode_picker.SetToolTip(
                "Choose how the backend handles sandbox and approval requests"
            )
        else:
            self.mode_picker.SetToolTip(
                "muse.ai uses Sentinel approvals. Review them in Model, muse.ai, Approvals."
                if selected == BACKEND_MUSEAI
                else f"{backend_label(selected)} has no permission modes. It never stops to ask."
            )
        self.Layout()

    # ----- Model and effort -----
    def _model_summary(self) -> str:
        """What the next message will run as: this tab's override where it has
        one, otherwise whatever the selected backend last reported."""
        model = (
            "automatic routing"
            if self.model == AUTO_MODEL
            else self.model or self._cli_model or "CLI default"
        )
        effort = self.effort or (
            "automatic per task" if self.model == AUTO_MODEL else self._cli_effort or "CLI default"
        )
        return f"model {model}, effort {effort}"

    def warm_model_probe(self) -> None:
        """Ask the CLI about models in the background, so /model opens fast.

        The answer also tells us which model is in use, which is what the
        status line reports whenever this tab passes no --model flag.
        """

        backend = self.selected_backend()

        def work() -> None:
            options = probe_model_options(self.cwd, PROBE_TTL_SECONDS, backend)
            wx.CallAfter(self._remember_cli_model, options, backend)

        threading.Thread(target=work, daemon=True).start()

    def _remember_cli_model(self, options: ModelOptions, backend: str | None = None) -> None:
        if not self:  # tab closed while the probe was running
            return
        if backend is not None and normalize_backend(backend) != self.selected_backend():
            return
        self._cli_model = options.current_model
        self._cli_effort = options.current_effort
        # The list this read produced is what the picker now offers, so it
        # becomes the baseline a later background read compares against. A read
        # that came back with nothing -- a probe that failed, or a backend with
        # no list of its own -- is not a baseline: it would make the next good
        # read look like the whole list had just been added.
        if options.models:
            self._known_models = list(options.models)

    def refresh_models_in_background(self) -> None:
        """Re-read this tab's model catalog, off the UI thread, and say changes.

        A window left open used to read the catalog only when /model opened or
        the backend changed, so a model retired upstream stayed on offer until
        somebody happened to open the picker. The window's timer calls this
        instead, and it is a no-op while the cached answer is inside the
        interval the person set, so running it often costs nothing. Zero means
        the refresh is off altogether.
        """
        if self._model_refresh_running:
            return
        minutes = SETTINGS.model_refresh_minutes
        if minutes <= 0:
            return
        backend = self.selected_backend()
        if cached_model_options(self.cwd, minutes * 60, backend) is not None:
            return
        self._model_refresh_running = True

        def work() -> None:
            options = probe_model_options(self.cwd, backend=backend)
            wx.CallAfter(self._models_refreshed, backend, options)

        threading.Thread(target=work, daemon=True).start()

    def _models_refreshed(self, backend: str, options: ModelOptions) -> None:
        if not self:  # tab closed while the probe was running
            return
        self._model_refresh_running = False
        provider = normalize_backend(backend)
        if provider != self.selected_backend():
            return
        # Read before the new list is recorded, so this compare is against what
        # the tab last showed rather than the list that has just replaced it.
        previous = self._known_models
        self._remember_cli_model(options, provider)
        self._announce_model_changes(provider, options.models, previous)

    def _announce_model_changes(
        self, backend: str, models: list[str], previous: list[str] | None
    ) -> None:
        """Say which models the backend gained or lost since this tab last read.

        Nothing is said the first time. That list is what /model already offers,
        not news, and a listener should not be greeted by it on the first tick.
        After that any difference is real -- a model retired upstream, or one
        the service has just added -- and a listener, who cannot see the list
        change, has no other way to learn it. A read that came back with
        nothing is not a change; it is a probe that failed.
        """
        if previous is None or not models or previous == models:
            return
        added = [model for model in models if model not in previous]
        gone = [model for model in previous if model not in models]
        said = []
        if gone:
            said.append(f"no longer offers {', '.join(gone)}")
        if added:
            said.append(f"now offers {', '.join(added)}")
        if said:
            self._on_activity("notice", f"{backend_label(backend)} {'; '.join(said)}.")

    def open_model_dialog(self, force_refresh: bool = False) -> None:
        """/model — offer the two combo boxes, filled from the CLI.

        A recent probe opens the dialog immediately; only a cold cache waits on
        the CLI, and that wait is announced so nothing looks frozen. Either way
        a background refresh runs, so the next open is both fast and current.
        """
        backend = self.selected_backend()
        if force_refresh:
            invalidate_model_options(backend)
        cached = (
            None if force_refresh else cached_model_options(self.cwd, PROBE_TTL_SECONDS, backend)
        )
        if cached is not None:
            self.warm_model_probe()
            self._show_model_dialog(cached, backend)
            return

        self._announce(f"Reading the model list from {backend_label(backend)}…")

        def work() -> None:
            options = probe_model_options(self.cwd, backend=backend)
            wx.CallAfter(self._show_model_dialog, options, backend)

        threading.Thread(target=work, daemon=True).start()

    def _show_model_dialog(self, options: ModelOptions, backend: str | None = None) -> None:
        if not self:  # tab closed while the probe was running
            return
        provider = normalize_backend(backend or self.selected_backend())
        self._remember_cli_model(options, provider)
        dlg = ModelDialog(
            self,
            options,
            self.model,
            self.effort,
            backend_label(provider),
            allow_auto_model=provider == BACKEND_COMMANDCODE,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self._announce(f"Model unchanged. Still using {self._model_summary()}.")
                return
            model, effort = dlg.selection()
        finally:
            dlg.Destroy()
        self.set_model(model, effort)

    def open_connect_dialog(self) -> None:
        """/connect — opencode's provider list, as its own command offers it."""
        if self.selected_backend() != BACKEND_OPENCODE:
            self._announce(
                "Error: /connect is an opencode command. Choose opencode under Model, Backend first"
            )
            return
        dlg = ConnectDialog(self)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        self.prompt.SetFocus()

    def open_status_dialog(self) -> None:
        """/status — what this tab is set to, and who the backend signed in as.

        Offered for every backend, because none of them answers it themselves
        in the headless mode BlindPilot drives them in: Claude Code's own
        /status is interactive-only and replies "/status isn't available in
        this environment" when it arrives as a message, and Codex, FreeBuff and
        opencode have no status command at all. Each is asked in the way it can
        answer instead, on a thread of its own — reaching a provider CLI
        takes a second or two, and the window stays usable meanwhile.
        """
        backend = self.selected_backend()
        self._announce(f"Reading {backend_label(backend)} status")

        def work() -> None:
            report = backend_status(backend)
            wx.CallAfter(self._show_status, backend, report)

        threading.Thread(target=work, daemon=True).start()

    def _note_context(self, seen: tuple[int, int] | None) -> None:
        """Keep the context fill the turn reported, and warn once past 80%.

        A conversation that fills its window is compacted by the backend
        without asking, or fails, and either one arrives with no warning.
        """
        if not seen:
            return
        used, window = seen
        before = SessionPanel._known_context(self)
        window = window or before[1]
        self._context = (used, window)
        self._context_session = SessionPanel._context_owner(self)
        if window and used >= 0.8 * window and not (before[1] and before[0] >= 0.8 * before[1]):
            self._announce(
                f"{_context_line(used, window)}. Compact Conversation, Ctrl+Shift+K, makes room."
            )

    def _known_context(self) -> tuple[int, int]:
        """The context reading, if it belongs to the conversation in the tab now.

        Tied to the session id rather than cleared at each place a conversation
        is replaced (new conversation, a reopened one, a backend switch), so no
        such place can be missed.
        """
        if getattr(self, "_context_session", None) != SessionPanel._context_owner(self):
            return (0, 0)
        return getattr(self, "_context", (0, 0))

    def _context_owner(self) -> tuple:
        """The conversation a reading belongs to. A backend picked since the
        last message starts a new one on the next, whatever the id says."""
        session = getattr(self, "_session_id", None)
        backend = getattr(self, "_session_backend", None)
        selected = self.selected_backend() if hasattr(self, "selected_backend") else backend
        return (backend, session) if selected == backend else (selected, None)

    def _session_status_lines(self) -> list[str]:
        """What this tab will do with the next message, as the report says it."""
        backend = self.selected_backend()
        conversation = "continuing" if self._session_id else "new, nothing sent yet"
        if self._session_id and backend != self._session_backend:
            conversation = "new, the backend changed since the last message"
        if BACKENDS[backend].supports_permissions:
            mode = _MODE_LABEL_BY_VALUE.get(self.mode, self.mode)
        else:
            # The picker is disabled for these, so reporting the remembered
            # value would name a setting that has no effect on this backend.
            mode = "not offered by this backend"
        model = self.model or self._cli_model or "CLI default"
        if self.model == AUTO_MODEL:
            model = f"Automatic routing ({self._auto_route or 'no route selected yet'})"
        effort = self.effort or (
            "automatic per task" if self.model == AUTO_MODEL else self._cli_effort or "CLI default"
        )
        return [
            f"Model: {model}",
            f"Effort: {effort}",
            f"Permission mode: {mode}",
            # A remote Hermes session may have no folder on this machine at all,
            # and "Folder: " followed by nothing reads as a missing value rather
            # than as a deliberate one.
            f"Folder: {self.cwd or 'chosen by the Hermes running this session'}",
            f"Conversation: {conversation}",
            _context_line(*SessionPanel._known_context(self)),
        ]

    def turn_status(self) -> None:
        """How long this turn has run and what it did last (Ctrl+Shift+T).

        A long turn with Keep up narration, or with live activity off, is
        silent apart from the working sound, and that says nothing about
        whether it is three seconds or thirty minutes in, or what it is doing.
        """
        started = getattr(self, "_turn_started_at", None)
        if started is None:
            last = getattr(self, "_last_turn_seconds", None)
            said = "No turn is running."
            if last is not None and getattr(self, "_turn_attached", False):
                # Attached part way through: only the end of it was seen.
                said += f" The last one was followed for {_spoken_duration(last)} after attaching."
            elif last is not None:
                said += f" The last one took {_spoken_duration(last)}."
        else:
            took = _spoken_duration(time.monotonic() - started)
            if getattr(self, "_turn_attached", False):
                said = f"Attached to a running turn {took} ago."
            else:
                said = f"Working for {took}."
            step = getattr(self, "_last_step", "")
            said += f" Last step: {step}." if step else " No tool used yet."
        queued = len(getattr(self, "_pending_messages", []))
        if queued:
            said += f" {queued} {'message' if queued == 1 else 'messages'} queued."
        tasks = getattr(self, "task_list", None)
        if tasks is not None and tasks.progress():
            said += f" {tasks.progress()}"
        self._announce(said)

    def _on_tasks(self, source: object, *call: object) -> None:
        """One task-list update from the worker: apply it, say what got done."""
        if source == "codex":
            finished = self.task_list.apply_codex(call[0] if call else None)
        elif source == "copilot":
            finished = self.task_list.apply_checklist(call[0] if call else None)
        elif len(call) == 3 and isinstance(call[1], dict):
            finished = self.task_list.apply_claude(str(call[0]), call[1], str(call[2]))
        else:
            return
        for subject in finished:
            self._announce(f"Task done: {subject}")

    def open_task_list(self) -> None:
        """The agent's task list, each step with its state (Ctrl+Shift+J)."""
        tasks = self.task_list.tasks
        if not tasks:
            self._announce(
                "No task list in this conversation yet. Claude Code, Codex and Copilot keep one "
                "for longer tasks."
            )
            return
        items = [
            ConversationItem(line, "\n\n".join(filter(None, (task.subject, task.description))))
            for line, task in zip(self.task_list.lines(), tasks, strict=True)
        ]

        def read(item: ConversationItem) -> None:
            viewer = ReadView(dialog, item.payload, item.label)
            try:
                viewer.ShowModal()
            finally:
                viewer.Destroy()

        dialog = ConversationItemsDialog(
            self,
            items,
            f"Task List, {self.task_list.progress()}",
            links=False,
            copy=_copy_to_clipboard,
            read=read,
            announce=announce,
        )
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def command_items(self) -> list[ConversationItem]:
        """Each command the agent ran, newest first: done, failed or no result yet."""
        found: list[list[str]] = []  # [command, state, output]
        for row in self._rows:
            if row.kind == "tool" and row.label.startswith("Running"):
                found.append([row.payload.removeprefix("Running:").strip(), "no result yet", ""])
            elif found and found[-1][1] == "no result yet":
                if row.kind == "result":
                    found[-1][1:] = ["done", row.payload]
                elif row.kind == "tool" and row.label.startswith("Failed"):
                    found[-1][1:] = ["failed", row.payload]
        return [
            ConversationItem(f"{command}, {state}", f"{command}\n\n{output}".strip())
            for command, state, output in reversed(found)
        ]

    def _show_items(self, items: list[ConversationItem], title: str, empty: str) -> None:
        if not items:
            self._announce(empty)
            return

        def read(item: ConversationItem) -> None:
            viewer = ReadView(dialog, item.payload, item.label)
            try:
                viewer.ShowModal()
            finally:
                viewer.Destroy()

        dialog = ConversationItemsDialog(
            self,
            items,
            title,
            links=False,
            copy=_copy_to_clipboard,
            read=read,
            announce=announce,
        )
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def open_commands(self) -> None:
        """Every command the agent ran, with its output (Ctrl+Shift+X)."""
        items = self.command_items()
        self._show_items(
            items,
            f"Commands, {len(items)}",
            "No commands in this conversation yet. Turn on live activity in "
            "Preferences to keep the agent's steps.",
        )

    def open_decisions(self) -> None:
        """Every question and permission request, with the answer (Ctrl+Shift+Y)."""
        items = [
            ConversationItem(f"{asked}. {answer}", f"{asked}\n\n{answer}")
            for asked, answer in reversed(self.decisions)
        ]
        self._show_items(
            items, f"Decisions, {len(items)}", "No decisions in this conversation yet."
        )

    def property_lines(self) -> list[str]:
        """This session as "Property: value" lines (Properties, Alt+Enter)."""
        backend = self._session_backend or self.selected_backend()
        rows = self._rows
        sources = [(row.kind, row.source or row.payload) for row in rows if row.kind == "header"]
        lines = [
            f"Title: {self.tab_title or 'not named yet'}",
            f"Backend: {backend_label(backend)}",
            f"Folder: {self.cwd or 'none'}",
            f"Model: {self.model or 'the backend default'}",
            f"Effort: {self.effort or 'the backend default'}",
            f"Permission mode: {_MODE_LABEL_BY_VALUE.get(self.mode, self.mode)}",
            f"Session ID: {self._session_id or 'none yet'}",
            *(
                [f"Remote Control: {'on' if self.remote_control else 'off'}"]
                if backend == BACKEND_CLAUDE
                else []
            ),
            f"Your messages: {sum(row.kind == 'you' for row in rows) or len(self._turns)}",
            f"Responses: {self._response_count}",
            f"Tool steps: {sum(row.kind == 'tool' for row in rows)}",
            f"Commands: {len(self.command_items())}",
            f"Code blocks: {len(collect_items(sources, 'code'))}",
            f"Links: {len(collect_items(sources, 'links'))}",
            f"Decisions: {len(self.decisions)}",
        ]
        progress = self.task_list.progress()
        return lines + ([progress] if progress else [])

    def open_properties(self) -> None:
        lines = self.property_lines()
        self._show_items(
            [ConversationItem(line, line) for line in lines], "Properties", "Nothing to show."
        )

    def chat_permission_lines(self) -> list[str]:
        """What this session may do without asking, as lines to read.

        The mode first, then the rules granted for this session from the
        permission dialog, then -- for Claude Code -- the rules in Claude
        Code's own settings files that apply to this folder, which
        BlindPilot only ever reads.
        """
        backend = self._session_backend or self.selected_backend()
        lines = [
            f"Backend: {backend_label(backend)}",
            f"Permission mode: {_MODE_LABEL_BY_VALUE.get(self.mode, self.mode)}",
            f"Folder: {self.cwd or 'none'}",
            "Allowed for this session, by you:",
        ]
        granted = list(getattr(self, "_session_permission_rule_lines", None) or [])
        if self._session_id:
            for line in _saved_session_rules().get(self._session_id) or []:
                if line not in granted:
                    granted.append(line)
        lines += granted or ["Nothing beyond the permission mode."]
        lines.append(
            "Session grants last while this conversation runs here and are "
            "never written to a settings file."
        )
        if backend == BACKEND_CLAUDE:
            lines += ["Rules in Claude Code's own settings that apply to this folder:"]
            lines += _claude_settings_rule_lines(self.cwd) or ["None found."]
            lines.append("Change those in Claude Code, for example with /permissions.")
        return lines

    def open_chat_permissions(self) -> None:
        self._show_items(
            [ConversationItem(line, line) for line in self.chat_permission_lines()],
            "Chat Permissions",
            "Nothing to show.",
        )

    def _on_remote(self, data: dict) -> None:
        """Claude Code's answer to turning Remote Control on."""
        error = str(data.get("error") or "")
        if error:
            if error != getattr(self, "_remote_error", ""):
                self._remote_error = error
                self._announce(f"Could not turn on Remote Control: {error}")
            return
        bridge = str(data.get("bridge_session_id") or "")
        first = bool(bridge) and bridge != self.remote_bridge
        self.remote_bridge = bridge or self.remote_bridge
        self.remote_url = str(data.get("session_url") or "") or self.remote_url
        if self._session_id:
            _remember_remote(self._session_id, self.remote_bridge, self.remote_url)
        if first:
            self._announce(
                "This session is on Remote Control. Remote Control in Conversation, "
                "Find and Browse copies its claude.ai address."
            )

    def remote_control_menu(self) -> None:
        """Turn Remote Control on or off for this Claude Code tab, or copy its address."""
        if self.selected_backend() != BACKEND_CLAUDE:
            self._announce(
                "Remote Control is a Claude Code feature. Switch this tab to Claude Code."
            )
            return
        if not self.remote_control:
            answer = wx.MessageBox(
                "Turn on Remote Control for this session? From your next message, the "
                "conversation is copied to claude.ai and stays there, and while a turn runs "
                "anyone signed in to your Claude account can type into it or answer its "
                "questions from claude.ai or the Claude app.",
                "Remote Control",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                self,
            )
            if answer != wx.YES:
                return
            self.remote_control = True
            self._announce("Remote Control turns on with your next message.")
            return
        choices = (["Copy its claude.ai address"] if self.remote_url else []) + [
            "Turn Remote Control off"
        ]
        with wx.SingleChoiceDialog(
            self, "This session is on Remote Control.", "Remote Control", choices
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            picked = choices[dlg.GetSelection()]
        if picked.startswith("Copy"):
            if _copy_to_clipboard(self.remote_url):
                self._announce(f"Copied {self.remote_url}")
            return
        self.remote_control = False
        if self._session_id:
            _forget_remote(self._session_id)
        self._announce("Remote Control turns off with your next message.")

    def list_agents_results(self) -> list[str]:
        """Each ListAgents result in this conversation, oldest first."""
        results: list[str] = []
        listing = False
        for row in self._rows:
            if row.kind == "tool":
                listing = row.label.startswith(f"Using {remote_sessions.LIST_TOOL}")
            elif row.kind == "result" and listing:
                results.append(row.payload)
                listing = False
        return results

    def other_machines(self) -> None:
        """Your Claude Code sessions on other computers, through Claude (Ctrl+Shift+U)."""
        if self.selected_backend() != BACKEND_CLAUDE or not self.remote_control:
            wx.MessageBox(
                "Other machines are reached through Claude Code with Remote Control on, "
                "and each of them needs it on too. Switch this tab to Claude Code and turn "
                "on Remote Control in Conversation, Find and Browse, then try again.",
                "Other Machines",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            return
        found = remote_sessions.latest_list(self.list_agents_results())
        sessions = found or []
        if found is None:
            prompt = "Claude hasn't listed your other computers' sessions here yet."
        elif not sessions:
            prompt = (
                "Claude's latest list had no sessions on other computers. Each needs "
                "Remote Control on, and its computer awake."
            )
        else:
            prompt = (
                "Send a message to which session? From Claude's latest list; offline "
                "often only means between turns, and a message waits for it."
            )
        refresh = "Refresh the list (asks Claude)"
        choices = [session.describe() for session in sessions] + [refresh]
        with wx.SingleChoiceDialog(self, prompt, "Other Machines", choices) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            index = dlg.GetSelection()
        if index == len(sessions):
            if self.deliver(remote_sessions.LIST_PROMPT):
                self._announce("Asking Claude for your other computers' sessions.")
            else:
                self._announce("Error: This session is busy. Try again when its turn ends.")
            return
        target = sessions[index]
        with wx.TextEntryDialog(self, f"Message to {target.name}:", "Other Machines") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            text = dlg.GetValue().strip()
        if not text:
            self._announce("Nothing sent: the message was empty.")
            return
        address = remote_sessions.addresses(sessions)[index]
        if self.deliver(remote_sessions.send_prompt(address, text)):
            self._announce(f"Claude is sending your message to {target.name}.")
        else:
            self._announce("Error: This session is busy. Try again when its turn ends.")

    def copy_session_link(self) -> None:
        """Copy a Markdown link that reopens this conversation in BlindPilot."""
        backend = self._session_backend or self.selected_backend()
        if not self._session_id:
            self._announce("This session has no conversation to link to yet. Send a message first.")
            return
        link = remote_sessions.session_link(backend, self._session_id)
        title = (self.tab_title or _tab_label("", self.cwd)).replace("]", ")").replace("[", "(")
        if _copy_to_clipboard(f"[{title}]({link})"):
            self._announce(f"Copied a link to {title}")

    def open_changed_files(self) -> None:
        """What differs from the last commit here (Model menu, Ctrl+Shift+D).

        Enter on a file reads its changes a line at a time; closing that comes
        back to the list on the same file. Git discovery runs on a worker so
        the shortcut returns at once, and a folder's last listing is reused
        until Refresh asks Git again.
        """
        cwd = self.cwd
        if not cwd:
            self._announce("Open a session tab first, so changes have a folder to look in.")
            return
        cached = cached_changed_files(cwd)
        if cached is not None:
            serial = self._changed_files_serial + 1
            self._changed_files_serial = serial
            self._show_changed_files(serial, cwd, cached)
            return
        self._load_changed_files(cwd)

    def _load_changed_files(self, cwd: str) -> None:
        """Ask Git for this folder's changes on a worker."""
        serial = self._changed_files_serial + 1
        self._changed_files_serial = serial
        self._announce("Looking for changed files since the last commit…")

        def work() -> None:
            found = None
            error = ""
            try:
                found = changed_files(cwd)
            except (OSError, subprocess.SubprocessError) as exc:
                error = str(exc)
            _call_after_alive(self._changed_files_loaded, serial, cwd, found, error)

        threading.Thread(target=work, daemon=True).start()

    def _changed_files_loaded(
        self,
        serial: int,
        cwd: str,
        found: tuple[str, list[tuple[str, str, bool]]] | None,
        error: str,
    ) -> None:
        """Fill the list from a worker, unless its tab or request is no longer current."""
        if not self or serial != self._changed_files_serial or cwd != self.cwd:
            return
        if error:
            self._announce(f"Error: git could not be run: {error}")
            return
        remember_changed_files(cwd, found)
        self._show_changed_files(serial, cwd, found)

    def _show_changed_files(
        self,
        serial: int,
        cwd: str,
        found: tuple[str, list[tuple[str, str, bool]]] | None,
        chosen: int = 0,
    ) -> None:
        """Offer a folder's listing, with an explicit Refresh in place."""
        if not self or serial != self._changed_files_serial:
            return
        if found is None:
            self._announce("This folder is not in a git repository, so changes cannot be listed")
            return
        root, files = found
        if not files:
            self._announce("No changed files since the last commit")
            return
        n = len(files)
        refresh = "Refresh the list (asks Git)"
        while True:
            with wx.SingleChoiceDialog(
                self,
                f"{n} changed {'file' if n == 1 else 'files'} since the last commit. "
                "Enter reads a file's changes; Refresh re-checks Git.",
                "Changed Files",
                [label for _path, label, _new in files] + [refresh],
            ) as dlg:
                dlg.SetSelection(chosen)
                if dlg.ShowModal() != wx.ID_OK:
                    break
                chosen = dlg.GetSelection()
            if chosen == n:
                forget_changed_files(cwd)
                self._load_changed_files(cwd)
                return
            path, _label, new = files[chosen]
            self._load_changed_file(serial, cwd, root, path, new, files, chosen)
            return
        self.prompt.SetFocus()

    def _load_changed_file(
        self,
        serial: int,
        cwd: str,
        root: str,
        path: str,
        new: bool,
        files: list[tuple[str, str, bool]],
        chosen: int,
    ) -> None:
        """Read a selected file or its Git diff without blocking the event loop."""

        def work() -> None:
            try:
                if new:
                    with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                        text = "New file:\n" + fh.read()
                else:
                    text = readable_diff(_git(root, "diff", "HEAD", "--", path).stdout)
            except (OSError, subprocess.SubprocessError) as exc:
                text = f"Could not read the file: {exc}"
            _call_after_alive(
                self._changed_file_loaded,
                serial,
                cwd,
                root,
                path,
                files,
                chosen,
                text,
            )

        threading.Thread(target=work, daemon=True).start()

    def _changed_file_loaded(
        self,
        serial: int,
        cwd: str,
        root: str,
        path: str,
        files: list[tuple[str, str, bool]],
        chosen: int,
        text: str,
    ) -> None:
        """Show a completed read only if its tab and request are still current."""
        if not self or serial != self._changed_files_serial or cwd != self.cwd:
            return
        view = ReadView(self, text or "No changes to show", path, monospace=True)
        try:
            view.ShowModal()
        finally:
            view.Destroy()
        self._show_changed_files(serial, cwd, (root, files), chosen)

    def _show_status(self, backend: str, report: str) -> None:
        """Put the finished report on screen, once the probe has answered."""
        if not self:  # tab closed while the probe was running
            return
        if normalize_backend(backend) != self.selected_backend():
            # The backend was switched while this was being read, so the report
            # is about one that is no longer selected. Say so rather than
            # presenting it as the status of what is chosen now.
            self._announce(
                f"Backend changed while {backend_label(backend)} status was being read. "
                "Send /status again."
            )
            return
        text = "\n".join([report, "", *self._session_status_lines()])
        dlg = ReadView(self, text, "Status")
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        self.prompt.SetFocus()

    def set_model(self, model: str, effort: str = "") -> None:
        """Apply the model / effort to every message sent from here on."""
        if model == "auto" and self.selected_backend() == BACKEND_COMMANDCODE:
            model = AUTO_MODEL
        if self.selected_backend() == BACKEND_FREEBUFF and model != self.model:
            self._session_id = None
            self._drop_held_backends()
            self._announce("FreeBuff model changed; the next message starts a new conversation.")
            # Whatever terminal was waiting was started on the old model, and
            # FreeBuff reads that at launch, so it cannot serve the new one.
            discard_freebuff_prewarm()
            prewarm_freebuff(self.cwd, None, model)
        effort_changed = self.selected_backend() == BACKEND_HERMES and effort != self.effort
        self.model = model
        self.effort = effort
        self._auto_route = ""
        self._auto_check_announced = False
        note = ""
        if effort_changed:
            # Hermes fixes the reasoning level when the conversation is created
            # and offers no way to move a live one onto another: its own
            # /reasoning command runs in a separate worker process and does not
            # reach the running agent (measured). Rather than accept the pick
            # and quietly not apply it, the conversation is ended here, so the
            # next message starts one that really does use the chosen level.
            # The model needs none of this -- /model does reach a live session.
            self._session_id = None
            self._drop_held_backends()
            note = " Hermes fixes the reasoning level per conversation, so this starts a new one."
        if model == AUTO_MODEL:
            note += " Auto may switch providers, including free models, for each task."
        self._announce(f"Using {self._model_summary()} from your next message.{note}")
        self.prompt.SetFocus()

    def cycle_mode(self) -> None:
        """Quick-cycle the everyday subset (full auto → accept edits → plan).

        Full auto comes first, and is where a mode outside the subset lands, so
        the chord always has a way back to the mode nothing interrupts.
        """
        if self.mode in _CYCLE_VALUES:
            nxt = _CYCLE_VALUES[(_CYCLE_VALUES.index(self.mode) + 1) % len(_CYCLE_VALUES)]
        else:
            nxt = _CYCLE_VALUES[0]
        self._set_mode(nxt)

    # ----- Attachments -----
    def attach_files(self) -> None:
        """Pick files to attach (Attach button / Cmd-Ctrl+Shift+A)."""
        with wx.FileDialog(
            self,
            "Attach files",
            defaultDir=self.cwd,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self._add_attachments(dlg.GetPaths())

    def _pick_slash_command(self, refresh: bool = False) -> None:
        """Slash-command picker: choose a command to insert into the prompt.

        The built-in list opens at once. Anything the backend has to be asked
        for is probed on a worker and filled into the open picker; a backend
        and folder's last answer is reused until Refresh asks again.
        """
        backend = self.selected_backend()
        cwd = self.cwd
        if refresh:
            forget_slash_commands(backend, cwd)
        cached = None if refresh else cached_slash_commands(backend, cwd)
        if cached is not None:
            self._show_slash_commands(backend, cwd, list(cached), probe=False)
            return
        self._announce("Looking for this backend's slash commands…")
        commands = _static_slash_commands_for_backend(backend)
        self._show_slash_commands(backend, cwd, commands, probe=True)

    def _show_slash_commands(
        self,
        backend: str,
        cwd: str | None,
        commands: list[tuple[str, str]],
        probe: bool,
    ) -> None:
        """Open the picker, and let a worker fill it while it is open."""
        serial = self._slash_commands_serial + 1
        self._slash_commands_serial = serial
        shown = list(commands)
        dlg = SlashCommandDialog(
            self,
            "Choose a slash command. It will be placed in the prompt ready to send.",
            _slash_command_labels(shown),
        )
        self._slash_dialog = dlg
        try:
            if probe:
                self._load_slash_commands(serial, backend, cwd, dlg, shown)
            chosen = dlg.GetSelection() if dlg.ShowModal() == wx.ID_OK else -1
        finally:
            self._slash_dialog = None
            dlg.Destroy()
        if not (0 <= chosen < len(shown)):
            if chosen == len(shown):
                self._pick_slash_command(refresh=True)
            else:
                self.prompt.SetFocus()
            return
        cmd_text = shown[chosen][0]
        # Strip the placeholder hint (e.g. "[message]", "[model-id]") so the
        # inserted text is the raw command; user can append arguments if needed.
        cmd_text = cmd_text.split(" [")[0]
        # ChangeValue fires no EVT_TEXT, so this is not read back as dictation.
        self.prompt.ChangeValue(cmd_text)
        self._prompt_text = cmd_text
        self.prompt.SetInsertionPointEnd()
        self.prompt.SetFocus()
        self._announce(f"Slash command: {cmd_text}. Edit if needed, then press Enter to send.")

    def _load_slash_commands(
        self,
        serial: int,
        backend: str,
        cwd: str | None,
        dlg: SlashCommandDialog,
        shown: list[tuple[str, str]],
    ) -> None:
        """Ask this backend for its own commands on a worker."""

        def work() -> None:
            found: list[tuple[str, str]] = []
            error = ""
            try:
                if normalize_backend(backend) == BACKEND_COMMANDCODE:
                    # The agent's list is fetched on its own worker; wait for it
                    # here so this probe returns the complete answer to cache.
                    from commandcode_backend import commandcode_agent_commands

                    commandcode_agent_commands(cwd, wait=True)
                found = _slash_commands_for_backend(backend, cwd)
            except (OSError, subprocess.SubprocessError) as exc:
                error = str(exc)
            _call_after_alive(
                self._slash_commands_loaded,
                serial,
                backend,
                cwd,
                dlg,
                shown,
                found,
                error,
            )

        threading.Thread(target=work, daemon=True).start()

    def _slash_commands_loaded(
        self,
        serial: int,
        backend: str,
        cwd: str | None,
        dlg: SlashCommandDialog,
        shown: list[tuple[str, str]],
        found: list[tuple[str, str]],
        error: str,
    ) -> None:
        """Fill a completed probe, unless its tab or request is no longer current."""
        if (
            not self
            or serial != self._slash_commands_serial
            or normalize_backend(backend) != self.selected_backend()
            or cwd != self.cwd
        ):
            return
        if error:
            self._announce(f"Error: could not read slash commands: {error}")
            return
        remember_slash_commands(backend, cwd, found)
        if dlg is not getattr(self, "_slash_dialog", None):
            return
        shown[:] = found
        dlg.SetLabels(_slash_command_labels(shown))

    def _pick_saved_prompt(self) -> None:
        """Saved Prompts: put one in the prompt at the caret, over any selection."""
        dlg = SavedPromptsDialog(self, self.prompt.GetValue())
        try:
            if dlg.ShowModal() != wx.ID_OK or not dlg.chosen:
                self.prompt.SetFocus()
                return
            chosen = dlg.chosen
        finally:
            dlg.Destroy()
        start, end = self.prompt.GetSelection()
        value = self.prompt.GetValue()
        # ChangeValue fires no EVT_TEXT, so this is not read back as dictation.
        self.prompt.ChangeValue(value[:start] + chosen + value[end:])
        self._prompt_text = self.prompt.GetValue()
        self.prompt.SetInsertionPoint(start + len(chosen))
        self.prompt.SetFocus()
        self._announce("Prompt inserted. Edit if needed, then press Enter to send.")

    def _save_prompt_and_send(self) -> None:
        """Ctrl+Shift+Enter: keep this message as a saved prompt, then send it.

        Escape at the name neither saves nor sends.
        """
        text = self.prompt.GetValue()
        if not text.strip():
            self._announce("Error: Type a message first")
            return
        with wx.TextEntryDialog(
            self, "Name for the saved prompt:", "Send and Save", " ".join(text.split()[:5])
        ) as ask:
            if ask.ShowModal() != wx.ID_OK or not ask.GetValue().strip():
                self.prompt.SetFocus()
                return
            name = ask.GetValue().strip()
        if _store_saved_prompts([*_saved_prompts(), {"name": name, "text": text}]):
            self._announce(f"Prompt saved: {name}")
        else:
            self._announce("Saved prompts could not be written. Sending anyway.")
        self.prompt.SetFocus()
        self._on_send()

    def response_text(self) -> str:
        """The answer text of the selected response, or of the newest one."""
        number = 0
        sel = self._selected_row()
        if sel != wx.NOT_FOUND and self._responses_ctrl().HasFocus():
            number = self._displayed[sel].response_number
        headers = [row for row in self._rows if row.kind == "header"]
        if number:
            headers = [row for row in headers if row.response_number == number]
        for row in reversed(headers):
            text = row.source if row.source else row.payload
            if text.strip():
                return text
        return ""

    def deliver(self, text: str) -> bool:
        """Send `text` as if typed here, keeping any draft. False if it could not go."""
        if self._run_in_progress() and self._session_backend not in _QUEUEING_BACKENDS:
            return False
        draft, files = self.prompt.GetValue(), self._attachments
        self.prompt.SetValue(text)
        self._attachments = []
        try:
            if self._run_in_progress():
                before = len(getattr(self, "_pending_messages", []))
                self._queue_message()
                return len(getattr(self, "_pending_messages", [])) > before
            self._on_send()
            return self._worker is not None
        finally:
            self.prompt.SetValue(draft)
            self._attachments = files

    def _add_attachments(self, paths) -> None:
        added = 0
        for path in paths:
            ap = os.path.abspath(path)
            if ap not in self._attachments:
                self._attachments.append(ap)
                added += 1
        if not added:
            return
        names = ", ".join(os.path.basename(p) for p in self._attachments)
        count = len(self._attachments)
        self._announce(
            f"Attached {count} file{'' if count == 1 else 's'}: {names}. Send to upload."
        )

    def _try_paste_attachment(self) -> bool:
        """If the clipboard holds files or an image, attach them and report True.

        Files copied in Finder/Explorer arrive as filenames; a screenshot (or any
        copied image) arrives as a bitmap, which we save to a temp PNG and attach.
        Plain text returns False so the normal paste proceeds.
        """
        if not wx.TheClipboard.Open():
            return False
        try:
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_FILENAME)):
                file_data = wx.FileDataObject()
                if wx.TheClipboard.GetData(file_data):
                    files = [f for f in file_data.GetFilenames() if os.path.isfile(f)]
                    if files:
                        self._add_attachments(files)
                        return True
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_BITMAP)):
                bmp_data = wx.BitmapDataObject()
                if wx.TheClipboard.GetData(bmp_data):
                    bmp = bmp_data.GetBitmap()
                    if bmp.IsOk():
                        path = self._save_clipboard_image(bmp)
                        if path:
                            self._add_attachments([path])
                            return True
        finally:
            wx.TheClipboard.Close()
        return False

    @staticmethod
    def _save_clipboard_image(bmp: wx.Bitmap) -> str | None:
        fd, path = tempfile.mkstemp(prefix="blindpilot-paste-", suffix=".png")
        os.close(fd)
        if bmp.ConvertToImage().SaveFile(path, wx.BITMAP_TYPE_PNG):
            return path
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    # ----- Status forwarding -----
    def _set_status(self, text: str) -> None:
        self.last_status = text
        self._on_status(self, text)

    def _come_forward(self) -> None:
        """Raise the window with this tab showing, from a notification."""
        if not self:
            return
        frame = cast("MainFrame | None", wx.GetTopLevelParent(self))
        if frame is None:
            return
        frame.Iconize(False)
        frame.Raise()
        if getattr(frame, "_app_mode", APP_MODE_AGENT) != APP_MODE_AGENT:
            # Chat hides every session tab; the turn that called is one of them.
            frame._set_app_mode(APP_MODE_AGENT)
        notebook = getattr(frame, "notebook", None)
        if notebook is not None and notebook.FindPage(self) != wx.NOT_FOUND:
            notebook.SetSelection(notebook.FindPage(self))
        self.focus_prompt()

    def _announce(self, text: str, urgent: bool = False) -> None:
        """Speak a confirmation and mirror it to the status bar as a fallback."""
        announce(text, urgent=urgent)
        self._set_status(text)

    # ----- Prompt focus / key handling -----
    def _on_prompt_focus(self, event: wx.FocusEvent) -> None:
        event.Skip()
        if screen_speech._MAC_ANNOUNCE:
            wx.CallAfter(announce, "Prompt, edit text")

    def _on_prompt_text_changed(self, event: wx.CommandEvent) -> None:
        """Arrange to read back text that nothing else will have spoken.

        Dictation puts words in the prompt with no keystrokes, so the screen
        reader stays silent and there is no way to tell what landed. Typing is
        the opposite: every character has already been echoed, and this used to
        fire for that too, so pausing to think for a second and a half read the
        whole prompt back over the top of it.

        One more character is a keystroke. Bulk is dictation or a paste.
        """
        event.Skip()
        text = self.prompt.GetValue()
        before, self._prompt_text = self._prompt_text, text
        if self._dictation_timer is not None:
            self._dictation_timer.Stop()
            self._dictation_timer = None
        if len(text) - len(before) <= 1:
            # Typed, or deleted. Either way it has been spoken already, and a
            # pending read-back is now stale: carrying on by hand means the
            # screen reader is echoing again.
            self._dictation_pending = ""
            return
        # Successive chunks of one utterance land as separate events, so they
        # accumulate and are read once when the dictating stops.
        if text.startswith(before):
            self._dictation_pending += text[len(before) :]
        else:
            self._dictation_pending = text
        self._dictation_timer = wx.CallLater(_DICTATION_PAUSE_MS, self._read_prompt_text)

    def _read_prompt_text(self) -> None:
        if not self:
            # A deferred callback outliving its panel. `wx.CallLater` holds a
            # reference, so this still runs after the tab is gone, and reading
            # a destroyed control raises rather than returning nothing.
            return
        self._dictation_timer = None
        # What arrived, not the whole prompt: dictating a second sentence onto
        # a long one should not replay the first.
        text = (self._dictation_pending or self.prompt.GetValue()).strip()
        self._dictation_pending = ""
        if text:
            announce(text)

    def _on_prompt_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_TAB and event.ShiftDown() and self._row_count() == 0:
            # An empty native ListBox exposes transient invalid accessibility
            # children on Windows. There is nothing to visit, so cross the
            # page boundary directly instead of making NVDA announce
            # "Responses, list, unknown" and log accRole failures.
            self.focus_above_responses()
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if event.ShiftDown() and event.CmdDown():
                self._save_prompt_and_send()
                return
            if event.ShiftDown():
                event.Skip()  # default: insert newline
                return
            self._on_send()
            return
        if key == wx.WXK_UP and (event.CmdDown() or event.AltDown()):
            # Deliberately a modifier, not a bare Up. Up used to enter the
            # newest response whenever the caret was on the first line, which
            # made a multi-line prompt unreadable: reviewing what you had just
            # typed, or moving back up through a dictated paragraph, threw focus
            # out of the field the moment the caret reached the top line. In a
            # screen reader that is worse than a missing shortcut, because the
            # text is still there and the caret is not.
            #
            # Nothing is lost: the responses are reachable by Ctrl+R (which has
            # its own menu entry saying so) and by Shift+Tab, and Up/Down inside
            # the prompt now do only what every other multi-line field does.
            if self._row_count() > 0:
                self._focus_row(self._row_count() - 1)
                return
        if key == ord("V") and (event.CmdDown() or event.ControlDown()) and not event.AltDown():
            # Paste of a file or image becomes an attachment; plain text pastes
            # normally.
            if self._try_paste_attachment():
                return
        event.Skip()

    def _on_send_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_send()
            return
        event.Skip()

    # ----- Mid-run questions -----
    def _ask_questions(self, questions: Sequence[Question]) -> list[list[str]] | None:
        """Put a backend's question to the user and wait for the answer.

        Called on the worker thread, and blocks it: the turn that asked is
        waiting, and every adapter needs the answer on the thread that read the
        question. The dialog itself has to be opened on the GUI thread, so it is
        handed there and the answer comes back through this event.

        Returns None when there is nobody to ask - the tab is closing, or the
        run is being stopped - which every adapter reports to its backend as
        the question having gone unanswered.
        """
        if not questions:
            return None
        # The backend asked properly. Whatever its answer then says, it is not
        # a turn that ended with nobody told a question was waiting.
        self._structured_question_asked = True
        answered = threading.Event()
        held: dict[str, list[list[str]] | None] = {"answers": None}

        def show() -> None:
            try:
                held["answers"] = self._show_question_dialog(questions)
            finally:
                answered.set()

        wx.CallAfter(show)
        while not answered.wait(0.2):
            if self._stopping or not self:
                # Stopped, or the tab went away, while the dialog was open. The
                # backend is about to be killed either way.
                return None
        return held["answers"]

    def _ask_permission(self, backend: str, tool: str, payload: dict, suggestions: list) -> dict:
        """Ask whether `backend` may use `tool`; the worker thread waits for it.

        Returns the body the backend takes back: allow (with the rules it
        suggested when the answer was "for this session") or deny with a
        reason it reads. Stopped, or the tab gone, it is a denial.
        """
        answered = threading.Event()
        held: dict[str, dict] = {"answer": {"behavior": "deny", "message": "Nobody answered."}}

        def show() -> None:
            try:
                if self:
                    held["answer"] = self._show_permission_dialog(
                        backend, tool, payload, suggestions
                    )
                    allowed = held["answer"].get("behavior") == "allow"
                    asked = "approve its plan" if tool == "ExitPlanMode" else f"use {tool}"
                    self.decisions.append(
                        (
                            f"{backend_label(backend)} asked to {asked}",
                            "You allowed it" if allowed else "You denied it",
                        )
                    )
            finally:
                answered.set()

        wx.CallAfter(show)
        while not answered.wait(0.2):
            if self._stopping or not self:
                return {"behavior": "deny", "message": "The turn was stopped.", "interrupt": True}
        return held["answer"]

    def _show_permission_dialog(
        self, backend: str, tool: str, payload: dict, suggestions: list
    ) -> dict:
        """Open the permission or plan dialog. GUI thread only."""
        plan = tool == "ExitPlanMode"
        label = backend_label(backend)
        asking = (
            f"{label} has a plan for you to approve" if plan else f"{label} wants to use {tool}"
        )
        self._announce(asking, urgent=True)
        # The turn waits on this, so someone in another window needs to know.
        _notify_if_away(self, asking)
        self._earcons.stop_progress()
        self._hide_working()
        dlg = PermissionDialog(self, backend, tool, payload, bool(suggestions))
        try:
            choice = dlg.ShowModal()
            reason = dlg.reason()
        finally:
            dlg.Destroy()
            if self._worker is not None:
                self._earcons.start_progress()
                self._show_working()
        if plan and choice in (PermissionDialog.APPROVE_EDITS, PermissionDialog.APPROVE_ASK):
            mode = "acceptEdits" if choice == PermissionDialog.APPROVE_EDITS else "default"
            # Later turns run in the mode chosen, not back in plan mode.
            wx.CallAfter(self._set_mode, mode)
            update = {"type": "setMode", "mode": mode, "destination": "session"}
            return {"behavior": "allow", "updatedPermissions": [update]}
        if choice == PermissionDialog.ALLOW:
            self._announce("Allowed")
            return {"behavior": "allow"}
        if choice == PermissionDialog.ALLOW_SESSION:
            self._announce("Allowed for this session")
            # Claude Code often suggests saving the rule to settings.local.json.
            # "For this session" means this session: every update is kept in
            # memory only, never written to a settings file.
            for_session = [
                {**update, "destination": "session"}
                for update in suggestions
                if isinstance(update, dict)
            ]
            # Keep a readable record of the grant for Chat Permissions; the
            # grant itself stays in the backend's session, this is only the
            # list of what was handed over.
            granted = _permission_update_lines(suggestions)
            if granted:
                mine: list[str] | None = getattr(self, "_session_permission_rule_lines", None)
                if mine is None:
                    mine = []
                    self._session_permission_rule_lines = mine
                for line in granted:
                    if line not in mine:
                        mine.append(line)
                _remember_session_rules(self._session_id or "", granted)
            return {"behavior": "allow", "updatedPermissions": for_session}
        if plan:
            self._announce("Kept planning")
            message = "The user wants to keep planning."
        else:
            self._announce("Denied")
            message = f"The user denied {tool}."
        if reason:
            message += f" They said: {reason}"
        return {"behavior": "deny", "message": message}

    def _show_question_dialog(
        self, questions: Sequence[Question], *, ended_turn: bool = False
    ) -> list[list[str]] | None:
        """Open the question dialog. GUI thread only."""
        if not self:
            return None
        backend = self._session_backend or self.selected_backend()
        self._announce(f"{backend_label(backend)} is asking a question")
        _notify_if_away(self, f"{backend_label(backend)} is asking you a question")
        # The progress loop means "still working", and it is not: the run is
        # waiting on this dialog, and a loop under a question is only noise.
        self._earcons.stop_progress()
        self._hide_working()
        dlg = QuestionDialog(
            self, backend_label(backend), questions, announce=announce, ended_turn=ended_turn
        )
        self._question_dialog = dlg
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self._announce("Question left unanswered")
                for question in questions:
                    self.decisions.append(
                        (f"{backend_label(backend)} asked: {question.question}", "Left unanswered")
                    )
                return None
            answers = dlg.answers()
            for question, answer in zip(questions, answers, strict=False):
                self.decisions.append(
                    (
                        f"{backend_label(backend)} asked: {question.question}",
                        "You answered: " + (", ".join(answer) or "nothing"),
                    )
                )
        finally:
            self._question_dialog = None
            dlg.Destroy()
            if self._worker is not None:
                self._earcons.start_progress()
                self._show_working()
        self._announce("Answer sent")
        return answers

    def _ask_question_left_in_the_answer(self, question: str | Sequence[Question]) -> None:
        """Put a question the turn only wrote down in front of the person.

        The same dialog a backend's own question tool opens, because it is the
        same thing happening: a turn wants an answer. The difference is that
        this turn has already ended, so there is nothing holding a reply — what
        is typed is sent as the next turn, which is what typing it into the
        message box would have done.

        What arrives is either a block the turn wrote, which carries its own
        questions and their options, or a bare question read out of prose,
        which has nothing to offer but a text field.
        """
        if not self or self._run_in_progress():
            # Something was sent between the turn ending and this being posted.
            # That message is the answer now, whatever it says.
            return
        questions = (Question(question=question),) if isinstance(question, str) else tuple(question)
        if not questions:
            return
        answers = self._show_question_dialog(questions, ended_turn=True)
        if not answers:
            return
        # One line per question, in the order they were asked: the order every
        # provider that takes a list of answers here already uses.
        said = [
            "; ".join(part.strip() for part in (group or []) if part.strip()) for group in answers
        ]
        answer = "\n".join(part for part in said if part).strip()
        if not answer or not self.prompt:
            return
        # The answer goes out through the message box, which may already hold
        # something typed while the turn was running. That draft is put back
        # once the answer has gone, rather than being sent in its place or lost.
        draft = self.prompt.GetValue()
        self.prompt.SetValue(answer)
        self._on_send()
        if draft.strip() and not self.prompt.GetValue():
            self.prompt.SetValue(draft)
            self.prompt.SetInsertionPointEnd()

    def _close_question_dialog(self) -> None:
        """Take down an open question, because the run it belongs to is going.

        For a tab closing or the app quitting. The dialog is modal, so Stop
        cannot be pressed while it is open; `_on_stop` calls this only for
        completeness.
        """
        dlg = self._question_dialog
        if dlg is not None:
            self._question_dialog = None
            dlg.EndModal(wx.ID_CANCEL)

    # ----- Worker-to-GUI event mailbox -----
    def _worker_complete(self, text: str) -> None:
        """Queue a finished answer, unless it is the reply to Stop.

        Muse, Command Code and FreeBuff end a stopped turn through the same
        callback as a finished one. Taken as "the turn beat the cancellation",
        it un-muted narration and read the partial answer aloud, which sounds
        exactly like a Stop that did nothing. Decided here, on the worker's
        thread as the answer lands, so an answer that really did arrive
        before Stop is still delivered; the stopped turn keeps what streamed.
        """
        if not self._stopping:
            self._queue_worker_event("complete", text)

    def _queue_worker_event(self, name: str, *args: object) -> None:
        """Queue one worker callback without flooding wx's event loop.

        Every backend invokes callbacks from its worker thread. A single
        scheduled drain preserves their order while allowing a long stream to
        accumulate in this mailbox instead of as thousands of native GUI
        events.
        """
        with self._worker_event_lock:
            self._worker_events.append((name, args))
            if self._worker_events_scheduled:
                return
            self._worker_events_scheduled = True
        _call_after_alive(self._drain_worker_events)

    def _drain_worker_events(self) -> None:
        """Apply a short batch, then yield to keyboard and accessibility events."""
        if not self:
            with self._worker_event_lock:
                self._worker_events.clear()
                self._worker_events_scheduled = False
            return

        started = time.monotonic()
        handled = 0
        rows_changed = False
        while handled < _WORKER_EVENT_BATCH_SIZE:
            if handled and time.monotonic() - started >= _WORKER_EVENT_BUDGET_SECONDS:
                break
            with self._worker_event_lock:
                if not self._worker_events:
                    break
                name, args = self._worker_events.popleft()

            if name == "activity":
                self._on_activity(str(args[0]), str(args[1]), refresh=False)
                rows_changed = True
            else:
                # Make preceding streamed rows visible before a status or
                # terminal event that logically follows them.
                if rows_changed:
                    self._refresh_list()
                    rows_changed = False
                if name == "session":
                    self._on_session_started(str(args[0]))
                elif name == "started":
                    self._announce("Receiving response")
                elif name == "complete":
                    self._on_response_complete(str(args[0]))
                elif name == "failed":
                    self._on_failed(str(args[0]))
                elif name == "subagent":
                    self._on_subagent(*(str(arg) for arg in args))
                elif name == "tasks":
                    self._on_tasks(*args)
                elif name == "remote":
                    self._on_remote(cast(dict, args[0]))
                elif name == "done":
                    self._on_worker_finished()
                elif name == "late_turn":
                    # The mailbox carries plain objects. This one is the
                    # generation `_claude_worker_extra` put in it.
                    self._start_late_turn(cast(int, args[0]))
            handled += 1

        if rows_changed:
            self._refresh_list()

        with self._worker_event_lock:
            pending = bool(self._worker_events)
            if not pending:
                self._worker_events_scheduled = False
        if pending:
            # Posting at the back of the native queue lets arrow, Tab, paint,
            # and screen-reader events already waiting run before the next batch.
            wx.CallAfter(self._drain_worker_events)

    # ----- Subagents -----
    def _subagents_shown(self) -> bool:
        return self.subagents.IsShown() and self.subagents.GetCount() > 0

    def _on_subagent(self, agent_id: str, name: str, status: str, line: str) -> None:
        """A worker's report that one of its agents started, changed, or acted."""
        entry = self._subagents.get(agent_id)
        before = entry.status if entry is not None else ""
        if entry is None:
            entry = SubagentEntry(name=name or "Subagent", status=status or SUBAGENT_RUNNING)
            self._subagents[agent_id] = entry
        else:
            if name:
                entry.name = name
            if status:
                entry.status = status
        if line and entry.lines and entry.lines[-1] == line:
            # Providers that re-send a whole item on every change repeat
            # its last line; the log says each thing once.
            line = ""
        if line:
            entry.lines.append(line)
            del entry.lines[:-SUBAGENT_LOG_LINES]
        view = self._subagent_view
        if view is not None and view.agent_id == agent_id:
            view.update(entry, line)
        if entry.status != before:
            if not before:
                self._say(f"Subagent started: {entry.name}", "tool")
            elif entry.status != SUBAGENT_RUNNING:
                self._say(f"Subagent {entry.status}: {entry.name}", "tool")
        self._refresh_subagents()

    def _refresh_subagents(self) -> None:
        """Bring the list up to date, and show it only while an agent runs.

        Rows are changed in place rather than the list rebuilt, so the row a
        reader is on keeps its place and a screen reader is not handed a new
        list for every line an agent logs.
        """
        ids = list(self._subagents)
        labels = [self._subagents[agent_id].label() for agent_id in ids]
        if ids[: len(self._subagent_ids)] != self._subagent_ids:
            self.subagents.Set(labels)
        else:
            count = self.subagents.GetCount()
            for index, label in enumerate(labels):
                if index >= count:
                    self.subagents.Append(label)
                elif self.subagents.GetString(index) != label:
                    self.subagents.SetString(index, label)
        self._subagent_ids = ids
        if labels and self.subagents.GetSelection() == wx.NOT_FOUND:
            self.subagents.SetSelection(0)
        show = any(entry.status == SUBAGENT_RUNNING for entry in self._subagents.values())
        if show == self.subagents.IsShown():
            return
        had_focus = self.subagents.HasFocus()
        self.subagents_label.Show(show)
        self.subagents.Show(show)
        self.Layout()
        if had_focus and not show:
            # The list left the Tab order under the reader. The conversation
            # is the next thing in it.
            self.focus_first_control(past_subagents=True)

    def _prune_subagents(self) -> None:
        """Let go of the agents that are finished, keeping the running ones."""
        finished = [i for i, e in self._subagents.items() if e.status != SUBAGENT_RUNNING]
        if not finished:
            return
        for agent_id in finished:
            del self._subagents[agent_id]
        self._refresh_subagents()

    def _forget_subagents(self) -> None:
        self._subagents.clear()
        self._refresh_subagents()

    def _settle_subagents(self) -> None:
        """Close out agents the ended turn never reported finished.

        Their process or connection has gone with the turn, so they are not
        running whatever the last report said -- except on a backend whose
        agents outlive the turn that started them, which reports their end
        when it comes.
        """
        if self._subagents_outlive_turn:
            return
        ending = SUBAGENT_STOPPED if self._stopping else SUBAGENT_ENDED
        changed = False
        for agent_id, entry in self._subagents.items():
            if entry.status == SUBAGENT_RUNNING:
                entry.status = ending
                changed = True
                view = self._subagent_view
                if view is not None and view.agent_id == agent_id:
                    view.update(entry, "")
        if changed:
            self._refresh_subagents()

    def _on_subagents_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._open_subagent()
            return
        event.Skip()

    def _open_subagent(self) -> None:
        """Show the chosen agent's activity, kept current while it is open."""
        index = self.subagents.GetSelection()
        if not 0 <= index < len(self._subagent_ids):
            return
        agent_id = self._subagent_ids[index]
        entry = self._subagents.get(agent_id)
        if entry is None:
            return
        dialog = SubagentDialog(self, agent_id, entry)
        self._subagent_view = dialog
        try:
            dialog.ShowModal()
        finally:
            self._subagent_view = None
            dialog.Destroy()
        if self._subagents_shown():
            self.subagents.SetFocus()
        else:
            self.focus_first_control(past_subagents=True)

    # ----- Send flow -----
    def _run_in_progress(self) -> bool:
        """Whether a turn is still going, as far as this window is concerned.

        Deliberately not `is_alive()`: the worker thread dies as soon as it
        has queued its last event, while `complete` and `done` may still be
        in the mailbox. `_worker` is cleared on this thread when `done`
        drains, so it agrees with the rest of the state these handlers read.
        """
        return self._worker is not None

    def send_now(self) -> None:
        """Public entry point so the frame can fire a seeded side-chat prompt."""
        self._on_send()

    def _queue_message(self, steering: bool = False) -> None:
        text = self.prompt.GetValue().strip()
        if not text and not self._attachments:
            self._announce("Error: Type a message or attach a file first")
            return
        if self.selected_backend() != self._session_backend:
            self._announce("Error: Switch back to the running backend before queuing a message")
            return
        pending = getattr(self, "_pending_messages", [])
        item = (text, list(self._attachments), self._session_backend)
        if steering:
            pending.insert(0, item)
            if not self._session_id and self._turns:
                self._steering_context = getattr(self, "_active_send_text", self._turns[-1].prompt)
        else:
            pending.append(item)
        self._pending_messages = pending
        self.prompt.SetValue("")
        self._attachments = []
        self._earcons.play_send(self._session_backend)
        if steering:
            if self._worker is not None and self._worker.is_alive() and not self._stopping:
                self._on_stop()
            self._queue_paused = False
            self._announce(f"Steering: stopping and resuming with your instruction. {text}")
        else:
            self._announce(f"Message queued. {len(pending)} waiting. {text}")

    def _send_queued_message(self) -> None:
        pending = getattr(self, "_pending_messages", [])
        if self._run_in_progress() or not pending or getattr(self, "_queue_paused", False):
            return
        text, attachments, backend = pending[0]
        if backend != self.selected_backend() or backend != self._session_backend:
            self._queue_paused = True
            self._announce(
                "Queue paused because the backend changed. Switch back and use /queue resume"
            )
            return
        pending.pop(0)
        context = getattr(self, "_steering_context", "")
        self._steering_context = ""
        if context and not self._session_id:
            text = f"Original task:\n{context}\n\nUpdated instruction:\n{text}"
        # Do not replace a draft the person has been typing while waiting.
        draft, draft_files = self.prompt.GetValue(), self._attachments
        self.prompt.SetValue(text)
        self._attachments = attachments
        try:
            self._on_send()
            if self._worker is None:
                pending.insert(0, (text, attachments, backend))
                self._queue_paused = True
        finally:
            self.prompt.SetValue(draft)
            self._attachments = draft_files

    def _commandcode_command(self, raw: str) -> bool:
        """Dispatch Command Code equivalents without sending terminal commands as prompts."""
        if not raw.startswith("/"):
            return False
        parts = raw.split(maxsplit=1)
        name, argument = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""
        aliases = {"/sessions": "/resume", "/quit": "/exit"}
        if name in aliases and not argument:
            self.prompt.SetValue(aliases[name])
            return False
        if name in {
            "/btw",
            "/clear",
            "/new",
            "/compact",
            "/exit",
            "/model",
            "/models",
            "/resume",
            "/status",
        }:
            return False
        if name == "/queue":
            action = argument.lower()
            if action in ("", "list"):
                pending = getattr(self, "_pending_messages", [])
                text = "\n".join(
                    f"{i}. {p or 'Attached files'}"
                    for i, (p, _files, _backend) in enumerate(pending, 1)
                )
                self._announce(text or "No queued messages")
            elif action == "clear":
                self._pending_messages = []
                self._steering_context = ""
                self._queue_paused = False
                self._announce("Queued messages cleared")
            elif action == "resume":
                self.prompt.SetValue("")
                self._queue_paused = False
                if not getattr(self, "_pending_messages", []):
                    self._announce("No queued messages")
                else:
                    self._send_queued_message()
                return True
            elif self._run_in_progress():
                self.prompt.SetValue(argument)
                self._queue_message()
                return True
            else:
                self.prompt.SetValue(argument)
                return False
        elif name == "/steer":
            if not argument:
                self._announce("Usage: /steer followed by your instruction")
                return True
            self.prompt.SetValue(argument)
            self._on_steer()
            return True
        elif name == "/stop":
            self._on_stop()
        elif name == "/effort":
            from commandcode_backend import COMMANDCODE_EFFORTS

            if not argument:
                self._announce(
                    f"Reasoning effort: {self.effort or self._cli_effort or 'CLI default'}"
                )
            elif argument.lower() in COMMANDCODE_EFFORTS:
                self.set_model(self.model, argument.lower())
            else:
                self._announce(f"Error: Choose an effort: {', '.join(COMMANDCODE_EFFORTS)}")
                return True
        elif name == "/mode" or name.startswith("/mode:") or name == "/plan":
            mode = "plan" if name == "/plan" else (name.partition(":")[2] or argument)
            modes = {
                "standard": "default",
                "default": "default",
                "auto-accept": "acceptEdits",
                "plan": "plan",
                "dont-ask": "dontAsk",
                "auto": "auto",
                "yolo": "bypassPermissions",
            }
            if not mode:
                self._announce(f"Permission mode: {_MODE_LABEL_BY_VALUE.get(self.mode, self.mode)}")
            elif mode.lower() in modes:
                self._set_mode(modes[mode.lower()])
                if name == "/plan" and argument:
                    self.prompt.SetValue(f"Plan this task without changing files:\n{argument}")
                    return False
            else:
                self._announce(f"Error: Choose a mode: {', '.join(modes)}")
                return True
        elif name == "/add-dir":
            if not argument:
                self._announce(
                    "Additional directories: "
                    + (", ".join(getattr(self, "_commandcode_dirs", ())) or "none")
                )
            else:
                directory = Path(argument.strip('"')).expanduser()
                if not directory.is_absolute():
                    directory = Path(self.cwd) / directory
                if not directory.is_dir():
                    self._announce(f"Error: Directory does not exist: {directory}")
                    return True
                self._commandcode_dirs = tuple(
                    dict.fromkeys(
                        (*getattr(self, "_commandcode_dirs", ()), str(directory.resolve()))
                    )
                )
                self._announce(f"Added directory for subsequent messages: {directory}")
        elif name in ("/login", "/connect", "/update", "/config") and not argument:
            method = "_settings_files_active" if name == "/config" else "_manage_backends"
            callback = getattr(wx.GetTopLevelParent(self), method, None)
            if callable(callback):
                wx.CallAfter(callback)
        elif name in ("/init", "/review", "/pr-comments"):
            prompts = {
                "/init": "Inspect this project and create or update AGENTS.md with accurate build, test, and coding instructions.",
                "/review": "Review the current changes for bugs and regressions. Report findings with file references; do not change files.",
                "/pr-comments": "Fetch and summarize the review comments for the pull request associated with this branch. Do not post replies or change files.",
            }
            text = (
                f"Review pull request {argument} for bugs and regressions. Do not post a review or change files."
                if name == "/review" and argument
                else prompts[name]
            )
            self.prompt.SetValue(text)
            return False
        elif name == "/copy" and not argument:
            response = next((turn.response for turn in reversed(self._turns) if turn.response), "")
            self._announce(
                "Last response copied"
                if response and _copy_to_clipboard(response)
                else "Error: No response to copy or clipboard unavailable"
            )
        elif name == "/help":
            text = "Enter queues a follow-up while Command Code is running. Steer stops and resumes with your instruction. Stop pauses queued messages.\n\n"
            text += "\n".join(
                f"{command}: {description}"
                for command, description in _slash_commands_for_backend(BACKEND_COMMANDCODE)
            )
            text += (
                "\n\nAny other slash command works too. Command Code's skills and mod commands "
                "go to Command Code as typed. Its console commands run in Command Code's own "
                "console, off screen, on this conversation, and what they show is added here. "
                "A command that opens a menu lists the choices; pick one with BlindPilot's own "
                "control for it, or in Command Code's console."
            )
            with ReadView(self, text, "Command Code commands") as dialog:
                dialog.ShowModal()
        else:
            from commandcode_backend import commandcode_agent_commands

            # Cached per folder after the first look (a couple of seconds).
            offered = {n.lower() for n, _ in commandcode_agent_commands(self.cwd, wait=True)}
            if name in offered:
                # A skill, a mod's command, /loop, /peek: Command Code's agent
                # runs these itself, so they go out as typed.
                return False
            # Everything else lives in Command Code's console: run it there.
            self.prompt.SetValue("")
            self._run_commandcode_console(raw)
            return True
        self.prompt.SetValue("")
        return True

    def _run_commandcode_console(self, command: str) -> None:
        """Run a console-only Command Code command off screen on this
        conversation and put what it showed into the conversation."""
        if self._run_in_progress():
            self._announce(f"Error: Wait for this turn to finish, then run {command}")
            return
        self._announce(f"Running {command} in Command Code's console")
        self._earcons.start_progress()
        cwd, session = self.cwd, self._session_id or ""

        def run() -> None:
            from commandcode_backend import commandcode_console_command

            try:
                text = commandcode_console_command(command, cwd, session)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                text = f"Error: {command} could not be run in Command Code's console: {exc}"
            wx.CallAfter(self._commandcode_console_done, command, text)

        threading.Thread(target=run, daemon=True).start()

    def _commandcode_console_done(self, command: str, text: str) -> None:
        if not self:
            return
        self._earcons.stop_progress()
        self._response_count += 1
        number = self._response_count
        self._turns.append(Turn(prompt=command, response=text))
        self._rows.append(
            Row(kind="you", label=f"You: {command}", payload=command, response_number=number)
        )
        self._rows.extend(parse_response(text, number))
        self._refresh_list()
        first = next((line for line in text.splitlines() if line.strip()), "")
        self._announce(f"{command}: {first}" if first else f"{command} finished")

    def _on_send(self, worker_extra: dict | None = None) -> None:
        # ``worker_extra`` carries per-turn arguments only some backends take —
        # compaction, at present. Ordinary sends pass nothing.
        #
        # "/btw [message]" opens a new side-chat tab in the same directory
        # instead of sending to this conversation.
        raw = self.prompt.GetValue().strip()
        backend = self.selected_backend()
        command = raw.split(maxsplit=1)[0].lower() if raw else ""
        if backend == BACKEND_COMMANDCODE or (
            backend in _QUEUEING_BACKENDS and command == "/queue"
        ):
            if self._commandcode_command(raw):
                return
            raw = self.prompt.GetValue().strip()
        low = raw.lower()
        if self.selected_backend() == BACKEND_FREEBUFF:
            low = {"/history": "/resume", "/usage": "/status"}.get(low, low)
        if low == "/btw" or low.startswith("/btw "):
            self.prompt.SetValue("")
            self._on_side_chat(self.cwd, raw[4:].strip())
            return
        if low in ("/clear", "/new"):
            self.prompt.SetValue("")
            self.clear_conversation()
            return
        if low == "/compact":
            self.prompt.SetValue("")
            self.compact_conversation()
            return
        # "/model" opens the picker; "/model <name>" sets it straight away.
        if low in ("/model", "/models") or low.startswith(("/model ", "/models ")):
            self.prompt.SetValue("")
            argument = raw.split(maxsplit=1)[1].strip() if " " in raw else ""
            if argument:
                parts = argument.split()
                effort = parts[1] if len(parts) > 1 else self.effort
                self.set_model(parts[0], effort)
            else:
                self.open_model_dialog(force_refresh=low == "/models")
            return
        if low == "/exit":
            self.prompt.SetValue("")
            # The frame owns the session; this panel only knows it has one
            # somewhere above it, which is why the method is looked up rather
            # than called outright.
            close_session = getattr(wx.GetTopLevelParent(self), "_close_current_session", None)
            if callable(close_session):
                wx.CallAfter(close_session)
            return
        if low == "/resume":
            self.prompt.SetValue("")
            open_history = getattr(wx.GetTopLevelParent(self), "_open_history", None)
            if callable(open_history):
                wx.CallAfter(open_history)
            return
        if low == "/connect":
            self.prompt.SetValue("")
            self.open_connect_dialog()
            return
        if low == "/status":
            self.prompt.SetValue("")
            self.open_status_dialog()
            return

        if self._run_in_progress() and self._session_backend in _QUEUEING_BACKENDS:
            self._queue_message()
            return

        if (
            self._worker is not None
            and self._worker.is_alive()
            and getattr(self._worker, "accepting_input", lambda: True)()
        ):
            # A run is already going, so Enter steers it rather than failing —
            # same thing the Steer button does.
            self._on_steer()
            return

        if self._run_in_progress():
            self._announce("Error: The current backend is still finishing the previous turn")
            return

        prompt = self.prompt.GetValue().strip()
        if not prompt and not self._attachments:
            self._announce("Error: Prompt is empty")
            return

        selected_backend = self.selected_backend()
        if selected_backend == BACKEND_MUSEAI and getattr(self, "_museai_main_required", False):
            self.open_museai_main(resume_pending=True)
            if self._museai_main_required:
                return
        if selected_backend != self._session_backend:
            if self._session_id:
                # A conversation existed here and is being left behind, so the
                # name given to it is not the new one's name -- same reasoning
                # as in ``clear_conversation``. Before the first message there
                # is nothing to leave behind, and the name still applies.
                self._session_title = ""
            self._session_id = None
            # The next backend's conversation is a new one, and takes the
            # Account Codex uses when it starts rather than the last one's.
            self._forget_codex_conversation_account()
            # A held Hermes connection belongs to the conversation being left.
            self._drop_held_backends()
            self._session_backend = selected_backend
            self.model = ""
            self.effort = ""
            self._auto_route = ""
            self._auto_check_announced = False
            self._cli_model = ""
            self._cli_effort = ""
            # The next backend's catalog is not comparable to the last one's,
            # so the first read after the switch is not a change to announce.
            self._known_models = None
            self.backend_status.SetLabel(f"Backend: {backend_label(selected_backend)}")
            self._announce(
                f"Starting a new {backend_label(selected_backend)} conversation in this tab"
            )

        send_text = self._build_send_text(prompt)
        # Attachments leave the tab's pending list here, but the turn still
        # needs them: an uploading backend is handed the paths so the worker
        # can send the bytes.
        outgoing_files = list(self._attachments)
        uploading = bool(outgoing_files) and self._backend_uploads_attachments()
        row_text = send_text
        if uploading:
            summary = self._attachment_summary()
            row_text = f"{send_text}\n{summary}" if send_text else summary
        self._turns.append(Turn(prompt=prompt))
        if len(self._turns) == 1 and not str(getattr(self, "_session_title", "") or "").strip():
            # A conversation with no name of its own is named by its first
            # message, the title Recent Conversations lists it under. A name
            # typed in the New Session dialog is never replaced: it is the one
            # thing about the conversation the person chose, and a session
            # named for its subject used to be renamed "start" by its first
            # message. getattr because stub panels in tests lack the attribute.
            self._on_title(self, make_title(prompt))
        self._assistant_narrated_this_turn = False
        self._streamed_assistant = ""
        self._stopping = False
        self._add_your_message(row_text)
        self.prompt.SetValue("")
        self._attachments = []

        self._announce(_sent_readback(prompt) if SETTINGS.read_back_sent else "Sending")
        self.send_btn.Disable()
        # Earcons: a one-shot "send", then loop "in progress" until the
        # response arrives (or the request fails).
        self._earcons.play_send(self._session_backend)
        self._earcons.start_progress()
        self._show_working()

        extra = dict(worker_extra or {})
        if selected_backend == BACKEND_HERMES:
            # No condition on "does this backend upload" here: this branch IS
            # the uploading backend. Guarding it twice would be a line no test
            # could ever hold to account.
            extra.update(self._hermes_worker_extra(outgoing_files))
        elif selected_backend == BACKEND_MUSE:
            extra.update(self._muse_worker_extra(outgoing_files))
        elif selected_backend == BACKEND_OPENCODE:
            extra.update(self._opencode_worker_extra(outgoing_files))
        elif selected_backend == BACKEND_CLAUDE:
            extra.update(self._claude_worker_extra())
        elif selected_backend == BACKEND_COMMANDCODE:
            extra["additional_dirs"] = getattr(self, "_commandcode_dirs", ())
            extra["auto_free_fallback"] = self.model == AUTO_MODEL
            extra["auto_route"] = self.model == AUTO_MODEL
            extra["auto_context_tokens"] = SessionPanel._known_context(self)[0] or None
        self._launch_turn(send_text, selected_backend, extra)

    def _codex_conversation_account(self) -> object:
        """The Account this tab's Codex conversation is bound to, taking one if new.

        A conversation stays on the Account it began on: choosing another one
        in the accounts dialog affects the next conversation, not the thread
        somebody is in the middle of. The Account is taken once, from the saved
        choice, and remembered until this tab starts a conversation of its own
        (see `_forget_codex_conversation_account`).
        """
        bound = getattr(self, "_codex_plan_account", UNBOUND)
        if bound is UNBOUND:
            bound = codex_plan_account()
            self._codex_plan_account = bound
        return bound

    def _forget_codex_conversation_account(self) -> None:
        """Let the next Codex conversation in this tab take its Account afresh."""
        self._codex_plan_account = UNBOUND

    def _set_auto_route(self, note: str) -> None:
        """Say a changed Auto route after the worker's live model check."""
        if note != getattr(self, "_auto_route", ""):
            self._auto_route = note
            self._announce(note)

    def _auto_check_status(self) -> None:
        """Show the live check each turn, speaking it once per Auto selection."""
        note = "Checking current Command Code models."
        if not self._auto_check_announced:
            self._auto_check_announced = True
            self._announce(note)
        else:
            self._set_status(note)

    def _turn_model(self, send_text: str | None, selected_backend: str) -> tuple[str, str]:
        """The explicit model, or the CLI default until a worker resolves Auto."""
        if selected_backend != BACKEND_COMMANDCODE or self.model != AUTO_MODEL:
            return self.model, self.effort
        return "", self.effort

    def _launch_turn(self, send_text: str | None, selected_backend: str, extra: dict) -> None:
        """Start the worker for one turn. `send_text` is None for a late turn,
        which has nothing to send and only reads what has already arrived."""
        self._active_send_text = send_text or ""
        self._turn_started_at: float | None = time.monotonic()
        self._turn_attached = False
        self._last_step = ""
        worker_type = worker_class(selected_backend, ClaudeWorker)
        # Agents that finished in an earlier turn have been read or ignored by
        # now; the ones still running stay, however old the turn that began
        # them.
        self._prune_subagents()
        self._subagents_outlive_turn = bool(getattr(worker_type, "subagents_outlive_turn", False))
        model, effort = SessionPanel._turn_model(self, send_text, selected_backend)
        if selected_backend == BACKEND_CODEX:
            # The Account this conversation began on, so its turns -- and any
            # resume of it -- stay there however the saved choice moves.
            extra["plan_account"] = self._codex_conversation_account()
        self._worker = worker_type(
            send_text,
            self._session_id,
            self.cwd,
            self.mode,
            model=model,
            effort=effort,
            on_session=lambda sid: self._queue_worker_event("session", sid),
            on_started=lambda: self._queue_worker_event("started"),
            on_activity=lambda kind, text: self._queue_worker_event("activity", kind, text),
            on_complete=self._worker_complete,
            on_failed=lambda msg: self._queue_worker_event("failed", msg),
            on_done=lambda: self._queue_worker_event("done"),
            on_question=self._ask_questions,
            on_subagent=lambda *report: self._queue_worker_event("subagent", *report),
            # A Claude turn brings its own, which holds the tab weakly.
            **{
                "on_permission": lambda tool, payload, suggestions: self._ask_permission(
                    selected_backend, tool, payload, suggestions
                ),
                **extra,
            },
        )
        # Set rather than passed: only the Claude and Codex workers look for it.
        cast(Any, self._worker).on_tasks = lambda *call: self._queue_worker_event("tasks", *call)
        cast(Any, self._worker).on_remote = lambda data: self._queue_worker_event("remote", data)
        if selected_backend == BACKEND_MUSEAI and send_text is None:
            from museai_worker import MuseAiWorker

            if isinstance(self._worker, MuseAiWorker):
                self._worker.follow_after = getattr(self, "_museai_follow_after", None)
        if (
            selected_backend == BACKEND_COMMANDCODE
            and self.model == AUTO_MODEL
            and send_text
            and not send_text.lstrip().startswith("/")
        ):
            SessionPanel._auto_check_status(self)
        try:
            self._worker.start()
        except RuntimeError as exc:
            # A thread that never started will never queue `done`, and `done`
            # is what says the turn is over. Clearing this by hand is what
            # keeps a failure here from leaving Send refused for good.
            self._worker = None
            self._turn_started_at = None
            self._earcons.stop_progress()
            self._hide_working()
            self.send_btn.Enable()
            self._announce(f"Error: The turn could not be started: {exc}")
            return
        self.steer_btn.Enable()
        self.stop_btn.Enable()
        if selected_backend in _QUEUEING_BACKENDS:
            self.send_btn.Enable()

    def _claude_worker_extra(self) -> dict:
        """What a Claude turn needs beyond the message, namely whose process
        it borrows and how to wake this tab when the CLI speaks with no turn
        running.

        The wake-up holds the tab weakly. The process keeps it for as long as
        the pool keeps the process, so a strong reference would mean a tab
        closed without teardown could never be collected. It carries the
        generation it was made in, so a report cannot land on a conversation
        it has nothing to do with.
        """
        tab = weakref.ref(self)
        generation = self._claude_generation

        def wake() -> None:
            panel = tab()
            if panel is not None:
                panel._queue_worker_event("late_turn", generation)

        def ask(tool: str, payload: dict, suggestions: list) -> dict:
            # Weak for the same reason as the wake-up: a closed tab is denied.
            panel = tab()
            if panel is None:
                return {"behavior": "deny", "message": "The tab was closed."}
            return SessionPanel._ask_permission(panel, BACKEND_CLAUDE, tool, payload, suggestions)

        remote = {
            "enabled": getattr(self, "remote_control", False),
            "name": getattr(self, "tab_title", "") or _tab_label("", getattr(self, "cwd", "")),
            "reattach": getattr(self, "remote_bridge", ""),
        }
        return {
            "held_for": self,
            "on_unsolicited": wake,
            "on_permission": ask,
            "remote_control": remote,
        }

    def _start_late_turn(self, generation: int) -> None:
        """Receive what the CLI says with no turn of ours running.

        An agent this tab started in the background, or resumed, has finished,
        and Claude is answering what it found. It is a turn like any other
        except that nobody typed anything, so there is no "You:" row.

        `generation` is the conversation the wake-up was queued for. The tab
        can have started a new conversation, restored one or moved to another
        backend since, and the report belongs to none of them.
        """
        if not self:
            return
        if generation != self._claude_generation or self._session_backend != BACKEND_CLAUDE:
            return
        if self._run_in_progress():
            self._late_turn_waiting = generation
            return
        self._late_turn_waiting = None
        self._assistant_narrated_this_turn = False
        self._streamed_assistant = ""
        self._stopping = False
        self._turns.append(Turn(prompt=""))
        self._announce("A background agent has reported. Receiving response")
        self.send_btn.Disable()
        self._earcons.start_progress()
        self._show_working()
        self._launch_turn(None, BACKEND_CLAUDE, self._claude_worker_extra())

    def _add_your_message(self, text: str, steering: bool = False) -> None:
        """Put the user's own message in the list, ahead of the answer to it.

        Carries the number of the response it belongs to, so both group together
        for jump-to-response and copy-whole-response. Skipped in
        silent-until-response mode and not added later either, so that
        transcript holds answers without the prompts that led to them.
        """
        if not SETTINGS.live_rows:
            return
        n = self._stream_response or self._response_count + 1
        prefix = "You, steering:" if steering else "You:"
        self._rows.append(
            Row(
                kind="you",
                label=f"{prefix} {' '.join(text.split())}",
                payload=text,
                response_number=n,
            )
        )
        self._refresh_list()

    def _on_steer(self) -> None:
        """Send what is typed into the run that is already going."""
        if (
            getattr(self, "_session_backend", None) in _QUEUEING_BACKENDS
            and self._run_in_progress()
        ):
            self._queue_message(steering=True)
            return
        worker = self._worker
        text = self.prompt.GetValue().strip()
        if worker is None or not worker.is_alive():
            self._announce("Error: Nothing is running to steer")
            return
        if not text:
            self._announce("Error: Type a message first, then steer")
            return
        if not worker.steer(text):
            # The turn finished between typing and pressing. Leave the text in
            # place so it can just be sent as the next prompt.
            self._announce("Error: The run already finished. Press Send to ask it now.")
            return
        self.prompt.SetValue("")
        self._earcons.play_send(self._session_backend)
        self._add_your_message(text, steering=True)
        self._announce(f"Steered: {text}")

    def _on_stop(self) -> None:
        """Stop the run in progress, keeping whatever it produced first.

        Stop asks the backend to end the turn. A backend that holds its
        process between turns, Claude Code and Codex, keeps that process when
        it confirms the stop, so what was already streamed is all this turn
        will say and the next message goes to the same process. The rows stay
        in the list, and the turn keeps their text as its response, so the
        transcript is not left with a question and no answer.
        """
        worker = self._worker
        if getattr(self, "_pending_messages", []):
            self._queue_paused = True
            self._announce(
                "Queued messages paused. Use /queue resume to send them or /queue clear to remove them"
            )
        if worker is None or not worker.is_alive():
            self._announce("Error: Nothing is running to stop")
            return
        self.stop_btn.Disable()
        self.steer_btn.Disable()
        self._stopping = True
        self._close_question_dialog()
        self._announce("Stopping")

        def cancel() -> None:
            # cancel() waits on the process, so it must not run on the UI thread.
            worker.cancel()
            # A backend that needs longer to land a stop says so.
            worker.join(timeout=getattr(worker, "stop_seconds", _CANCEL_JOIN_SECONDS))
            wx.CallAfter(self._after_cancel, worker)

        threading.Thread(target=cancel, daemon=True).start()

    def _after_cancel(self, worker: AgentWorker) -> None:
        """Give Stop back if the cancel did not end the turn.

        `_stopping` mutes narration and Stop is disabled, so a backend that
        ignored the cancel would otherwise run on in silence with no way to
        try again.
        """
        if not self or self._worker is not worker or not worker.is_alive():
            return
        self._stopping = False
        self.stop_btn.Enable()
        self._announce("Error: Could not stop the task. It is still running", urgent=True)

    def _finish_stopped_turn(self) -> None:
        """Close out a turn the user stopped, without reporting it as failed."""
        self._flush_live_answer()
        self._earcons.stop_progress()
        self._hide_working()
        partial = self._streamed_assistant.strip()
        if self._turns and not self._turns[-1].response:
            self._turns[-1].response = partial
        if self._stream_response is not None:
            for row in self._rows:
                if row.response_number == self._stream_response and row.kind == "header":
                    row.payload = _strip_noise(partial)
                    if row.source is None:
                        row.source = partial
                    break
        self._stream_response = None
        self._refresh_list()
        self._announce("Stopped")

    def _hermes_worker_extra(self, attachments: list[str]) -> dict:
        """The extra arguments a Hermes turn needs, gathered in one place.

        Kept as a method of its own so a test can ask what a turn will be
        given. Inline in the send path this was untestable, and a mutation that
        dropped the attachments -- meaning no file bytes ever left the machine,
        the exact bug being fixed -- went unnoticed by a green test run.
        """
        extra: dict = {}
        # Point the turn at a Hermes elsewhere when one is configured. Nothing
        # is added when it is not, so the local path stays the
        # zero-configuration default.
        remote_url = REMOTE_HERMES.url()
        if remote_url:
            extra["remote_url"] = remote_url
            extra["remote_token"] = REMOTE_HERMES.key
            extra["remote_credential"] = REMOTE_HERMES.credential
            extra["remote_username"] = REMOTE_HERMES.username
        # The connection belongs to the conversation, not the turn: the next
        # message reuses it instead of logging in and resuming again.
        if self._held_hermes is None:
            from hermes_worker import HeldConnection

            self._held_hermes = HeldConnection()
        extra["held"] = self._held_hermes
        # Only on the turn that CREATES the conversation. session.resume takes
        # no title, and sending one after the first turn would be a value the
        # protocol drops -- so it is cleared once it has been handed over,
        # leaving Hermes as the single owner of the name from then on.
        #
        # Read through getattr because upstream calls this method on stand-in
        # panels (and on one that has not finished __init__). Touching the
        # attribute directly raised AttributeError there, which in a teardown
        # path would leave a turn uncancelled -- the same class of defect as
        # the held-connection read, and caught the same way.
        session_title = str(getattr(self, "_session_title", "") or "")
        if session_title and not self._session_id:
            extra["session_title"] = session_title
        if attachments:
            extra["attachments"] = list(attachments)
        return extra

    def _opencode_worker_extra(self, attachments: list[str]) -> dict:
        """The extra arguments an opencode turn needs: the attached files.

        The worker sends pictures and text files as file parts and names the
        rest by path, so files are handed over rather than written into the
        prompt -- the uploading-backend shape Hermes established.
        """
        extra: dict = {}
        if attachments:
            extra["attachments"] = list(attachments)
        return extra

    def _muse_worker_extra(self, attachments: list[str]) -> dict:
        """The extra arguments a Muse turn needs: the attached files.

        The worker sends images as base64 parts and names the rest by paths
        translated for its host, so files are handed over rather than written
        into the prompt -- the uploading-backend shape Hermes established.
        """
        extra: dict = {}
        if attachments:
            extra["attachments"] = list(attachments)
        return extra

    def _build_send_text(self, prompt: str) -> str:
        """Combine the prompt with file paths for the selected coding agent.

        Only for backends that read the file off this machine's disk. A backend
        that takes an upload is handed the files themselves (see
        ``uploads_attachments``), and naming their paths here would describe
        them twice -- once as a path that may mean nothing on the far side.
        """
        parts = [prompt] if prompt else []
        if self._attachments and not self._backend_uploads_attachments():
            listing = "\n".join(self._attachments)
            parts.append("Attached files (please read them):\n" + listing)
        return "\n\n".join(parts)

    def _backend_uploads_attachments(self) -> bool:
        """Whether the chosen backend wants the file's bytes, not its path."""
        return bool(getattr(BACKENDS[self.selected_backend()], "uploads_attachments", False))

    def _attachment_summary(self) -> str:
        """One line naming the files going out, for the transcript row.

        An uploading backend gets no paths in its prompt, so without this the
        user's own message in the list would not mention the attachment at all
        and the transcript would read as a question about nothing.

        The name comes from `attachment_name`, the same helper the upload itself
        uses, rather than `os.path.basename`. On Linux or macOS basename does not
        treat a backslash as a separator, so a Windows path attached to a Hermes
        reached from a Linux desktop kept the whole of
        `D:\\projekty\\raport.xlsx` in a line that is read aloud. Sharing the
        helper is the point: the file Hermes stores and the name spoken in the
        transcript cannot drift apart.
        """
        # Imported here, not at module scope: hermes_worker pulls in the
        # websocket client, and a user on another backend should not pay for it.
        from hermes_worker import attachment_name

        names = [attachment_name(p) or p for p in self._attachments]
        if not names:
            return ""
        noun = "file" if len(names) == 1 else "files"
        return f"[Sending {len(names)} {noun}: {', '.join(names)}]"

    def clear_conversation(self) -> None:
        """Forget this conversation and start a fresh one in the same tab.

        The backend is not told anything: dropping its session id is what makes
        the next message the first of a new conversation. The old conversation
        is still on disk, and Recent Conversations can bring it back.
        """
        if self._run_in_progress():
            # Emptying `_turns` here while the last turn's `complete` is still
            # queued leaves that event writing into a list with nothing in it.
            self._announce("Error: Stop the running task before starting a new conversation")
            return
        self._museai_main_serial += 1
        self._session_id = None
        # A new conversation takes the Account Codex uses when it starts, so a
        # choice made since the last one reaches it.
        self._forget_codex_conversation_account()
        self._museai_main_required = False
        self._museai_main_loading = False
        self._museai_pending_send = False
        self._drop_held_backends()
        # The name from the New Session dialog belonged to the conversation
        # just abandoned. Keeping it would hand that name to the NEXT
        # conversation's session.create -- two conversations called the same
        # thing, only one of which the person named -- and would keep this tab
        # from taking the name of the first message, which is all a nameless
        # conversation has.
        self._pending_messages = []
        self._steering_context = ""
        self._queue_paused = False
        self._session_title = ""
        self._forget_subagents()
        self.task_list = TaskList()
        self.decisions = []
        self._turns = []
        self._rows = []
        self._displayed = []
        self._response_count = 0
        self._stream_response = None
        self._streamed_assistant = ""
        self._refresh_list()
        # Nothing has been said in this conversation yet, so it has no name;
        # the tab falls back to the folder until the first message gives it one.
        self._on_title(self, "")
        self._announce("New conversation started. The previous one is in Recent Conversations")

    def compact_conversation(self) -> None:
        """Ask the backend to summarise this conversation in place.

        Compaction replaces the conversation so far with a summary of it, which
        is how a long session keeps going once its context window fills up.
        Claude Code takes it as a message; Codex has a request of its own for
        it; FreeBuff's CLI cannot do it at all.
        """
        backend = self.selected_backend()
        request = compaction_request(backend)
        if request is None:
            self._announce(
                f"Error: {backend_label(backend)} cannot compact a conversation. "
                "Start a new conversation instead"
            )
            return
        if self._run_in_progress():
            self._announce("Error: Wait for the running task to finish before compacting")
            return
        if not self._session_id or backend != self._session_backend:
            self._announce("Error: There is no conversation to compact yet")
            return
        text, extra = request
        self.prompt.SetValue(text)
        self._announce("Compacting the conversation")
        self._on_send(worker_extra=extra)

    def restore_history(self, entry: HistoryEntry, turns: list[HistoryTurn]) -> None:
        """Put a past conversation back in this tab, ready to be continued.

        Rows are rebuilt the way a live turn builds them — the user's own
        message, then the answer segmented into navigable rows — so a
        conversation from last week reads exactly like one that just finished.
        Adopting the backend's own session id is what makes the next message a
        continuation of it rather than the start of something new.
        """
        self._museai_main_serial += 1
        self._museai_main_loading = False
        self._museai_pending_send = False
        self._session_id = entry.session_id
        self._museai_main_required = False
        self._pending_messages = []
        self._steering_context = ""
        self._queue_paused = False
        # The tab is now a different conversation, so any connection held for
        # the previous one must not carry the next message.
        self._drop_held_backends()
        # ...and a name typed for the previous one is not this conversation's
        # name either: the restored conversation has a title of its own, put on
        # the tab below.
        self._session_title = ""
        self._session_backend = normalize_backend(entry.backend)
        self._backend = self._session_backend
        self._turns = [Turn(prompt=turn.prompt, response=turn.response) for turn in turns]
        # The task list it left, so Task List and Turn Status pick up there.
        self.task_list = restore_tasks(entry)
        remote = (
            _remote_sessions().get(entry.session_id) if entry.backend == BACKEND_CLAUDE else None
        )
        self.remote_control = remote is not None
        self.remote_bridge = str((remote or {}).get("bridge") or "")
        self.remote_url = str((remote or {}).get("url") or "")
        self.decisions = []
        self._rows = []
        self._displayed = []
        self._search_term = ""
        self._response_count = 0
        self._stream_response = None
        self._streamed_assistant = ""
        self._assistant_narrated_this_turn = False
        for turn in turns:
            self._response_count += 1
            number = self._response_count
            if turn.prompt.strip():
                self._rows.append(
                    Row(
                        kind="you",
                        label=f"You: {' '.join(turn.prompt.split())}",
                        payload=turn.prompt,
                        response_number=number,
                    )
                )
            self._rows.extend(
                parse_response(turn.response, number, backend_label(self._session_backend))
            )
        self._refresh_list()
        self._on_title(self, entry.title)
        # Picks up the restored session: relabels the backend line, and gives
        # FreeBuff's terminal a head start on the conversation being resumed.
        self.backend_changed()
        responses = (
            "1 response" if self._response_count == 1 else f"{self._response_count} responses"
        )
        self._set_status(f"Resumed: {entry.title} — {responses}")
        # A muse.ai chat can still be running on its own machine: the last
        # thing in it is a message with no answer yet. Sit in on it so its
        # updates and answer arrive here instead of a blank response.
        if self._session_backend == BACKEND_MUSEAI:
            self._museai_seen: int | None = max((turn.sequence or 0 for turn in turns), default=0)
        if (
            self._session_backend == BACKEND_MUSEAI
            and turns
            and turns[-1].prompt.strip()
            and not turns[-1].response.strip()
        ):
            wx.CallAfter(self._follow_museai, entry.session_id)
        elif self._session_backend == BACKEND_MUSEAI:
            # Answered, but it may not be done: watch for more.
            self._watch_museai_later()

    # ----- muse.ai keeps working after it answers -----
    # Its answer often says "still on it" and background workers post later,
    # so an open muse.ai tab looks at the chat every so often while no turn is
    # running, and follows it again when anything new has happened.
    _MUSEAI_IDLE_CHECK_MS = 30_000

    def _watch_museai_later(self, seen: int | None = None) -> None:
        if seen is not None:
            self._museai_seen = seen
        if getattr(self, "_museai_watch", None) is None:
            self._museai_watch: wx.CallLater | None = wx.CallLater(
                self._MUSEAI_IDLE_CHECK_MS, self._check_museai
            )

    def _check_museai(self) -> None:
        self._museai_watch = None
        if not self or self._session_backend != BACKEND_MUSEAI or not self._session_id:
            return
        if self._run_in_progress():
            self._watch_museai_later()
            return
        session = self._session_id

        def look() -> None:
            from museai_backend import museai_latest_seq

            latest = museai_latest_seq(session)
            wx.CallAfter(self._museai_checked, session, latest)

        threading.Thread(target=look, daemon=True).start()

    def _museai_checked(self, session: str, latest: int) -> None:
        if not self or session != self._session_id or self._session_backend != BACKEND_MUSEAI:
            return
        seen = getattr(self, "_museai_seen", None)
        if seen is not None and latest > seen and not self._run_in_progress():
            self._follow_museai()
            return
        if latest and not seen:
            self._museai_seen = latest
        self._watch_museai_later()

    def _follow_museai(self, session: str | None = None) -> None:
        """A turn with nothing to send that reads what muse.ai posts next."""
        if (
            not self
            or self._run_in_progress()
            or self.selected_backend() != BACKEND_MUSEAI
            or self._session_backend != BACKEND_MUSEAI
            or not self._session_id
            or (session is not None and session != self._session_id)
        ):
            return
        self._museai_follow_after = getattr(self, "_museai_seen", None)
        self._assistant_narrated_this_turn = False
        self._streamed_assistant = ""
        self._stopping = False
        self._turns.append(Turn(prompt=""))
        self._announce("muse.ai is still working on this. Following it.")
        self.send_btn.Disable()
        self._earcons.start_progress()
        self._show_working()
        self._launch_turn(None, BACKEND_MUSEAI, {})

    def open_hermes_session(self, session_id: str, title: str, attaching: bool) -> None:
        """Take over this tab with a Hermes conversation, and read it back.

        Unlike ``restore_history`` this does not read a transcript off local
        disk: the conversation may live on another machine, so the transcript
        is asked of Hermes itself. A worker started in resume-only mode
        replays it through the same callbacks a live turn uses, which is what
        makes a conversation from a terminal on the server read exactly like
        one that just happened here.

        When ``attaching`` is true the conversation is running right now: the
        replay is followed by the events of the turn in progress, and Hermes'
        one-stream-per-conversation rule means this window now owns that
        stream.
        """
        if self._worker is not None and self._worker.is_alive():
            self._announce("Error: Stop the running task before opening another conversation")
            return
        # The tab becomes that conversation: the id it will continue, and the
        # backend it belongs to. A held connection from the previous one must
        # not carry the next message.
        self._session_id = session_id
        self._drop_held_backends()
        # This tab is now a conversation Hermes already named; a name typed for
        # whatever was here before is not it.
        self._session_title = ""
        self._session_backend = BACKEND_HERMES
        self._backend = BACKEND_HERMES
        self._turns = []
        self._rows = []
        self._displayed = []
        self._search_term = ""
        self._response_count = 0
        self._stream_response = None
        self._streamed_assistant = ""
        self._assistant_narrated_this_turn = False
        self._stopping = False
        self._replaying = True
        self._refresh_list()
        self._on_title(self, title or make_title(session_id))
        self.backend_changed()

        extra = self._hermes_worker_extra([])
        # Resume-only: this worker reopens the conversation and says nothing
        # into it. Without the flag it would submit a prompt, which on a live
        # conversation would post an empty message into someone else's turn.
        extra["resume_only"] = True
        self._announce("Attaching" if attaching else "Reopening")
        self._earcons.start_progress()
        self._show_working()
        self._worker = worker_class(BACKEND_HERMES, ClaudeWorker)(
            "",
            self._session_id,
            self.cwd,
            self.mode,
            model=self.model,
            effort=self.effort,
            on_session=lambda sid: self._queue_worker_event("session", sid),
            on_started=lambda: self._queue_worker_event("started"),
            on_activity=lambda kind, text: self._queue_worker_event("activity", kind, text),
            on_complete=self._worker_complete,
            on_failed=lambda msg: self._queue_worker_event("failed", msg),
            on_done=lambda: self._queue_worker_event("done"),
            on_question=self._ask_questions,
            on_subagent=lambda *report: self._queue_worker_event("subagent", *report),
            **extra,
        )
        self._worker.start()
        if attaching:
            # A turn somebody else started: how long it ran before is unknown,
            # so Turn Status counts from here and says so.
            self._turn_started_at = time.monotonic()
            self._turn_attached = True
            self._last_step = ""
        self.stop_btn.Enable()
        if attaching:
            # Steering a turn someone else started is exactly what this is for.
            self.steer_btn.Enable()

    def _drop_held_backends(self) -> None:
        """Let go of every process held for this tab's conversation.

        Called wherever the tab stops being the conversation those processes
        were started for: a new conversation, a restored one, a different
        backend, a model or effort change that invalidates the session. A
        process carries the conversation's live ids, so reusing one across that
        boundary would send the next message into the previous conversation.

        Process-wide backends -- Codex, opencode -- are deliberately not
        dropped here: their one process serves every tab, and this tab
        abandoning a conversation is not a reason to end four others' work.
        """
        held = getattr(self, "_held_hermes", None)
        if held is not None:
            held.drop()  # type: ignore[attr-defined]
            self._held_hermes = None
        shared = backend_pool.pool()
        for backend in (BACKEND_CLAUDE, BACKEND_HERMES, BACKEND_FREEBUFF):
            # A key never held is a documented no-op, so this stays correct
            # while Hermes and FreeBuff still start fresh each turn.
            shared.drop_async(backend_pool.pool_key(backend, self))
        # A wake-up queued by the process that just went belonged to the
        # conversation it went with. Read through getattr because this also
        # runs on half-built panels and on test stand-ins.
        self._claude_generation = getattr(self, "_claude_generation", 0) + 1

    def _on_session_started(self, session_id: str) -> None:
        # Normally the held id wins, so a worker's incidental ids never move
        # the tab. One a worker that REPLACED the conversation reports must be
        # adopted even though an id is held: Command Code after a fresh or
        # free-model retry, or a compaction's summarized replacement.
        if self._session_id and not getattr(self._worker, "session_replaced", False):
            return
        self._session_id = session_id

    def _begin_stream_response(self) -> int:
        """Open a new response (header row) the first time a turn produces output.

        Returns the response number so the streamed rows group under it.
        """
        if self._stream_response is None:
            self._response_count += 1
            self._stream_response = self._response_count
            self._rows.append(
                Row(
                    kind="header",
                    label=f"Response {self._response_count}",
                    payload="",
                    response_number=self._response_count,
                )
            )
        return self._stream_response

    def _on_activity(self, kind: str, text: str, *, refresh: bool = True) -> None:
        """Stream real content into the list as it arrives during a turn.

        ``kind == "assistant"`` is the backend's narration/answer text (segmented
        into prose and code rows). ``kind == "thinking"`` is its reasoning about
        what to do next. ``kind == "tool"`` is an action line for a tool it just
        invoked. ``kind == "result"`` is that tool's actual output (file
        contents, command output), shown as one row whose payload is the full
        result.

        Prose, thinking, and tool steps are spoken as they arrive so the user
        follows the work by ear; a tool result only speaks its short preview
        line, since results run to hundreds of lines.

        ``kind == "you"`` is the user's own message in a replayed Hermes
        conversation, and ``kind == "subagent"`` is a subagent's commentary.
        A notice is BlindPilot's own status, never assistant answer text.

        With live activity switched off in Options, the response lands at the
        end; notices still reach speech and status. A replayed conversation is
        the exception: its rows are the transcript, not live activity, and
        without them a reopened conversation would show nothing at all.
        """
        if kind == "auto_route":
            SessionPanel._set_auto_route(self, text)
            return
        if kind == "replay_end":
            # The transcript a reopened Hermes conversation replays is over;
            # whatever follows is the attached turn itself.
            self._replay_done = True
            return
        if kind == "notice":
            if text.strip():
                self._say(text.strip(), "notice")
            return
        if kind == "step":
            # Where a long turn has got to (muse.ai's step count): the last row
            # of the conversation, changed in place as it moves on, plus the
            # status line and the progress report. Nothing is spoken, since it
            # changes every few seconds; arrow to the bottom to read it.
            self._last_step = text.strip()
            self._set_status(text.strip())
            self._show_step_row(text.strip())
            return
        if kind == "tool" and text.strip():
            if not getattr(self, "_replaying", False) or getattr(self, "_replay_done", False):
                # A replayed history row is an old step, not what this turn did.
                self._last_step = text.strip()
        if not SETTINGS.live_rows and not self._replaying:
            return
        if not text.strip():
            # A blank stream chunk (a message's trailing newline) makes no
            # row, but would still speak the bare backend name.
            return
        n = self._begin_stream_response()
        if kind in ("you", "result", "tool", "thinking"):
            # Something else is being said, so the message streamed so far has
            # ended and its last block is finished too.
            self._flush_live_answer()
        if kind == "you":
            # A replayed conversation carries the user's own messages, which a
            # live turn adds itself (_add_your_message) before the worker
            # starts. Reopening one has no such moment, so the row is built
            # here — same label, so a reopened conversation reads exactly like
            # one that just happened.
            self._rows.append(
                Row(
                    kind="you",
                    label=f"You: {' '.join(text.split())}",
                    payload=text,
                    response_number=n,
                )
            )
            self._say(f"You: {' '.join(text.split())}")
        elif kind == "result":
            self._rows.append(
                Row(
                    kind="result",
                    label=_result_label(text),
                    payload=text,
                    response_number=n,
                )
            )
            self._say(_result_label(text), "result")
        elif kind == "tool":
            self._rows.append(Row(kind="tool", label=text, payload=text, response_number=n))
            self._say(text, "tool")
        elif kind == "thinking":
            # Reasoning is the backend talking to itself. It is off by default:
            # it roughly doubles what has to be listened through before the
            # answer, and it is not the answer.
            if not SETTINGS.show_thinking:
                return
            # Read as plain text: the word "Thinking" in front of every one of
            # these lines is repeated far more often than it is informative.
            flat = " ".join(text.split())
            self._rows.append(
                Row(
                    kind="thinking",
                    label=flat,
                    payload=text,
                    response_number=n,
                )
            )
            self._say(flat, "thinking")
        else:
            # Reuse the Markdown segmenter; drop its header (index 0) since
            # this turn already has one. The first row of each incoming message
            # is marked with the active backend's name, the way "You:" marks
            # the user's own messages.
            speaker = backend_label(self._session_backend)
            from_subagent = kind == "subagent"
            if from_subagent:
                # Named so the row says whose words these are: several
                # agents' commentary arrives interleaved on one stream.
                speaker = f"{speaker} subagent"
            self._stream_answer_rows(text, n, speaker)
            if not from_subagent:
                # A subagent's words are not this turn's answer, and were
                # already kept out of it upstream.
                self._streamed_assistant += ("\n\n" if self._streamed_assistant else "") + text
            if self._say(f"{speaker}. {' '.join(text.split())}", kind) and not from_subagent:
                self._assistant_narrated_this_turn = True
        if refresh:
            self._refresh_list()

    def _stream_answer_rows(self, text: str, n: int, speaker: str) -> None:
        """Add streamed answer text to the list, one finished block at a time.

        Streaming backends hand over a sentence at a time so it is spoken at
        once. Parsed alone, every sentence became a row of its own: a numbered
        list lost its numbers, and a line was split at its colon. The sentences
        of one message are parsed together instead, and a block becomes a row
        once the next block has begun, so rows are only ever appended and the
        list never rebuilds under the reader.
        """
        live = getattr(self, "_live_answer", None)
        if live is None or live[0] != n or live[1] != speaker:
            self._flush_live_answer()
            live = self._live_answer = [n, speaker, "", 0, 0]
        if live[2] and not live[2].endswith("\n") and not text[:1].isspace():
            # A chunk that arrived trimmed. A line break keeps a list item or
            # heading that starts it a block of its own, and is otherwise just
            # a space inside the paragraph.
            live[2] += "\n"
        live[2] += text
        self._add_live_answer_rows(final=False)

    def _add_live_answer_rows(self, final: bool) -> None:
        """Add the blocks of the streamed answer that have finished.

        Only the text from the parse offset on is parsed, and the block still
        growing is held back until the one after it begins, so a chunk never
        re-parses the whole answer or emits half a block. The offset only ever
        lands on a block's first line; where a prefix the parser would strip
        begins indented, an empty html block props the indent up, so an
        indented block still arrives whole.
        """
        live = self._live_answer
        if live is None:
            return
        n, speaker, text, offset, done = live
        split = len(text) - offset if final else _last_block_start(text[offset:])
        prefix = text[offset : offset + split]
        if offset and prefix[:1].isspace():
            prefix = "<!-- -->\n\n" + prefix
        rows = parse_response(prefix, n)[1:]
        for i, row in enumerate(rows):
            if done == 0 and i == 0:
                row.speaker = speaker
                if row.kind != "code":
                    row.label = f"{speaker}: {row.label}"
            self._rows.append(row)
        live[3] = offset + split
        live[4] = done + len(rows)

    def _flush_live_answer(self) -> None:
        """Add the last block of the message being streamed; it has ended."""
        live = getattr(self, "_live_answer", None)
        if live is not None:
            self._add_live_answer_rows(final=True)
            for row in reversed(self._rows):
                if row.kind == "header" and row.response_number == live[0]:
                    row.source = f"{row.source}\n\n{live[2]}" if row.source else live[2]
                    break
            self._live_answer = None

    def _say(self, text: str, kind: str = "assistant") -> bool:
        """Speak live activity, and mirror a short form to the status bar.

        Only the visible tab narrates — a background session talking over the
        one being read would be unusable. The status bar gets the line either
        way, so nothing this declines to speak is actually lost: it is a row in
        the list and it is under the review cursor.
        """
        self._set_status(text[:99] + "…" if len(text) > 100 else text)
        if not SETTINGS.speak_live:
            return False
        if self._stopping:
            # The turn was stopped. Narration queued before that still arrives
            # afterwards, and hearing the run carry on describing itself sounds
            # exactly like a Stop that did not work.
            return False
        if SETTINGS.narration == NARRATION_KEEP_UP and kind not in _ALWAYS_SPOKEN:
            return False
        book = self.GetParent()
        if isinstance(book, wx.BookCtrlBase) and book.GetCurrentPage() is not self:
            return False
        announce(text)
        return True

    def _narrate_completed_response(self, text: str) -> None:
        """Speak a final answer when no assistant activity was narrated live."""
        if not SETTINGS.speak_live or self._assistant_narrated_this_turn or not text.strip():
            return
        speaker = backend_label(self._session_backend)
        if self._say(f"{speaker}. {' '.join(text.split())}"):
            self._assistant_narrated_this_turn = True

    def _on_response_complete(self, text: str) -> None:
        # The turn beat the cancellation, so it is a normal response.
        self._stopping = False
        self._flush_live_answer()
        # Stop the in-progress loop and play the "received" cue.
        self._earcons.play_received()
        # Kept rather than asked here: the turn is still finishing, and a modal
        # over a run that has not reported itself done would be answered into a
        # turn that cannot take it.
        # A block written into the reply carries real options, so the dialog
        # can offer them to pick from; the prose reading is the fallback, and
        # is all a backend with no question tool of its own can give.
        asked_properly = getattr(self, "_structured_question_asked", False)
        replaying = getattr(self, "_replaying", False)
        block = () if (asked_properly or replaying) else questions_written_in_the_answer(text)
        self._question_in_the_answer = block or question_left_in_the_answer(
            text,
            asked_properly=asked_properly,
            replaying=replaying,
        )
        # A reopened conversation completes with no text: nothing new was said,
        # the rows are the transcript that was just replayed. Announcing a
        # "response received, 0 segments" there, or parsing "" into a fresh
        # response, would invent an empty answer at the end of someone's
        # history — and a screen reader would read that gap as the last thing
        # in the conversation.
        if not text.strip() and not self._turns:
            # Close the replayed response, or the next message lands under it.
            self._stream_response = None
            self._set_status(f"Reopened, {len(self._rows)} rows")
            return
        if not text.strip() and self._stream_response is None and self._turns:
            # A turn that ended without an error and without a word: the fresh
            # "Response N" header below would carry an empty payload, a row
            # with nothing for a reader to read, and its status would claim
            # segments that do not exist. The response number is left for the
            # next real answer rather than spent on the empty one.
            self._turns[-1].response = ""
            self._stream_response = None
            return
        self._narrate_completed_response(text)
        if self._turns:
            self._turns[-1].response = text
        if self._stream_response is None:
            # Silent-until-response mode, or no streamed output arrived — parse the final text
            # into a fresh response so nothing is lost.
            self._response_count += 1
            new_rows = parse_response(
                text, self._response_count, backend_label(self._session_backend)
            )
            self._rows.extend(new_rows)
            self._stream_response = None
            self._refresh_list()
            self._set_status(
                f"Response {self._response_count} received, {len(new_rows) - 1} segments"
            )
            return
        # Fill the header payload so 'copy whole response' yields the full
        # answer text (the streamed rows are already in the list).
        for row in self._rows:
            if row.response_number == self._stream_response and row.kind == "header":
                row.payload = _strip_noise(text)
                row.source = text
                break
        # Streaming is best-effort: a backend can finish with text that
        # never arrived as activity. Without this the answer would exist
        # only in the header payload, and the list would end on whatever
        # the last streamed row happened to be.
        if text.strip() and _flatten(text) not in _flatten(self._streamed_assistant):
            speaker = backend_label(self._session_backend)
            segments = parse_response(text, self._stream_response)[1:]
            for i, row in enumerate(segments):
                if i == 0:
                    row.speaker = speaker
                    if row.kind != "code":
                        row.label = f"{speaker}: {row.label}"
                self._rows.append(row)
        n = self._response_count
        self._stream_response = None
        self._refresh_list()
        self._set_status(f"Response {n} received")

    def _on_failed(self, message: str) -> None:
        if self._stopping:
            # A cancelled backend reports its own interruption. The user asked
            # for it, so it is not news, and it is not an error.
            return
        self._queue_paused = True
        self._flush_live_answer()
        self._earcons.stop_progress()
        self._hide_working()
        self._earcons.play_error()
        if self._turns and not self._turns[-1].response:
            self._turns.pop()
        if self._stream_response is None and self._rows and self._rows[-1].kind == "you":
            # Nothing streamed, so the "You:" row is numbered for a response
            # that never opened. Spend that number on it, or the next turn
            # takes the same one and the two prompts copy as one response.
            self._response_count = max(self._response_count, self._rows[-1].response_number)
        if getattr(self._worker, "lost_session", False):
            # The backend could not resume this conversation -- Codex says so
            # on its failure, Command Code on a lost --resume -- so the id
            # names nothing. The next message starts a fresh one, as the
            # worker has already said, rather than failing the same way again.
            self._session_id = None
            # And a fresh one is a new conversation: it takes the Account Codex
            # uses when it starts, not the one the lost thread was held under.
            self._forget_codex_conversation_account()
        if getattr(self._worker, "plan_token_failed", False) and self._session_id is None:
            # The Account this conversation was bound to could not produce a
            # token, and nothing has begun on it. Let the next turn take the
            # saved choice again: deleting or signing out of that Account
            # already put the choice back on Codex's own login, where retrying
            # its dead id would fail the same way every time.
            self._forget_codex_conversation_account()
        # The failure stays in the list as a row of its own. The status bar
        # line is overwritten by the next event, and a list that looks the
        # same after a failed turn as after a finished one tells a reader
        # nothing when they come back to it.
        self._rows.append(
            Row(
                kind="error",
                label=f"Error: {message}",
                payload=message,
                response_number=self._stream_response or self._response_count,
            )
        )
        self._refresh_list()
        self._stream_response = None
        self._announce(f"Error: {message}", urgent=True)
        self._turn_failed = True
        _notify_if_away(self, f"The turn failed: {message}")

    def _on_worker_finished(self) -> None:
        answered = not self._stopping and not getattr(self, "_turn_failed", False)
        started = getattr(self, "_turn_started_at", None)
        if started is not None:
            self._last_turn_seconds = time.monotonic() - started
            self._turn_started_at = None
        fallback_model = getattr(self._worker, "fallback_model", "")
        if getattr(self, "model", "") == AUTO_MODEL and fallback_model:
            self._auto_route = f"Switched to {fallback_model} after a Command Code stop."
        self._replay_done = False
        SessionPanel._note_context(self, getattr(self._worker, "context_tokens", None))
        SessionPanel._drop_step_rows(self)
        if getattr(self, "_session_backend", "") == BACKEND_MUSEAI:
            # muse.ai may keep working after its answer: watch past the
            # newest thing this turn saw.
            SessionPanel._watch_museai_later(self, getattr(self._worker, "last_seq", 0) or None)
        self._settle_subagents()
        # Safety net: make sure the loop is never left running.
        self._earcons.stop_progress()
        self._hide_working()
        if getattr(self, "_live_answer", None) is not None:
            self._flush_live_answer()
            self._refresh_list()
        if self._stopping:
            self._stopping = False
            self._finish_stopped_turn()
        elif not getattr(self, "_turn_failed", False):
            _notify_if_away(self, "The turn finished")
        self._turn_failed = False
        if self.send_btn:
            self.send_btn.Enable()
        if self.steer_btn:
            self.steer_btn.Disable()
        if self.stop_btn:
            self.stop_btn.Disable()
        if self._turns and self._turns[-1].prompt == "" and not self._turns[-1].response:
            # A late turn appends a turn for its answer to land in. One woken
            # for a process that had already gone reads nothing, and a blank
            # question with a blank answer is not an entry in a transcript.
            self._turns.pop()
        self._worker = None
        self._replaying = False
        # Read and cleared here, because the next turn starts from whatever
        # this one left behind and a question already put is not asked twice.
        written = getattr(self, "_question_in_the_answer", "")
        self._question_in_the_answer = ""
        self._structured_question_asked = False
        queued = bool(getattr(self, "_pending_messages", []))
        if queued:
            if getattr(self, "_queue_paused", False):
                self._announce(
                    "Queued messages paused. Use /queue resume to retry or /queue clear to remove them"
                )
            else:
                self._send_queued_message()
        waiting = getattr(self, "_late_turn_waiting", None)
        self._late_turn_waiting = None
        if written and not queued and waiting is None and SETTINGS.ask_written_questions:
            # Posted rather than opened, so the event batch this is being
            # drained from finishes first: a modal opened inside it would hold
            # every row, cue and announcement still queued behind it.
            wx.CallAfter(self._ask_question_left_in_the_answer, written)
        if waiting is not None:
            self._start_late_turn(waiting)
        report = getattr(self, "on_turn_answer", None)
        if report is not None and not queued and waiting is None:
            answer = self._turns[-1].response if answered and self._turns else None
            wx.CallAfter(report, self, answer)

    # ----- List + find -----
    def _show_step_row(self, text: str) -> None:
        """Keep one live status row at the bottom of the conversation."""
        row = Row(kind="step", label=text, payload=text, response_number=self._response_count or 1)
        last_is_step = bool(self._rows) and self._rows[-1].kind == "step"
        shown_last = bool(self._displayed) and self._displayed[-1] is (
            self._rows[-1] if self._rows else None
        )
        if last_is_step and shown_last and not self._search_term:
            self._rows[-1] = row
            self._displayed[-1] = row
            if SETTINGS.text_view:
                if self._row_starts:
                    start = self._row_starts[-1]
                    was_at = self.responses_text.GetInsertionPoint()
                    self.responses_text.Replace(
                        start, self.responses_text.GetLastPosition(), _one_line(text)
                    )
                    self.responses_text.SetInsertionPoint(
                        min(was_at, self.responses_text.GetLastPosition())
                    )
            else:
                self.responses.ReplaceLast(row)
            return
        # Anything said since the last update sits above it now; the old
        # status row is dropped at the end of the turn.
        self._rows.append(row)
        self._refresh_list()

    def _drop_step_rows(self) -> None:
        """The turn is over, so its live status rows have nothing left to say."""
        if any(row.kind == "step" for row in getattr(self, "_rows", [])):
            self._rows = [row for row in self._rows if row.kind != "step"]
            self._refresh_list()

    def _refresh_list(self) -> None:
        # Rebuilding a control loses the selection, and putting it back is
        # what speaks (a list selection or a caret move is what NVDA reads).
        # Output is only ever appended, so the usual case appends too, and
        # nothing speaks.
        previous = [row.label for row in self._displayed]
        # `previous` records what the model last displayed, not what the
        # control shows. clear_conversation empties both lists while the
        # control still holds the old transcript, and apply_view_mode shows a
        # control only the visible one ever fills, so the append path asks
        # the control what it is actually showing before trusting the record.
        if SETTINGS.text_view:
            shown = len(self._row_starts) if self.responses_text.GetLastPosition() else 0
        else:
            shown = self.responses.GetCount()
        trustworthy = shown == len(previous)
        keep = self._selected_row()
        term = self._search_term.lower()
        labels: list[str] = []
        self._displayed = []
        for row in self._rows:
            if term:
                payload, label = _search_key(row)
                if term not in payload and term not in label:
                    continue
            labels.append(row.label)
            self._displayed.append(row)

        if trustworthy and labels[: len(previous)] == previous:
            added_rows = self._displayed[len(previous) :]
            if added_rows:
                self._append_rows(added_rows)
            return

        # The rows really did change shape - a search, a new turn, a response
        # replaced by its parsed form - so there is no way around a rebuild,
        # and restoring the selection afterwards is right rather than wrong.
        if SETTINGS.text_view:
            lines = [_one_line(row.label) for row in self._displayed]
            self._row_starts = _starts_of(lines)
            self.responses_text.ChangeValue("\n".join(lines))
        else:
            self.responses.Set(self._displayed)
        if keep != wx.NOT_FOUND and labels:
            self._select_row(keep)

    def _append_rows(self, rows: list[Row]) -> None:
        """Add rows to the end, leaving the reader exactly where they are."""
        if not SETTINGS.text_view:
            self.responses.AppendItems(rows)
            return
        lines = [_one_line(row.label) for row in rows]
        last = self.responses_text.GetLastPosition()
        base = last + (1 if last else 0)
        for line in lines:
            self._row_starts.append(base)
            base += len(line) + 1
        text = "\n".join(lines)
        was_at = self.responses_text.GetInsertionPoint()
        lead = "\n" if last else ""
        # Appending moves the caret to the end, which is itself a move worth
        # announcing, so it goes straight back to the line being read.
        self.responses_text.AppendText(lead + text)
        self.responses_text.SetInsertionPoint(was_at)

    def open_find(self) -> None:
        """Find-in-responses popup (Conversation menu, Ctrl+F). Blank clears it."""
        with wx.TextEntryDialog(
            self,
            "Search responses (leave blank to show all):",
            "Find in Responses",
            self._search_term,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self._search_term = dlg.GetValue().strip()
            if self._search_term:
                # Kept after the filter is cleared, for F3 and Shift+F3.
                self._find_term = self._search_term
        # The row being read, by identity: the list is rebuilt around a new
        # filter and its numeric position no longer means the same row.
        sel = self._selected_row()
        reading = self._displayed[sel] if 0 <= sel < len(self._displayed) else None
        self._refresh_list()
        # Spoken, not just written to the status bar. No screen reader reads a
        # status bar it was not asked to, and a search that matched nothing
        # does not move focus either - so the list quietly emptied and not one
        # thing said so, which is indistinguishable from a dropped keystroke.
        if self._search_term:
            self._announce(
                f"Showing {len(self._displayed)} of {len(self._rows)} rows for '{self._search_term}'"
            )
            if self._row_count() > 0:
                self._focus_row(0)
        else:
            self._announce("Search cleared")
            same = [i for i, row in enumerate(self._displayed) if row is reading]
            if same:
                self._focus_row(same[0])

    def find_next(self, step: int) -> None:
        """Move to the next (1) or previous (-1) row holding the last search.

        Searches each row's whole text, not just the line shown, and goes round
        from the other end when it reaches one, saying so.
        """
        term = getattr(self, "_find_term", "").casefold()
        if not term:
            self._announce("Nothing to find yet. Ctrl+F searches the responses.")
            return
        rows = self._displayed
        start = self._selected_row()
        if start == wx.NOT_FOUND:
            start = -1 if step > 0 else len(rows)
        for offset in range(1, len(rows) + 1):
            index = (start + step * offset) % len(rows)
            if term in f"{rows[index].label}\n{rows[index].payload}".casefold():
                wrapped = (index <= start) if step > 0 else (index >= start)
                if wrapped and index != start:
                    self._announce("Wrapped to the top" if step > 0 else "Wrapped to the bottom")
                elif index == start:
                    self._announce("No other match")
                self._focus_row(index)
                return
        self._announce(f"'{getattr(self, '_find_term', '')}' not found")

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        """Row keys for both responses controls — the list box and the
        read-only edit field, where a line is a row."""
        key = event.GetKeyCode()

        if key == wx.WXK_TAB and event.ShiftDown():
            self.focus_above_responses()
            return

        sel = self._selected_row()

        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if sel != wx.NOT_FOUND:
                if event.ShiftDown():
                    self._read_formatted(self._displayed[sel])
                else:
                    self._open_row(sel)
            return

        if key == wx.WXK_F3:
            self.find_next(-1 if event.ShiftDown() else 1)
            return

        if key == ord("C") and event.CmdDown() and event.ShiftDown():
            if sel != wx.NOT_FOUND:
                self.copy_last_code(self._displayed[sel].response_number)
            return

        if key == wx.WXK_WINDOWS_MENU:
            self._show_row_menu()
            return

        if key == wx.WXK_DOWN:
            if event.CmdDown():
                if sel != wx.NOT_FOUND:
                    self._jump_to_next_response(sel)
                return
            # The responses are one focus region. At the bottom, consume Down
            # and remain on the final row; only Tab may enter the prompt. In
            # text view a row wraps to several visual lines and Down moves by
            # visual line, so the guard only applies once the caret is on the
            # last row's own last visual line; before that, Down must still
            # move the caret so the rest of the row can be read.
            if sel != wx.NOT_FOUND and sel == self._row_count() - 1:
                if SETTINGS.text_view:
                    _ok, _col, line = self.responses_text.PositionToXY(
                        self.responses_text.GetInsertionPoint()
                    )
                    if line == self.responses_text.GetNumberOfLines() - 1:
                        return
                else:
                    return
            event.Skip()
            return

        if key == wx.WXK_UP and event.CmdDown():
            if sel != wx.NOT_FOUND:
                self._jump_to_prev_response(sel)
            return

        # Plain 'c' copies the row; Shift+C copies the whole response. Modifier
        # combos (Cmd/Ctrl/Alt + C) fall through to the platform default.
        if (
            key == ord("C")
            and not event.CmdDown()
            and not event.ControlDown()
            and not event.AltDown()
        ):
            if sel != wx.NOT_FOUND:
                if event.ShiftDown():
                    self._copy_response(sel)
                else:
                    self._copy_row(sel)
            return

        event.Skip()

    def _on_list_activate(self, event: wx.CommandEvent) -> None:
        self._open_row(event.GetSelection())

    # ----- Row actions -----
    def _open_row(self, sel: int) -> None:
        if not (0 <= sel < len(self._displayed)):
            return
        row = self._displayed[sel]
        title = row.label if row.kind != "header" else f"Response {row.response_number}"
        dlg = ReadView(self, row.payload, title, monospace=row.kind == "code")
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        self._focus_row(sel)

    def _copy_row(self, sel: int) -> None:
        if not (0 <= sel < len(self._displayed)):
            return
        row = self._displayed[sel]
        if not _copy_to_clipboard(row.payload):
            self._announce("Error: Could not access clipboard")
            return
        self._announce(self._copy_message(row))

    def _copy_response(self, sel: int) -> None:
        if 0 <= sel < len(self._displayed):
            self._action_copy_response(self._displayed[sel])

    def copy_last_code(self, response_number: int) -> None:
        """Copy the response's last code block (Ctrl+Shift+C in the responses).

        The code an answer ends on is usually the one wanted, and reaching it
        meant arrowing past everything written after it.
        """
        code = [r for r in self._rows if r.kind == "code" and r.response_number == response_number]
        if not code:
            self._announce(f"Response {response_number} has no code block")
            return
        if not _copy_to_clipboard(code[-1].payload):
            self._announce("Error: Could not access clipboard")
            return
        self._announce(self._copy_message(code[-1]))

    @staticmethod
    def _copy_message(row: Row) -> str:
        if row.kind == "code":
            n = row.payload.count("\n") + 1 if row.payload else 0
            unit = "line" if n == 1 else "lines"
            if row.language:
                return f"Copied {n} {unit} of {row.language}"
            return f"Copied {n} {unit} of code"
        if row.kind == "header":
            return f"Copied response {row.response_number}"
        if row.kind == "you":
            return "Copied your message"
        names = {
            "heading": "heading",
            "list": "list",
            "quote": "quote",
            "result": "result",
            "thinking": "thinking",
            "tool": "tool step",
        }
        return f"Copied {names.get(row.kind, 'paragraph')}"

    # ----- Per-row actions menu -----
    def open_items(self, kind: str, response_number: int | None = None) -> None:
        messages = []
        for row in self._rows:
            if row.kind not in {"header", "you"}:
                continue
            if response_number is not None and row.response_number != response_number:
                continue
            source = row.source if row.source is not None else row.payload
            live = getattr(self, "_live_answer", None)
            if row.kind == "header" and live is not None and live[0] == row.response_number:
                source = f"{source}\n\n{live[2]}" if source else live[2]
            messages.append((row.label, source))
        _show_conversation_items(self, messages, kind)

    def _build_row_menu(self, row: Row) -> wx.Menu:
        """The actions offered for one row.

        Each item binds on the menu it belongs to rather than on the panel:
        a binding made on the panel outlives the menu that needed it, so every
        open of this menu used to add five more for as long as the
        conversation stayed on screen.
        """
        menu = wx.Menu()
        formatted = menu.Append(wx.ID_ANY, "Read response as formatted page\tShift+Enter")

        def read_formatted(_event: wx.CommandEvent) -> None:
            self._read_formatted(row)

        menu.Bind(
            wx.EVT_MENU, _gui_work("Read response as formatted page", read_formatted), formatted
        )
        for kind, label in (
            ("code", "Code Blocks in This Response…"),
            ("links", "Links in This Response…"),
        ):
            item = menu.Append(wx.ID_ANY, label)

            def browse_items(_event: wx.CommandEvent, chosen: str = kind) -> None:
                self.open_items(chosen, row.response_number)

            menu.Bind(wx.EVT_MENU, _gui_work(label, browse_items), item)
        if row.kind == "code":
            item = menu.Append(wx.ID_ANY, "Save code to file…")

            def save_code(_event: wx.CommandEvent) -> None:
                self._action_save_code(row)

            menu.Bind(wx.EVT_MENU, _gui_work("Save code to file", save_code), item)
        insert_item = menu.Append(wx.ID_ANY, "Insert into prompt")

        def insert(_event: wx.CommandEvent) -> None:
            self._action_insert(row)

        menu.Bind(wx.EVT_MENU, _gui_work("Insert response into prompt", insert), insert_item)
        copy_item = menu.Append(wx.ID_ANY, "Copy whole response")

        def copy_response(_event: wx.CommandEvent) -> None:
            self._action_copy_response(row)

        menu.Bind(wx.EVT_MENU, _gui_work("Copy whole response", copy_response), copy_item)
        copy_all_item = menu.Append(wx.ID_ANY, "Copy whole conversation")
        menu.Bind(
            wx.EVT_MENU,
            _gui_work("Copy whole conversation", lambda _e: self._action_copy_conversation()),
            copy_all_item,
        )
        return menu

    def _show_row_menu(self) -> None:
        """Arrowable actions for the focused row (Menu key / context gesture)."""
        sel = self._selected_row()
        if not (0 <= sel < len(self._displayed)):
            return
        menu = self._build_row_menu(self._displayed[sel])
        self._responses_ctrl().PopupMenu(menu)
        menu.Destroy()

    def _read_formatted(self, row: Row) -> None:
        """The row's whole response as a web page, for browse mode.

        A response split into rows is read a row at a time; as a page, the
        screen reader's own keys move by heading, list, table and link. Falls
        back to the plain text view where no web view is available.
        """
        n = row.response_number
        header = next(
            (r for r in self._rows if r.kind == "header" and r.response_number == n), None
        )
        text = (
            header.payload if header is not None and header.payload else reassemble(self._rows, n)
        )
        title = f"Response {n}"
        dlg = FormattedView.open(self, markdown_page(text, title), title) or ReadView(
            self, text, title
        )
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        sel = self._selected_row()
        if sel != wx.NOT_FOUND:
            self._focus_row(sel)

    def _action_save_code(self, row: Row) -> None:
        ext = _LANG_EXT.get(row.language or "", ".txt")
        with wx.FileDialog(
            self,
            "Save code to file",
            defaultDir=self.cwd,
            defaultFile="snippet" + ext,
            wildcard="All files (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(row.payload)
        except OSError as exc:
            self._announce(f"Error saving file: {exc}")
            return
        self._announce(f"Saved code to {os.path.basename(path)}")

    def _action_insert(self, row: Row) -> None:
        current = self.prompt.GetValue()
        sep = "\n" if current and not current.endswith("\n") else ""
        # ChangeValue fires no EVT_TEXT, so a long payload is not read back as
        # dictation on top of "Inserted into prompt".
        self.prompt.ChangeValue(current + sep + row.payload)
        self._prompt_text = self.prompt.GetValue()
        self.prompt.SetInsertionPointEnd()
        self.prompt.SetFocus()
        self._announce("Inserted into prompt")

    def _action_copy_response(self, row: Row) -> None:
        text = reassemble(self._rows, row.response_number)
        if not _copy_to_clipboard(text):
            self._announce("Error: Could not access clipboard")
            return
        self._announce(f"Copied whole response {row.response_number}")

    def _action_copy_conversation(self) -> None:
        """Every row in the list, first to last, on the clipboard."""
        if not self._rows:
            self._announce("Error: Nothing to copy yet")
            return
        text = reassemble_all(self._rows)
        if not _copy_to_clipboard(text):
            self._announce("Error: Could not access clipboard")
            return
        n = len(self._rows)
        self._announce(f"Copied whole conversation, {n} {'row' if n == 1 else 'rows'}")

    def export_conversation(self) -> None:
        """Save every row to a file (Conversation menu, Ctrl+E).

        The same text Copy Whole Conversation puts on the clipboard, so a long
        conversation can be kept, read in an editor or shared without pasting
        it somewhere first. Markdown and the web page give each response a
        heading, so browse mode moves between them with H. Offered in
        Documents rather than the project folder, where a stray transcript
        would end up in somebody's commit.
        """
        if not self._rows:
            self._announce("Error: Nothing to export yet")
            return
        name = _tab_label(getattr(self, "tab_title", ""), self.cwd)
        name = " ".join(re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).split()).strip(".")
        name = name or "Conversation"
        documents = os.path.join(os.path.expanduser("~"), "Documents")
        with wx.FileDialog(
            self,
            "Export Conversation",
            defaultDir=documents if os.path.isdir(documents) else os.path.expanduser("~"),
            defaultFile=f"{name} {time.strftime('%Y-%m-%d')}.md",
            wildcard="Markdown (*.md)|*.md|Web page (*.html)|*.html|Text (*.txt)|*.txt",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
            chosen = (".md", ".html", ".txt")[max(0, min(dlg.GetFilterIndex(), 2))]
        kind = os.path.splitext(path)[1].lower()
        if kind not in (".md", ".html", ".htm", ".txt"):
            # GTK and macOS can hand back the name as typed, with no extension
            # from the type chosen; the type is then what says the format.
            path, kind = path + chosen, chosen
            # The dialog only asked about the name as typed, not this one.
            if os.path.exists(path) and (
                wx.MessageBox(
                    f"{os.path.basename(path)} already exists. Replace it?",
                    "Export Conversation",
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                    self,
                )
                != wx.YES
            ):
                return
        if kind == ".txt":
            text = reassemble_all(self._rows)
        else:
            text = reassemble_all(self._rows, header="## Response {}")
        if kind in (".html", ".htm"):
            text = markdown_page(text, name)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text.rstrip("\n") + "\n")
        except OSError as exc:
            self._announce(f"Error: Could not export the conversation: {exc}")
            return
        self._announce(f"Exported conversation to {os.path.basename(path)}")

    # ----- Response navigation -----
    def jump_to_latest_response(self) -> None:
        """Cycle through response headers on each Cmd+R press.

        First press goes to the latest response. Subsequent presses cycle
        backwards through older responses, wrapping from the first back to
        the latest. This lets the user step through every response with the
        same key without touching arrow keys.
        """
        headers = [i for i, r in enumerate(self._displayed) if r.kind == "header"]
        if not headers:
            return
        cur = self._selected_row()
        # Find which header slot we're currently on (if any)
        if cur in headers:
            pos = headers.index(cur)
            # Step backwards; wrap from first header back to last (latest)
            nxt = headers[(pos - 1) % len(headers)]
        else:
            # Not on a header — jump to latest first
            nxt = headers[-1]
        self._focus_row(nxt)
        announce(self._displayed[nxt].label)

    def _jump_to_prev_response(self, current_sel: int) -> None:
        for i in range(current_sel - 1, -1, -1):
            if self._displayed[i].kind == "header":
                self._focus_row(i)
                announce(self._displayed[i].label)
                return

    def _jump_to_next_response(self, current_sel: int) -> None:
        for i in range(current_sel + 1, len(self._displayed)):
            if self._displayed[i].kind == "header":
                self._focus_row(i)
                announce(self._displayed[i].label)
                return

    # ----- Cleanup hook -----
    def cancel_worker(self, wait: bool = True) -> threading.Thread | None:
        """Give up this panel's turn, for a tab closing or the app quitting.

        cancel() waits on the process, so `wait=False` hands it to a daemon
        thread; a tab being destroyed has nothing to wait for. Quitting waits,
        or the CLI outlives the application (`_on_close` spends one budget on
        every tab). The shared progress loop is stopped here because the
        mailbox drops the worker's `done` once the panel is gone.
        """
        self._queue_paused = True
        self._pending_messages = []
        self._close_question_dialog()
        self._earcons.stop_progress()
        self._hide_working()
        if self._dictation_timer is not None:
            # It fires a second and a half after the text landed, by which
            # time this panel's widgets may not exist.
            self._dictation_timer.Stop()
            self._dictation_timer = None
        # A held backend outlives a single turn, so closing the tab is what
        # closes it, live turn or not. getattr because this runs during
        # teardown, on a panel closed before __init__ finished or on a stub.
        drop = getattr(self, "_drop_held_backends", None)
        if drop is not None:
            drop()
        worker = self._worker
        if worker is None or not worker.is_alive():
            return None
        if not wait:
            thread = threading.Thread(target=worker.cancel, name="cancel-worker", daemon=True)
            thread.start()
            return thread
        worker.cancel()
        worker.join(timeout=_CANCEL_JOIN_SECONDS)
        return None
