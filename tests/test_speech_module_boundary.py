"""Where the screen reader's state lives, and where it must not.

`announce()` reassigns the reader connection when the reader restarts. A name
imported from `screen_speech` would be a copy taken at import time and would
go stale the moment the connection was rebuilt, so the bug report and the
startup warning would report a reader that was there as missing.

These tests pin the boundary: the functions are shared by import, the state is
read through the module, and the old module no longer carries a copy of the
state that a test could patch into a silent no-op.
"""

from __future__ import annotations

import blindpilot_app
import screen_speech


def test_the_old_module_no_longer_carries_the_reader_state():
    # A test that patched one of these on blindpilot_app would look like it
    # worked and change nothing, because the running code reads the name in
    # screen_speech. Making the name absent turns that into an AttributeError.
    for name in (
        "_SPEAKER",
        "_make_speaker",
        "_speaker_retry_after",
        "_last_announcement",
        "_MAC_ANNOUNCE",
        "_linux_announce",
        "_linux_native_announce",
    ):
        assert not hasattr(blindpilot_app, name), name


def test_the_functions_are_shared_by_import():
    # The same function object under two names: a caller in either module
    # reaches the same code, and replacing it in one place reaches both.
    assert blindpilot_app.announce is screen_speech.announce
    assert blindpilot_app.repeat_last_announcement is screen_speech.repeat_last_announcement


def test_the_reader_state_is_read_through_the_module(monkeypatch):
    # The bug report says how announcements reach the reader. It has to see
    # the connection as it is now, not as it was when the module was imported.
    # Windows is the branch that reports it; macOS and Linux answer with
    # their own line and never look at the connection.
    monkeypatch.setattr(blindpilot_app.platform, "system", lambda: "Windows")

    monkeypatch.setattr(screen_speech, "_SPEAKER", object())
    assert blindpilot_app._speech_route() != "none found"

    monkeypatch.setattr(screen_speech, "_SPEAKER", None)
    assert blindpilot_app._speech_route() == "none found"


def test_repeat_reaches_the_announcement_it_was_given(monkeypatch):
    # repeat_last_announcement lives beside announce, so it reaches announce.
    # If it were ever moved apart, this would say so rather than the repeat
    # quietly saying the wrong line.
    said: list[str] = []
    monkeypatch.setattr(screen_speech, "announce", lambda text, urgent=False: said.append(text))
    monkeypatch.setattr(screen_speech, "_last_announcement", "the last line")

    screen_speech.repeat_last_announcement()

    assert said == ["the last line"]
