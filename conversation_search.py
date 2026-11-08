"""Searching past conversations, and wording passed between tabs.

Extracted from ``blindpilot_app.py``; blindpilot_app re-exports the
same names. Per issue #136, no real screen-reader verification is
represented as completed for this extraction.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from agent_backends import backend_label
from session_history import HistoryEntry, HistoryTurn, describe_age, load_turns


def search_turns(turns: Sequence[HistoryTurn], term: str) -> list[tuple[int, str, str]]:
    """Where `term` appears in a conversation: (turn index, snippet, whole text).

    Matched without regard to case, in what was asked and what was answered.
    The snippet is the match with some words either side, for a list row.
    """
    wanted = term.casefold()
    found: list[tuple[int, str, str]] = []
    if not wanted:
        return found
    for index, turn in enumerate(turns):
        for who, text in (("You", turn.prompt), ("Answer", turn.response)):
            at = text.casefold().find(wanted)
            if at < 0:
                continue
            start, end = max(0, at - 60), min(len(text), at + len(term) + 60)
            snippet = " ".join(text[start:end].split())
            snippet = ("…" if start else "") + snippet + ("…" if end < len(text) else "")
            found.append((index, f"{who}: {snippet}", f"{who}: {text}"))
    return found


def search_all_conversations(
    entries: Sequence[HistoryEntry],
    term: str,
    on_progress: Optional[Callable[[int, str], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    load: Optional[Callable[[HistoryEntry], List[HistoryTurn]]] = None,
) -> list[tuple[str, HistoryEntry, int, str]]:
    """Search every past conversation for `term`: (row label, entry, turn, whole).

    Pure disk and database work, so the caller runs this on a worker thread
    and brings the rows back to the GUI thread. `on_progress(done, title)`
    fires before each entry is read; when `is_cancelled()` turns true the
    search stops early and the caller discards the partial rows.
    """
    loader = load if load is not None else load_turns
    rows: list[tuple[str, HistoryEntry, int, str]] = []
    for done, entry in enumerate(entries):
        if is_cancelled is not None and is_cancelled():
            break
        if on_progress is not None:
            on_progress(done, entry.title)
        for index, snippet, whole in search_turns(loader(entry), term):
            label = (
                f"{entry.title}, {backend_label(entry.backend)}, "
                f"{describe_age(entry.modified)}: {snippet}"
            )
            rows.append((label, entry, index, whole))
    return rows


def relay_message(speaker: str, text: str) -> str:
    """What one agent's reply says when it is handed to another tab.

    Always prefixed, so a reply that starts with a slash command cannot run it
    in the other tab.
    """
    return f"{speaker} replied:\n\n{text.strip()}"


def relay_setup(me: str, others: Sequence[str], role: str) -> str:
    """What each agent is told once, before its first message in the conversation."""
    text = (
        f"You are {me} in an agent conversation with {' and '.join(others)}, set up by "
        'the person in BlindPilot. Their messages reach you as "... replied:" and are '
        "not from the person."
    )
    if role.strip():
        text += f" Your role: {role.strip()}"
    return (
        text + " When you all agree the work is finished, end your reply with DONE on its "
        "own line.\n\n"
    )


def said_done(text: str) -> bool:
    """Whether a reply ends the conversation: DONE as its last line."""
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1].strip(" *_`.!#>").upper() == "DONE"
