"""Private-data-free history diagnostics shared by local and WSL readers.

The names in here are the module's public surface. A reader of a history
source reports what went wrong through these functions, and the window
turns the reports into words. Nothing here carries a path from the user's
disk, transcript text or a raw error.
"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "HistoryIssue",
    "backend_scope",
    "bridge_error",
    "current_issues",
    "history_diagnostics",
    "read_error",
    "record_issues",
    "report_issue",
    "source_is_dir",
    "source_is_file",
]


@dataclass(frozen=True)
class HistoryIssue:
    """A source failure containing no paths, transcript text or raw errors."""

    backend: str
    source: str
    reason: str

    @property
    def message(self) -> str:
        # The label lives in agent_backends, which is a large module. It is
        # looked up here rather than at import time so this module stays a
        # leaf: every reader can then import it at the top of its own file
        # without pulling the whole backend registry in behind it.
        from agent_backends import backend_label

        advice = _ADVICE.get(self.reason, _ADVICE_DEFAULT)
        return f"{backend_label(self.backend)} {self.source} is {self.reason}. {advice}"


# What to do about each kind of failure. A reason that is not in here still
# gets a sentence: an unknown reason must not crash the window that is
# trying to explain it.
_ADVICE = {
    "locked": "Close other apps using this history, then select Reload in Recent Conversations.",
    "unreadable": "Check access permissions, then select Reload in Recent Conversations.",
    "corrupt": "Some conversations or messages may be missing. Check the history in the backend app, then select Reload in Recent Conversations.",
    "unsupported": "Update BlindPilot and check the history in the backend app, then select Reload in Recent Conversations.",
    "missing": "The conversation source is no longer available. Select Reload in Recent Conversations.",
    "too large": "Open this conversation in the backend app.",
}
_ADVICE_DEFAULT = "Select Reload in Recent Conversations to read the sources again."


_ISSUES: ContextVar[list[HistoryIssue] | None] = ContextVar("history_issues", default=None)
_ISSUE_BACKEND: ContextVar[str] = ContextVar("history_issue_backend", default="")


@contextmanager
def history_diagnostics() -> Iterator[list[HistoryIssue]]:
    """Collect safe source failures for this scan or read, isolated per caller."""
    issues: list[HistoryIssue] = []
    token = _ISSUES.set(issues)
    try:
        yield issues
    finally:
        _ISSUES.reset(token)


@contextmanager
def backend_scope(name: str) -> Iterator[None]:
    """Name the backend an issue belongs to, for the length of one read."""
    token = _ISSUE_BACKEND.set(name)
    try:
        yield
    finally:
        _ISSUE_BACKEND.reset(token)


def current_issues() -> list[HistoryIssue]:
    """The issues collected so far in this context."""
    return _ISSUES.get() or []


def record_issues(issues: Iterable[HistoryIssue]) -> None:
    """Merge issues into the caller's collection, when there is one."""
    target = _ISSUES.get()
    if target is None:
        return
    for issue in issues:
        if issue not in target:
            target.append(issue)


def report_issue(reason: str, source: str = "history source") -> None:
    """Record a source failure under the backend being read."""
    issues = _ISSUES.get()
    backend = _ISSUE_BACKEND.get()
    if issues is not None and backend:
        issue = HistoryIssue(backend, source, reason)
        if issue not in issues:
            issues.append(issue)


def read_error(exc: Exception, source: str = "history source") -> None:
    """Classify a failure to read a source, and record it."""
    code = getattr(exc, "sqlite_errorcode", 0) or 0
    primary = code & 0xFF
    if primary in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        reason = "locked"
    elif primary in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB) or isinstance(
        exc, (ValueError, UnicodeError)
    ):
        reason = "corrupt"
    elif isinstance(exc, sqlite3.OperationalError) and any(
        text in str(exc).lower() for text in ("no such table", "no such column", "syntax error")
    ):
        reason = "unsupported"
    elif isinstance(exc, FileNotFoundError):
        reason = "missing"
    elif isinstance(exc, (KeyError, TypeError)):
        reason = "unsupported"
    else:
        reason = "unreadable"
    report_issue(reason, source)


def source_is_file(path: Path) -> bool:
    """Absence is normal for optional stores; failed inspection is not."""
    try:
        regular = stat.S_ISREG(path.stat().st_mode)
        if not regular:
            report_issue("unsupported")
        return regular
    except FileNotFoundError:
        return False
    except OSError as exc:
        read_error(exc)
        return False


def source_is_dir(path: Path) -> bool:
    """Whether a folder can be read. A failure to tell is reported."""
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        read_error(exc, "history folder")
        return False


def bridge_error(stderr: str) -> None:
    """Classify bridge stderr without retaining or exposing any of it."""
    text = (stderr or "").lower()
    if "database is locked" in text or "database table is locked" in text:
        reason = "locked"
    elif "malformed" in text or "not a database" in text:
        reason = "corrupt"
    elif "no such table" in text or "no such column" in text:
        reason = "unsupported"
    else:
        reason = "unreadable"
    report_issue(reason, "WSL history source")
