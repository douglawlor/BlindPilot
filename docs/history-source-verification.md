# History source status verification

Issue: [Report conversation history sources that could not be read](https://github.com/serrebidev/BlindPilot/issues/107).

Real screen-reader verification is pending. Automated tests do not establish
that the announcements or native controls work with a screen reader.

## Automated validation

Ruff lint and formatting checks pass, and mypy reports no issues in 30 source
files. The full warning-as-error run reports 2,700 passed, 18 skipped and no
failures, with the process exiting 0. Three consecutive runs agreed, with the
order shuffled by pytest-randomly each time.

Two problems stood between this work and a green run, and both are fixed.

- The four failures that also reproduced in a clean HEAD snapshot are gone.
  Two were workflow checks: `sync-upstream.yml` granted every job in the file
  a write token and ran several commands in one step with no `shell:`, so a
  failure in the first of them would have passed. Two were New Session dialog
  checks that assumed the folder box starts empty, which it does not on a
  machine that has used a folder before; they now empty it themselves.
- The abnormal shutdown is resolved. The cause was not the history work: pytest
  redirects the test process's standard input and error to files it owns and
  closes the handles they had, and `subprocess` still asks Windows for the
  handles the process was started with. Duplicating a stale handle fails with
  "The request is not supported", so every test that started a child failed
  with it, and which tests those were depended on the order the suite ran in.
  `tests/conftest.py` now points those two handles at handles a child can
  inherit.

## Screen-reader check

Run `python -m tools.history_status_demo` from the repository with your screen
reader running. The demonstration uses temporary example history and settings;
it does not start a coding backend or change your conversations.

1. Confirm that Recent Conversations announces the Codex source failure and
   still offers "Example conversation."
2. Tab to "History source status." Read the full explanation and recovery
   action with the screen reader. Confirm that it contains no private paths.
3. Press Enter while reading the status. Confirm that it does not open a
   conversation. Repeat with Ctrl+Enter.
4. Return to the conversation list and press Ctrl+Enter. Confirm that the example
   opens in a "Read: Example conversation" tab. Confirm that the prompt and
   "The fallback works" answer can be read, and that the database warning is
   announced and available in "History source status."
5. Use Ctrl+Tab and Ctrl+Shift+Tab to switch between the reading tab and the
   session. F6 moves between the document and tab strip. Confirm that arrowing
   the strip keeps focus there. Escape or Ctrl+W closes the reading tab and
   returns to the originating session. Press Ctrl+Shift+H to reopen Recent
   Conversations. The demonstration repairs its databases on the second scan;
   confirm that the warning disappears and the example remains available.
6. Reopen the example. Confirm that no source warning is announced. Press
   Escape to close the reading tab, then Alt+F4 to close the demonstration.

Record the platform, screen reader and version, steps checked, and any
unexpected speech or focus behavior before marking the accessibility work
verified. Check NVDA and JAWS on Windows and VoiceOver on macOS where available.
