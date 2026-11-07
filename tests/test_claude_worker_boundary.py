"""ClaudeWorker lives in claude_worker, not the GUI monolith.

blindpilot_app re-exports it (and its helpers) under the same names, so
existing call sites and tests that build ``blindpilot_app.ClaudeWorker``
keep working. Names the worker calls are patched on claude_worker,
where the calls are made from.
"""

import blindpilot_app as app
import claude_worker

NAMES = (
    "ClaudeWorker",
    "ASK_USER_QUESTION_TOOL",
    "_claude_questions",
    "_looks_like_auth_error",
    "_looks_like_refusal",
    "_tool_result_text",
)


def test_claude_worker_owns_the_worker_and_helpers():
    for name in NAMES:
        assert getattr(claude_worker, name) is not None, name


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(claude_worker, name), name


def test_helpers_behave_as_before():
    assert claude_worker._tool_result_text("  hello\n") == "hello"
    assert claude_worker._tool_result_text(None) == ""
    assert claude_worker._looks_like_auth_error("OAuth token has expired")
    assert not claude_worker._looks_like_auth_error("all good")
    assert claude_worker._looks_like_refusal("permission to use Bash has been denied")
    assert claude_worker.ASK_USER_QUESTION_TOOL == "AskUserQuestion"
