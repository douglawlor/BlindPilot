"""Run one backend's sign-in where the user cannot see it.

Extracted from ``blindpilot_app.py`` so the sign-in behaviour can be
changed and reviewed independently, ahead of the setup wizard that
is its only caller. Assistive-technology verification of the
wizard flows that use it is covered by the automated behaviour
tests; per issue #136's agreed exception, no real screen-reader
verification is represented as completed for this extraction.

Only a real sign-in page is handed back by _first_login_url: never
a loopback callback (the CLI announces its local server before the
address to actually visit), and never with the sentence's full stop
stuck to it. Terminal noise is cleaned before anything is read
aloud, and a sign-in that fails says so verbatim.
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
import urllib.parse
from typing import Callable, Optional

from agent_backends import BACKENDS, normalize_backend, subprocess_env
from connect_dialog import _open_web_page
from process_utils import end_process_group, no_window_kwargs, own_group_kwargs

_LOGIN_URL_RE = re.compile(r"https?://[^\s\x1b<>]+", re.IGNORECASE)


# What a CLI says when the sign-in itself went wrong, as opposed to a step of it
# that is still in progress. Worth repeating verbatim: "Login failed: Request
# failed with status code 400" is the only clue there is.
_LOGIN_FAILED_RE = re.compile(
    r"login failed|sign[- ]?in failed|authentication failed|not authenticated",
    re.IGNORECASE,
)


# The callback a CLI listens on is not the page anyone signs in on. Codex
# announces "Starting local login server on http://localhost:1455." before it
# prints the address to actually visit, and opening the first URL in its output
# lands the user on a blank local port.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


# Said instead of a callback address. The CLI is only announcing the local
# server the browser comes back to, and that port is not a place anyone can
# sign in -- reading it out sends a listener to the one address that cannot
# work.
_LOOPBACK_PROGRESS = "Waiting for the sign-in page to open…"


_LOGIN_NOISE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Z0-9])")


def _login_speech(text: str) -> str:
    """A line of CLI output as it should be read out.

    Colour and cursor codes are invisible on a screen and gibberish out loud,
    and a character the CLI wrote in some other encoding arrives here as
    U+FFFD, which NVDA announces in the middle of the sentence it interrupts.
    """
    return " ".join(_LOGIN_NOISE_RE.sub("", text).replace("�", "").split())


def _first_login_url(text: str) -> str:
    """The sign-in address in a line of CLI output, or "" if it has none."""
    for match in _LOGIN_URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:)]}'\"")
        host = (urllib.parse.urlsplit(url).hostname or "").casefold()
        if host in _LOOPBACK_HOSTS:
            continue
        return url
    return ""


class BackendLogin:
    """Runs a provider CLI's sign-in from a window that has no console.

    Every backend signs in the same shape: print an address, get the browser to
    it, and wait. What differs is who opens the browser and whether the CLI then
    wants a code typed back at it. Both are declared per backend, so this drives
    all of them and none of them is a special case in the caller.

    The output is read a character at a time rather than a line at a time,
    because the code prompt ("Paste code here if prompted > ") is written
    without a newline after it. Waiting for one would hide the very prompt the
    user has to answer, which is what made a sign-in look like it had frozen.

    The callbacks are called on the worker thread; a GUI caller marshals them.
    """

    def __init__(
        self,
        backend: str,
        binary: str,
        *,
        timeout: float = 300.0,
        opener: Optional[Callable[[str], bool]] = None,
        popen: Optional[Callable[..., "subprocess.Popen"]] = None,
    ):
        self.backend = normalize_backend(backend)
        self.binary = binary
        self.url = ""
        self.failure = ""
        self._info = BACKENDS[self.backend]
        self._timeout = timeout
        # Same checked door as the opencode sign-in uses: a CLI's address is
        # already constrained to http or https by the pattern it is read out
        # of, and this leaves one place in the file that opens anything.
        self._opener = opener or _open_web_page
        self._popen = popen or subprocess.Popen
        self._proc: Optional[subprocess.Popen] = None
        self._writing = threading.Lock()

    # ---- Driving it ----
    def run(
        self,
        on_progress: Callable[[str], None],
        on_url: Callable[[str, bool], None],
        on_code_prompt: Callable[[str, str], None],
    ) -> int:
        """Sign in. Returns the CLI's exit code, -1 on timeout, -2 if it never ran."""
        args = [self.binary, *self._info.login_args]
        try:
            proc = self._popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # A pipe, not DEVNULL: a CLI that wants the code from the
                # browser has to have somewhere to read it from.
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=subprocess_env(self.binary),
                # A cancelled sign-in must not leave the CLI's own child still
                # running and still waiting for a browser nobody is in.
                **own_group_kwargs(),
                **no_window_kwargs(),
            )
        except OSError:
            return -2
        self._proc = proc
        pattern = self._info.login_code_prompt
        prompt = re.compile(pattern) if pattern else None
        events: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, args=(proc, prompt, events), daemon=True).start()

        deadline = time.monotonic() + self._timeout
        asked = 0
        ended = False
        while not (ended and proc.poll() is not None):
            if time.monotonic() > deadline:
                self._stop()
                return -1
            try:
                item = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                ended = True
                continue
            kind, text = item
            if kind == "prompt":
                # The browser round-trip may still finish on its own, so this is
                # an offer rather than a stop: the caller shows it and reading
                # goes on. Three is enough for a mistyped code without letting a
                # CLI that re-prompts forever keep the dialog up forever.
                asked += 1
                if asked > 3:
                    self._stop()
                    return -1
                deadline = time.monotonic() + self._timeout
                on_code_prompt(text.strip(), self.url)
                continue
            self._announce(text, on_progress, on_url)
        return proc.returncode

    def submit_code(self, code: str) -> None:
        """Answer the CLI's code prompt. Safe to call from another thread."""
        stdin = getattr(self._proc, "stdin", None)
        if stdin is None:
            return
        with self._writing:
            try:
                stdin.write(f"{code}\n")
                stdin.flush()
            except (OSError, ValueError):
                pass

    def open_page(self) -> bool:
        """Put the sign-in address in the browser. False if nothing happened."""
        if not self.url:
            return False
        try:
            return bool(self._opener(self.url))
        except Exception:
            return False

    def cancel(self) -> None:
        self._stop()

    # ---- Internals ----
    def _announce(
        self,
        text: str,
        on_progress: Callable[[str], None],
        on_url: Callable[[str, bool], None],
    ) -> None:
        spoken = _login_speech(text)
        if _LOGIN_FAILED_RE.search(spoken):
            self.failure = spoken
        found = _first_login_url(text)
        if found and not self.url:
            self.url = found
            # Codex's page is opened here, because its own browser launch does
            # not reliably arrive from the hidden process BlindPilot starts. A
            # CLI that opens its own page (Claude Code) is left to it, so the
            # user does not end up with two tabs on the same authorization. The
            # wizard's Open Sign-in Page button opens it either way, for when
            # the browser did not arrive.
            opened = False if self._info.login_opens_browser else self.open_page()
            on_url(found, opened)
            return
        if not found and _LOGIN_URL_RE.search(text):
            # Every URL on this line was a loopback one: the CLI is announcing
            # its own callback server, which is not the sign-in page. Reading
            # it out would send the listener to a local port that cannot sign
            # them in, so say what is actually happening instead.
            on_progress(_LOOPBACK_PROGRESS)
            return
        if spoken:
            on_progress(spoken)

    def _read(self, proc, prompt, events: queue.Queue) -> None:
        stream = proc.stdout
        pending = ""
        try:
            while True:
                char = stream.read(1)
                if not char:
                    break
                if char == "\r":
                    continue
                if char == "\n":
                    events.put(("line", pending))
                    pending = ""
                    continue
                pending += char
                if prompt is not None and prompt.search(pending):
                    events.put(("prompt", pending))
                    pending = ""
        except (OSError, ValueError):
            pass
        finally:
            if pending:
                events.put(("line", pending))
            events.put(None)

    def _stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        # Bounded rather than expected: the signals land immediately, and the
        # wait is only there so a wizard never closes over a live sign-in.
        end_process_group(proc, timeout=5)
