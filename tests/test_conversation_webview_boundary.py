"""ConversationWebViewPanel and Turn live in conversation_webview.

blindpilot_app re-exports both names, so existing call sites and
tests keep working. The contract is unchanged by the move: a
conversation page makes the title its one level-one heading and
each exchange a heading under it, and the text fallback reads
"You:" then "Answer:" per exchange.
"""

import blindpilot_app as app
import conversation_webview


def test_conversation_webview_owns_the_panel_and_turn():
    assert conversation_webview.ConversationWebViewPanel is not None
    turn = conversation_webview.Turn(prompt="hi")
    assert turn.prompt == "hi" and turn.response == ""


def test_blindpilot_app_reexports_the_same_objects():
    assert app.ConversationWebViewPanel is conversation_webview.ConversationWebViewPanel
    assert app.Turn is conversation_webview.Turn


def test_page_and_text_shapes_are_unchanged():
    from types import SimpleNamespace

    panel = SimpleNamespace(_title="Chat", _entry=SimpleNamespace(backend="claude"))
    turns = [conversation_webview.Turn(prompt="hi", response="hello")]
    markup = conversation_webview.ConversationWebViewPanel._conversation_html(panel, turns)
    assert "<h1>Chat</h1>" in markup
    text = conversation_webview.ConversationWebViewPanel._conversation_text(panel, turns)
    assert "You, exchange 1\nhi" in text and "exchange 1\nhello" in text
