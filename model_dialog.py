"""The model picker for /model: one box for the model, one for effort.

Extracted from ``blindpilot_app.py`` following question_dialog.py, so
the picker's UI behaviour can be changed and reviewed independently.
Assistive-technology verification of this dialog is covered by the
automated behaviour tests; per issue #136's agreed exception, no real
screen-reader verification is represented as completed for this
extraction.

Both lists come from the probe that ran just before this opened. The
first entry in each box is "leave it alone" and says what that
currently means; picking it passes nothing at all. An alias is shown
with the version it resolves to and mapped back to the bare alias
when picked. Esc cancels, and focus starts in the model box.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from typing import List, Optional

from agent_backends import (
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
    BACKENDS,
    backend_label,
    codex_model_options,
    find_backend_cli,
    freebuff_model_options,
    invalidate_backend_cache,
    normalize_backend,
    opencode_model_options,
    subprocess_env,
)
from app_config import REMOTE_HERMES
from cli_install import _find_claude
from hermes_backend import hermes_model_options
from process_utils import no_window_kwargs

import wx

# Dialog margins, matching blindpilot_app.PAD and PAD_DIALOG (kept
# local so this module does not import the application module).
PAD = 8
PAD_DIALOG = 12

DEFAULT_CHOICE = "(CLI default)"


AUTO_MODEL = "<BlindPilot auto>"


AUTO_MODEL_CHOICE = "Auto (choose for each task)"


def _plain(text: str) -> str:
    """Text for a label or a list entry, with markdown backticks taken out."""
    return text.replace("`", "")


@dataclass
class ModelOptions:
    """What a backend reports it can be asked for, plus what it is using now."""

    models: List[str]
    efforts: List[str]
    current_model: str = ""  # display name, e.g. "Opus 5"
    current_effort: str = ""  # e.g. "medium"
    error: str = ""  # non-empty when the probe fell back to defaults
    from_cache: bool = False  # served from a recent probe, not a fresh one
    # alias -> the model it currently resolves to, e.g. "opus" -> "Opus 5.5".
    labels: dict[str, str] = field(default_factory=dict)


def _keep_choice(current: str) -> str:
    """First combo-box entry: pass no flag, and say what that currently means."""
    current = _plain(current)
    return f"{DEFAULT_CHOICE}, currently {current}" if current else DEFAULT_CHOICE


def _model_choice(model: str, labels: dict[str, str]) -> str:
    """A model-box entry: the alias, plus the version it means when known."""
    label = labels.get(model, "")
    return f"{model}: {label}" if label and label.lower() != model.lower() else model


class ModelDialog(wx.Dialog):
    """Model picker for /model: one combo box for the model, one for effort.

    Both lists come from the probe that ran just before this opened, so the
    choices are whatever the selected backend actually accepts. The first entry
    in each box names what that backend is using right now -- picking it passes
    nothing at all, so it keeps that. The model box is editable so a full model
    ID can be typed; the effort box is a fixed list. Esc cancels.

    A backend that takes no effort level gets an empty list for it, so the box
    offers only "leave it alone" rather than a control the turn would ignore.
    """

    def __init__(
        self,
        parent: wx.Window,
        options: "ModelOptions",
        selected_model: str,
        selected_effort: str,
        backend_name: str = "CLI",
        allow_auto_model: bool = False,
    ):
        super().__init__(parent, title="Model")

        current = _plain(options.current_model) or "unknown"
        if options.current_effort:
            current = f"{current}, effort {_plain(options.current_effort)}"
        lines = [f"{backend_name} reports the current model as: {current}."]
        if selected_model or selected_effort:
            lines.append(
                "This tab overrides that with: "
                f"model {'automatic routing' if selected_model == AUTO_MODEL else selected_model or 'unchanged'}, "
                f"effort {selected_effort or 'unchanged'}."
            )
        if options.error:
            lines.append(options.error)
        if allow_auto_model:
            lines.append("Auto may switch providers, including free models, for each task.")
        summary = wx.StaticText(self, label="\n".join(lines))
        summary.Wrap(self.FromDIP(520))

        # "Leave it alone" is the first entry in both boxes, and it says what
        # leaving it alone actually means rather than just "(CLI default)".
        self._model_keep = _keep_choice(options.current_model)
        # Each alias is shown with the version it resolves to, "opus: Opus 5.5",
        # and mapped back to the bare alias when picked.
        self._model_by_choice = {_model_choice(m, options.labels): m for m in options.models}
        if allow_auto_model:
            self._model_by_choice[AUTO_MODEL_CHOICE] = AUTO_MODEL
        self._effort_keep = _keep_choice(options.current_effort)

        model_label = wx.StaticText(self, label="&Model:")
        self.model_box = wx.ComboBox(
            self,
            choices=[self._model_keep, *self._model_by_choice],
            style=wx.CB_DROPDOWN,
        )
        self.model_box.SetName("Model")
        self.model_box.SetValue(
            (
                AUTO_MODEL_CHOICE
                if selected_model == AUTO_MODEL and allow_auto_model
                else _model_choice(selected_model, options.labels)
                if selected_model
                else self._model_keep
            )
        )

        efforts = list(options.efforts)
        if selected_effort and selected_effort not in efforts:
            # Kept as a choice. A read-only box cannot show a value it does not
            # list, so OK would have silently cleared this tab's override.
            efforts.append(selected_effort)
        effort_label = wx.StaticText(self, label="&Effort:")
        self.effort_box = wx.ComboBox(
            self,
            choices=[self._effort_keep, *efforts],
            style=wx.CB_DROPDOWN | wx.CB_READONLY,
        )
        self.effort_box.SetName("Effort")
        self.effort_box.SetStringSelection(selected_effort or self._effort_keep)

        grid = wx.FlexGridSizer(2, 2, self.FromDIP(PAD), self.FromDIP(PAD))
        grid.AddGrowableCol(1, 1)
        grid.Add(model_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.model_box, 1, wx.EXPAND)
        grid.Add(effort_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.effort_box, 1, wx.EXPAND)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        pad = self.FromDIP(PAD_DIALOG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(summary, 0, wx.ALL, pad)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.model_box.SetFocus()
        self.CentreOnParent()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()

    def selection(self) -> tuple[str, str]:
        """(model, effort), with "" for either one left as the backend has it."""
        model = self.model_box.GetValue().strip()
        model = self._model_by_choice.get(model, model)
        effort = self.effort_box.GetValue().strip()
        return (
            "" if model in (DEFAULT_CHOICE, self._model_keep) else model,
            "" if effort in (DEFAULT_CHOICE, self._effort_keep) else effort,
        )


# ----- The catalog itself: probed from the CLI, then cached -----
# Nothing here is hard-coded as truth: the aliases and effort levels are
# read back from the installed CLI, because both lists change as the
# backends ship new models. The fallbacks are only for when the probe
# fails (offline, CLI missing, output format changed). Probing costs a
# CLI start-up, so results are reused for a while, stamped with the
# binary that answered so upgrading it drops the entry.

# ----- /model: what the CLI currently offers -----
# Nothing here is hard-coded as truth: the model aliases and effort levels are
# read back from the installed CLI every time the dialog opens, because both
# lists change as Claude Code ships new models. These constants are only the
# last-resort fallback for when the probe fails (offline, CLI missing, output
# format changed).
_FALLBACK_MODELS = ["default", "opus", "sonnet", "haiku", "fable", "opusplan"]


_FALLBACK_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _parse_model_aliases(text: str) -> List[str]:
    """Model names out of the CLI's `/model` usage line.

    The line looks like::

        Usage: /model <name>. Available: sonnet, opus, ..., or a full model ID.
    """
    match = re.search(r"Available:\s*(.+)", text, re.I)
    if not match:
        return []
    tail = match.group(1).strip().rstrip(".")
    names: List[str] = []
    for part in tail.split(","):
        name = part.strip().rstrip(".")
        # Drop the trailing prose ("or a full model ID") and any stray blanks;
        # every real alias or model ID is a single word.
        if not name or " " in name:
            continue
        if name not in names:
            names.append(name)
    return names


def _parse_current_model(text: str) -> tuple[str, str]:
    """(display name, effort) from the CLI's `Current model:` status line."""
    match = re.search(r"Current model:\s*([^\n(]+)(?:\(effort:\s*([^)]*)\))?", text, re.I)
    if not match:
        return "", ""
    return match.group(1).strip(), (match.group(2) or "").strip()


def _parse_alias_target(text: str) -> str:
    """The model an alias names, out of `/model <alias>`'s reply::

    Set model to `Opus 5.5` for this session only
    """
    match = re.search(r"Set model to\s*`?([^`\n]+?)`?\s+for this session", text, re.I)
    return match.group(1).strip() if match else ""


def _parse_effort_levels(help_text: str) -> List[str]:
    """Effort levels out of the `--effort <level>` entry in `claude --help`.

    The help text is hard-wrapped, so it is flattened before matching:
    ``--effort <level> Effort level for the current session (low, medium, …)``.
    """
    flat = " ".join(help_text.split())
    match = re.search(r"--effort <level>(.*?)(?=\s--\w|$)", flat)
    if not match:
        return []
    inner = re.search(r"\(([^)]*)\)", match.group(1))
    if not inner:
        return []
    levels = [p.strip() for p in inner.group(1).split(",")]
    return [lv for lv in levels if lv and " " not in lv]


def _run_claude(binary: str, args: List[str], cwd: Optional[str], timeout: int) -> str:
    """Run the CLI and return stdout+stderr, or "" if it could not be run."""
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            env=subprocess_env(binary),
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def _resolve_claude_aliases(binary: str, aliases: List[str], cwd: Optional[str]) -> dict:
    """Ask the CLI what each alias means today, all at once.

    Claude Code lists only aliases ("opus"), which says nothing about which
    Opus. `/model <alias>` in print mode names the model and changes nothing
    outside that one throwaway session.
    """

    def one(alias: str) -> str:
        reply = _run_claude(
            binary,
            ["-p", f"/model {alias}", "--no-session-persistence", "--output-format", "text"],
            cwd,
            45,
        )
        return _parse_alias_target(reply)

    with ThreadPoolExecutor(max_workers=max(1, len(aliases))) as pool:
        targets = list(pool.map(one, aliases))
    return {alias: target for alias, target in zip(aliases, targets, strict=True) if target}


_probe_lock = threading.Lock()


# "backend:cwd" -> (when it was probed, what came back, the CLI's stamp). The
# stamp is the CLI's path, mtime and size, so upgrading it drops the entry.
_probe_cache: dict[str, tuple[float, ModelOptions, str]] = {}


def invalidate_model_options(backend: str) -> None:
    """Clear model catalogs after an update or an explicit `/models` refresh."""
    selected = normalize_backend(backend)
    with _probe_lock:
        prefix = f"{selected}:"
        for key in [key for key in _probe_cache if key.startswith(prefix)]:
            _probe_cache.pop(key, None)
    invalidate_backend_cache(selected)


def _cli_stamp(binary: str) -> str:
    try:
        st = os.stat(binary)
        return f"{binary}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        return binary


def _remember_model_options(
    backend: str, cwd: Optional[str], binary: str, options: ModelOptions
) -> None:
    with _probe_lock:
        _probe_cache[f"{backend}:{cwd or ''}"] = (time.time(), options, _cli_stamp(binary))


def cached_model_options(
    cwd: Optional[str], max_age: float, backend: str = BACKEND_CLAUDE
) -> Optional[ModelOptions]:
    """A probe result no older than `max_age` seconds, or None.

    Never blocks, so it is safe on the GUI thread: the CLI is not searched
    for (on macOS that can mean a login shell), the entry remembers which
    binary answered and is dropped if that file has changed since.
    """
    if max_age <= 0:
        return None
    with _probe_lock:
        entry = _probe_cache.get(f"{normalize_backend(backend)}:{cwd or ''}")
    if entry is None:
        return None
    when, options, stamp = entry
    if (time.time() - when) > max_age or _cli_stamp(stamp.split("|", 1)[0]) != stamp:
        return None
    return replace(options, from_cache=True)


def probe_model_options(
    cwd: Optional[str] = None,
    max_age: float = 0,
    backend: str = BACKEND_CLAUDE,
) -> ModelOptions:
    """Ask the installed CLI which models and effort levels it accepts.

    Also reports the model and effort the CLI says it is using right now. Pass
    `max_age` to accept a recent cached answer instead of shelling out — with
    0 (the default) it always asks the CLI, which costs a CLI start-up, so this
    is blocking: call it off the GUI thread.
    """
    backend = normalize_backend(backend)
    binary = _find_claude() if backend == BACKEND_CLAUDE else find_backend_cli(backend)
    # A Hermes reached over the network is the one backend that needs no local
    # copy: the catalog comes from the server itself.
    remote_hermes_url = REMOTE_HERMES.url() if backend == BACKEND_HERMES else ""
    if binary is None and not remote_hermes_url:
        label = backend_label(backend)
        return ModelOptions(
            list(_FALLBACK_MODELS) if backend == BACKEND_CLAUDE else [],
            # Only offer effort levels for a backend that actually accepts one;
            # otherwise the picker shows a control its protocol ignores.
            list(_FALLBACK_EFFORTS) if BACKENDS[backend].supports_effort else [],
            error=f"{label} was not found.",
        )

    if backend == BACKEND_MUSEAI:
        # muse.ai picks its own model on its own machine; there is no list to offer.
        return ModelOptions([], [])

    fresh = cached_model_options(cwd, max_age, backend)
    if fresh is not None:
        return fresh

    if backend == BACKEND_HERMES:
        # A Hermes reached over the network answers the same model request as
        # one on this machine: `hermes serve` dispatches its WebSocket through
        # the identical JSON-RPC handlers. This used to return an error telling
        # the user to pick the model "on the machine it runs on", which for a
        # headless server (the whole point of the remote mode) meant there was
        # nowhere to pick it at all.
        models, efforts, current_model, current_effort, error = hermes_model_options(
            cwd,
            remote_url=remote_hermes_url,
            remote_token=REMOTE_HERMES.key if remote_hermes_url else "",
            remote_credential=(REMOTE_HERMES.credential if remote_hermes_url else "token"),
            remote_username=REMOTE_HERMES.username if remote_hermes_url else "",
        )
        options = ModelOptions(models, efforts, current_model, current_effort, error)
        if models and binary is not None:
            # Only the local path has a binary to stamp the cache against; a
            # remote catalog is re-read instead of being keyed on a file that
            # does not exist here.
            _remember_model_options(backend, cwd, binary, options)
        return options

    if binary is None:
        # Only the remote-Hermes path above is allowed to get this far without
        # a local CLI, and it has already returned.
        return ModelOptions([], [], error=f"{backend_label(backend)} was not found.")

    if backend == BACKEND_CODEX:
        models, efforts, current_model, current_effort, error = codex_model_options(cwd)
        options = ModelOptions(models, efforts, current_model, current_effort, error)
        if models:
            _remember_model_options(backend, cwd, binary, options)
        return options

    if backend == BACKEND_FREEBUFF:
        models, current_model, error = freebuff_model_options()
        return ModelOptions(models, [], current_model, "", error)

    if backend == BACKEND_OPENCODE:
        models, efforts, current_model, current_effort, error = opencode_model_options(cwd)
        options = ModelOptions(models, efforts, current_model, current_effort, error)
        if models:
            _remember_model_options(backend, cwd, binary, options)
        return options

    if backend == BACKEND_MUSE:
        # The catalog comes from Muse's own `model/list` on a live `muse
        # serve` host, which is the same request a model switch is served
        # by. On Windows that host runs inside WSL, so the probe is asked
        # of the translated directory rather than this one.
        from muse_backend import muse_model_options

        models, efforts, current_model, current_effort, error = muse_model_options(cwd)
        options = ModelOptions(models, efforts, current_model, current_effort, error)
        if models and binary is not None:
            _remember_model_options(backend, cwd, binary, options)
        return options

    if backend in (BACKEND_GEMINI, BACKEND_ANTIGRAVITY):
        # Gemini CLI has no model-list command, so its aliases are offered
        # with whatever the Gemini API lists for the key; agy answers
        # `agy models` itself, signed in or on a key.
        from google_backend import antigravity_model_options, gemini_model_options

        probe = gemini_model_options if backend == BACKEND_GEMINI else antigravity_model_options
        models, efforts, current_model, current_effort, error = probe()
        options = ModelOptions(models, efforts, current_model, current_effort, error)
        if models and binary is not None:
            _remember_model_options(backend, cwd, binary, options)
        return options

    if backend == BACKEND_COPILOT:
        # No model-list command: "auto" and the configured model, and any id
        # typed into the picker.
        from copilot_backend import copilot_model_options

        models, efforts, current_model, current_effort, error = copilot_model_options()
        return ModelOptions(models, efforts, current_model, current_effort, error)

    if backend == BACKEND_COMMANDCODE:
        # The catalog comes from `command-code --list-models`, and the model
        # and effort in use are read out of its stored config rather than
        # asked for, so the picker opens on what is actually selected.
        from commandcode_backend import commandcode_model_options

        models, efforts, current_model, current_effort, error = commandcode_model_options(cwd)
        options = ModelOptions(models, efforts, current_model, current_effort, error)
        if models and binary is not None:
            _remember_model_options(backend, cwd, binary, options)
        return options

    # The two probes are independent, so the help text is fetched while the
    # slower `/model` status call is still running.
    help_text: List[str] = []
    help_thread = threading.Thread(
        target=lambda: help_text.append(_run_claude(binary, ["--help"], None, 30)),
        daemon=True,
    )
    help_thread.start()
    # `/model` with no argument only prints status — it does not start a turn.
    status = _run_claude(binary, ["-p", "/model", "--output-format", "text"], cwd, 45)
    models = _parse_model_aliases(status)
    current_model, current_effort = _parse_current_model(status)
    labels = _resolve_claude_aliases(binary, models, cwd) if models else {}
    help_thread.join(30)
    efforts = _parse_effort_levels(help_text[0] if help_text else "")

    problems = []
    if not models:
        models = list(_FALLBACK_MODELS)
        problems.append("model list")
    if not efforts:
        efforts = list(_FALLBACK_EFFORTS)
        problems.append("effort levels")
    error = ""
    if problems:
        error = f"Could not read the {' and '.join(problems)} from Claude Code; showing the built-in list."
    options = ModelOptions(models, efforts, current_model, current_effort, error, labels=labels)
    if not problems:
        # Only a clean answer is worth reusing; a failed probe should be retried.
        _remember_model_options(backend, cwd, binary, options)
    return options
