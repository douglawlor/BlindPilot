"""Text as it will be read aloud or shown as a one-line label.

Pure formatting helpers with no GUI state: how long something took, how
full the context is, what a result row is called, and what is read back
when a message is sent. They live apart from the window so the wording
can be tested without building one.

blindpilot_app re-exports these names, so call sites and tests that
reach them as ``app._sent_readback`` keep working.
"""

from __future__ import annotations

import re


def _spoken_duration(seconds: float) -> str:
    """'2 minutes 5 seconds': how long something took, as it is said."""
    minutes, secs = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    parts = [(hours, "hour"), (minutes, "minute"), (secs, "second")]
    said = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n]
    return " ".join(said) or "0 seconds"


def _context_line(used: int, window: int) -> str:
    """'Context 62% full: 124,000 of 200,000 tokens', as far as it is known."""
    if not used:
        return "Context: not reported yet"
    if not window:
        return f"Context: {used:,} tokens used"
    return f"Context {round(100 * used / window)}% full: {used:,} of {window:,} tokens"


# A CommonMark fenced code block: three or more backticks or tildes, closed by
# a run of the same character at least as long, or by the end of the text.
_FENCED_BLOCKS = [
    re.compile(rf"(?ms)^ {{0,3}}({mark}{{3,}})[^\n]*\n.*?(?:^ {{0,3}}\1{mark}*[ \t]*$|\Z)")
    for mark in ("`", "~")
]


def _sent_readback(text: str, limit: int = 300) -> str:
    """'Sent: fix the build', said as the message goes, long ones cut short."""
    for fence in _FENCED_BLOCKS:
        text = fence.sub(" Code block omitted. ", text)
    words = text.split()
    if not words:
        return "Sent"
    said, rest = "", 0
    for index, word in enumerate(words):
        if len(said) + len(word) > limit:
            rest = len(words) - index
            if not said:
                # One long path or URL: its start, rather than nothing at all.
                said, rest = word[:limit], rest - 1
            break
        said = f"{said} {word}" if said else word
    more = f"... and {rest} more {'word' if rest == 1 else 'words'}" if rest else ""
    return f"Sent: {said}{more}"


def _result_label(text: str) -> str:
    """Short, screen-reader-friendly preview line for a result row."""
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    first = " ".join(first.split())
    if len(first) > 100:
        first = first[:99] + "…"
    return f"Result: {first}" if first else "Result"
