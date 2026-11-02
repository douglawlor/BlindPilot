"""Private-data-free history diagnostics shared by local and WSL readers."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from agent_backends import backend_label


@dataclass(frozen=True)
class HistoryIssue:
    """A source failure containing no paths, transcript text or raw errors."""

    backend: str
    source: str
    reason: str

    @property
    def message(self) -> str:
        advice = {
            "locked": "Close other apps using this history, then select Reload in Recent Conversations.",
            "unreadable": "Check access permissions, then select Reload in Recent Conversations.",
            "corrupt": "Some conversations or messages may be missing. Check the history in the backend app, then select Reload in Recent Conversations.",
            "unsupported": "Update BlindPilot and check the history in the backend app, then select Reload in Recent Conversations.",
            "missing": "The conversation source is no longer available. Select Reload in Recent Conversations.",
            "too large": "Open this conversation in the backend app.",
        }
        return (
            f"{backend_label(self.backend)} {self.source} is {self.reason}. {advice[self.reason]}"
        )


_ISSUES: ContextVar[Optional[list[HistoryIssue]]] = ContextVar("history_issues", default=None)
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


def _report(reason: str, source: str = "history source") -> None:
    issues = _ISSUES.get()
    backend = _ISSUE_BACKEND.get()
    if issues is not None and backend:
        issue = HistoryIssue(backend, source, reason)
        if issue not in issues:
            issues.append(issue)


def _read_error(exc: Exception, source: str = "history source") -> None:
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
    _report(reason, source)


def _source_is_file(path: Path) -> bool:
    """Absence is normal for optional stores; failed inspection is not."""
    try:
        regular = stat.S_ISREG(path.stat().st_mode)
        if not regular:
            _report("unsupported")
        return regular
    except FileNotFoundError:
        return False
    except OSError as exc:
        _read_error(exc)
        return False


def _source_is_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _read_error(exc, "history folder")
        return False


def _bridge_error(stderr: str) -> None:
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
    _report(reason, "WSL history source")
