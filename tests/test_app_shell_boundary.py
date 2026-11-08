"""App-shell constants and helpers live in app_shell, not the monolith.

blindpilot_app re-exports the same objects, so call sites and tests
that reach them as ``app.APP_NAME`` keep working.
"""

import blindpilot_app as app
import app_shell

NAMES = (
    "APP_NAME",
    "APP_MODE_AGENT",
    "APP_MODE_CHAT",
    "APP_MODE_LABELS",
    "CHAT_MODEL_ORDERS",
    "PAD",
    "PAD_DIALOG",
    "KEYBOARD_SHORTCUTS",
    "ORIGINAL_APP_CREDIT",
    "MODEL_REFRESH_MINUTES_DEFAULT",
    "about_description",
    "create_desktop_shortcut",
    "hand_over_link",
    "take_handed_links",
)


def test_app_shell_owns_the_helpers():
    for name in NAMES:
        assert getattr(app_shell, name) is not None, name
    assert app_shell.about_description.__module__ == "app_shell"


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(app_shell, name), name


def test_shell_helpers_behave_as_before():
    assert app_shell.APP_MODE_LABELS[app_shell.APP_MODE_AGENT] == "Agent"
    assert app_shell._valid_chat_model_order("name_ascending") == "name_ascending"
    assert app_shell._valid_chat_model_order("sideways") == "newest"
    assert "BlindPilot" in app_shell.about_description()
    assert app_shell._tab_chord_notes() == (
        "Ctrl+Tab",
        "Ctrl+Shift+Tab",
    ) or app_shell._tab_chord_notes() == (
        "Ctrl+Shift+]",
        "Ctrl+Shift+[",
    )
