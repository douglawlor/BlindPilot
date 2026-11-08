"""Conversation search and relay wording live in conversation_search.

blindpilot_app re-exports the same objects, so call sites and tests
that reach them as ``app.search_turns`` keep working.
"""

import blindpilot_app as app
import conversation_search
from session_history import HistoryTurn

NAMES = ("search_turns", "search_all_conversations", "relay_message", "relay_setup", "said_done")


def test_conversation_search_owns_the_helpers():
    for name in NAMES:
        assert callable(getattr(conversation_search, name)), name
    assert conversation_search.search_turns.__module__ == "conversation_search"


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(conversation_search, name), name


def test_search_and_relay_behave_as_before():
    turns = [HistoryTurn(prompt="Fix the build", response="The build is fixed")]
    found = conversation_search.search_turns(turns, "BUILD")
    assert found and found[0][0] == 0 and "build" in found[0][1]
    assert conversation_search.search_turns(turns, "") == []
    assert conversation_search.relay_message("Claude", " hi ") == "Claude replied:\n\nhi"
    assert conversation_search.said_done("All good\nDONE") is True
    assert conversation_search.said_done("DONE deal") is False
