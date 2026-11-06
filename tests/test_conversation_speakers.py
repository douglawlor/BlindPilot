"""Reply attribution survives history restore, streaming and transcript copies."""

from types import SimpleNamespace

import pytest

import blindpilot_app as app
from doubles import panel_stub
from markdown_rows import Row, parse_response, reassemble, reassemble_all
from session_history import HistoryEntry, HistoryTurn


@pytest.mark.parametrize("backend", ["opencode", "claude", "codex"])
def test_history_document_uses_the_recorded_backend(backend):
    entry = HistoryEntry(backend, "recorded-session", "Example", "", 0)
    dialog = SimpleNamespace(_entry=entry, _title=entry.title)
    speaker = app.backend_label(backend)
    turns = [HistoryTurn(prompt="Question", response="Answer")]
    fragment = app.ConversationWebViewPanel._conversation_html(dialog, turns)
    text = app.ConversationWebViewPanel._conversation_text(dialog, turns)
    assert f"<h2>{speaker}, exchange 1</h2>" in fragment
    assert f"{speaker}, exchange 1\nAnswer" in text


def test_transcripts_keep_speaker_separate_from_reply_payload():
    rows = [Row("you", "You: Question", "Question", 1)]
    answer = parse_response("First paragraph.\n\nSecond paragraph.", 1, "OpenCode")
    rows.extend(answer)
    assert answer[1].label == "OpenCode: First paragraph."
    assert answer[1].payload == "First paragraph."
    expected = "You: Question\n\nOpenCode: First paragraph.\n\nSecond paragraph."
    assert reassemble(rows, 1) == expected
    assert reassemble_all(rows) == (
        "You: Question\n\nResponse 1\n\nOpenCode: First paragraph.\n\nSecond paragraph."
    )


def test_code_first_reply_retains_attribution_without_changing_code():
    code = 'print("hello")'
    rows = parse_response(f"```python\n{code}\n```", 1, "OpenCode")
    assert rows[1].payload == code
    assert reassemble(rows, 1) == f"OpenCode:\n```python\n{code}\n```"


def test_header_only_response_retains_its_speaker():
    rows = [Row("header", "Response 1", "Answer", 1, speaker="OpenCode")]
    assert reassemble(rows, 1) == "OpenCode: Answer"


def test_empty_response_does_not_invent_an_answer_from_its_speaker():
    assert reassemble(parse_response("", 1, "OpenCode"), 1) == ""


def test_restored_history_keeps_backend_and_session_and_labels_the_reply(monkeypatch):
    monkeypatch.setattr(app, "_remote_sessions", dict)
    panel = panel_stub(
        _drop_held_backends=lambda: None,
        _on_title=lambda *_args: None,
        backend_changed=lambda: None,
    )
    entry = HistoryEntry("opencode", "original-session", "Example", "", 0)
    app.SessionPanel.restore_history(panel, entry, [HistoryTurn("Question", "Answer")])
    assert panel._session_id == "original-session"
    assert panel._session_backend == panel._backend == "opencode"
    speaker = app.backend_label("opencode")
    answer = next(row for row in panel._rows if row.kind == "prose")
    assert answer.label == f"{speaker}: Answer"
    assert reassemble(panel._rows, 1) == f"You: Question\n\n{speaker}: Answer"


def test_streamed_reply_keeps_main_and_subagent_names_in_copied_text():
    panel = panel_stub()
    panel._stream_answer_rows("Main reply.", 1, "OpenCode")
    panel._stream_answer_rows("Subagent reply.", 1, "OpenCode subagent")
    panel._flush_live_answer()
    assert (
        reassemble(panel._rows, 1) == "OpenCode: Main reply.\n\nOpenCode subagent: Subagent reply."
    )
