"""SavedPromptsDialog lives in saved_prompts_dialog, not the monolith.

blindpilot_app re-exports SavedPromptsDialog, _saved_prompts and
_store_saved_prompts, so existing call sites keep working. Names the
dialog calls are patched on saved_prompts_dialog (and app_config,
where the settings live), where the calls are made from. The
contract is unchanged by the move: entries without text are left
out, the first prompt's text is shown, and focus starts in the list.
"""

import wx

import blindpilot_app as app
import saved_prompts_dialog


def test_saved_prompts_dialog_owns_the_class_and_helpers():
    assert issubclass(saved_prompts_dialog.SavedPromptsDialog, wx.Dialog)
    assert saved_prompts_dialog.PAD_DIALOG == 12
    assert callable(saved_prompts_dialog._saved_prompts)
    assert callable(saved_prompts_dialog._store_saved_prompts)


def test_blindpilot_app_reexports_the_same_objects():
    assert app.SavedPromptsDialog is saved_prompts_dialog.SavedPromptsDialog
    assert app._saved_prompts is saved_prompts_dialog._saved_prompts
    assert app._store_saved_prompts is saved_prompts_dialog._store_saved_prompts


def test_prompts_round_trip_through_config(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(saved_prompts_dialog, "_load_config", lambda: dict(store))
    monkeypatch.setattr(saved_prompts_dialog, "_save_config", lambda cfg: store.update(cfg) or True)
    assert saved_prompts_dialog._saved_prompts() == []
    assert saved_prompts_dialog._store_saved_prompts([{"name": "A", "text": "do it"}])
    assert saved_prompts_dialog._saved_prompts() == [{"name": "A", "text": "do it"}]
