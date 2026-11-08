"""Speaking to the screen reader, and the state that goes with it.

This module owns every name that decides whether BlindPilot can be heard:
`announce`, `repeat_last_announcement`, the reader connection, and the
worker that speaks on BlindPilot's behalf.

Two rules govern the boundary, and both were learned the hard way.

**The functions are imported by name; the state is read through the
module.** `announce` and `repeat_last_announcement` are functions, and a
function carries the globals of the module it was defined in. Importing
them by name therefore works, and the caller keeps the ability to replace
them. The state -- `_SPEAKER`, `_speaker_retry_after`,
`_last_announcement` -- is reassigned by `announce` itself, so a name
imported from here would be a copy frozen at import time and would go
stale the first time a reader was rebuilt. Anything outside this module
that needs that state must read it as `screen_speech._SPEAKER`.

**The AppKit import stays inside the function.** It is lazy on purpose: at
module scope it would raise ImportError on Windows and Linux, and on
macOS it would load AppKit before wx.App(False) has created the
NSApplication. The names are pulled in where they are used, as
`_bring_to_front` already does.
"""

from __future__ import annotations

import importlib.util
import logging
import platform
import queue
import threading
import time

import wx

from linux_accessibility import announce as _linux_native_announce


# Only whether AppKit exists is settled here. The names themselves are pulled
# in where they are used, as _bring_to_front already does: importing them here
# would leave every one of them undefined on Windows and Linux, where the
# reader of this file -- and any type checker -- has to take it on faith that
# nothing reaches them.
_MAC_ANNOUNCE = False
if platform.system() == "Darwin":
    try:
        _MAC_ANNOUNCE = importlib.util.find_spec("AppKit") is not None
    except (ImportError, OSError, ValueError):
        _MAC_ANNOUNCE = False
# Windows has no equivalent of the NSAccessibility announcement API, and neither
# NVDA nor JAWS speaks a status-bar change on its own. accessible_output2 talks
# to whichever reader is running (NVDA controller client, JAWS COM, SAPI as a
# last resort), which is what makes live narration audible on Windows.
# How long to wait before looking for a reader again. Building the output
# scans for one, which is far too expensive to do per narration line during a
# fan-out, and a reader that is not there now is usually not there a moment
# later either.
_SPEAKER_RETRY_SECONDS = 5.0
_speaker_retry_after = 0.0


def _make_speaker():
    """Open a connection to whichever screen reader is running, or None."""
    if platform.system() != "Windows":
        return None
    try:
        from accessible_output2.outputs.auto import Auto as _AutoOutput  # type: ignore

        return _AutoOutput()
    except Exception:  # library missing, or no usable output found
        return None


# Build accessible_output2 lazily on its worker thread; its Windows backends
# can initialize thread-affine COM connections and scan other processes.
_SPEAKER = None
_SPEAKER_INITIALIZED = False

# accessible_output2 crosses into another process (NVDA, JAWS, or SAPI) and a
# disconnected or busy reader can make that call take seconds. Serialize those
# calls on one daemon worker so GUI callbacks only enqueue narration.
_ANNOUNCEMENT_QUEUE: queue.Queue[str] = queue.Queue()
_ANNOUNCEMENT_LOCK = threading.RLock()
_ANNOUNCEMENT_WORKER: threading.Thread | None = None

# What `announce` said last, for Repeat Last Announcement. A line spoken while
# the reader was busy, or cut off by a keystroke, is otherwise gone for good.
_last_announcement = ""


def _linux_announce(text: str) -> bool:
    """Post an ATK announcement that Orca reads without moving keyboard focus."""
    if wx.GetApp() is None:
        return False
    if not wx.IsMainThread():
        wx.CallAfter(_linux_announce, text)
        return True
    return _linux_native_announce(text)


def repeat_last_announcement() -> None:
    """Say the last announcement again (Conversation menu, Ctrl+Shift+R)."""
    with _ANNOUNCEMENT_LOCK:
        text = _last_announcement or "Nothing has been announced yet"
    announce(text)


def _speak_windows_announcement(text: str) -> None:
    """Deliver one Windows announcement and recover a dropped reader link."""
    global _SPEAKER, _SPEAKER_INITIALIZED, _speaker_retry_after
    if _SPEAKER is None:
        # No reader when BlindPilot started, or the last rebuild failed too.
        # Looked for again occasionally, so a reader started afterwards is
        # picked up instead of leaving the session silent for good.
        now = time.monotonic()
        if now >= _speaker_retry_after:
            _speaker_retry_after = now + _SPEAKER_RETRY_SECONDS
            _SPEAKER = _make_speaker()
            if not _SPEAKER_INITIALIZED and _SPEAKER is None:
                logging.getLogger("blindpilot").warning(
                    "no screen reader output is available: accessible-output2 could not "
                    "connect, so nothing will be spoken on Windows"
                )
            _SPEAKER_INITIALIZED = True
    if _SPEAKER is None:
        return
    try:
        # interrupt=False queues behind other reader speech instead of chopping
        # it off; the single worker preserves BlindPilot's own message order.
        _SPEAKER.speak(text, interrupt=False)
    except Exception:
        # Rebuild a connection that went stale and retry this line once.
        now = time.monotonic()
        if now < _speaker_retry_after:
            return
        _speaker_retry_after = now + _SPEAKER_RETRY_SECONDS
        _SPEAKER = _make_speaker()
        if _SPEAKER is not None:
            try:
                _SPEAKER.speak(text, interrupt=False)
            except Exception:
                _SPEAKER = None


def _announcement_worker(messages: queue.Queue[str]) -> None:
    while True:
        text = messages.get()
        try:
            _speak_windows_announcement(text)
        except Exception:
            # A reader failure must never kill the queue and silence later lines.
            logging.exception("Could not deliver screen-reader announcement")
        finally:
            messages.task_done()


def _enqueue_windows_announcement(text: str) -> None:
    global _ANNOUNCEMENT_WORKER
    _ANNOUNCEMENT_QUEUE.put(text)
    if _ANNOUNCEMENT_WORKER is None or not _ANNOUNCEMENT_WORKER.is_alive():
        worker = threading.Thread(
            target=_announcement_worker,
            args=(_ANNOUNCEMENT_QUEUE,),
            name="screen-reader-announcements",
            daemon=True,
        )
        _ANNOUNCEMENT_WORKER = worker
        worker.start()


def announce(text: str, urgent: bool = False) -> None:
    """Speak `text` via the screen reader without stealing focus.

    macOS uses the NSAccessibility announcement API, Windows goes through
    accessible_output2, and Linux posts an ATK announcement for Orca. Callers
    also mirror the message to the status bar so there is a fallback the review
    cursor can reach.
    """
    global _last_announcement
    if platform.system() == "Windows":
        with _ANNOUNCEMENT_LOCK:
            _last_announcement = text
            _enqueue_windows_announcement(text)
        return
    _last_announcement = text
    if platform.system() == "Linux" and _linux_announce(text):
        return
    if not _MAC_ANNOUNCE:
        return
    try:
        from AppKit import (  # type: ignore
            NSApp,
            NSAccessibilityPostNotificationWithUserInfo,
            NSAccessibilityAnnouncementRequestedNotification,
            NSAccessibilityAnnouncementKey,
            NSAccessibilityPriorityKey,
            NSAccessibilityPriorityHigh,
            NSAccessibilityPriorityMedium,
        )

        app = NSApp()
        if app is None:
            return
        window = app.keyWindow() or app.mainWindow()
        if window is None:
            return
        info = {
            NSAccessibilityAnnouncementKey: text,
            # High is what an error gets, not what everything gets. Posting
            # every line at the speak-now tier meant the same code queued
            # politely on Windows and chopped off the previous line here.
            NSAccessibilityPriorityKey: (
                NSAccessibilityPriorityHigh if urgent else NSAccessibilityPriorityMedium
            ),
        }
        NSAccessibilityPostNotificationWithUserInfo(
            window,
            NSAccessibilityAnnouncementRequestedNotification,
            info,
        )
    except Exception:  # an announcement is never worth raising over
        pass
