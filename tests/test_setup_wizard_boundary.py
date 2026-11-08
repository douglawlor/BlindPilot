"""SetupWizard lives in setup_wizard, not the GUI monolith.

blindpilot_app re-exports it under the same name, so existing call
sites and tests keep working. Names the wizard calls are patched
on setup_wizard, where the calls are made from.

The contract is unchanged by the move: the wizard opens on its
first page with one labelled choice per backend plus Chat mode,
and _chord names a key combination the way it is read aloud.
"""

import wx

import blindpilot_app as app
import setup_wizard


def test_setup_wizard_owns_the_class_and_helpers():
    assert issubclass(setup_wizard.SetupWizard, wx.Dialog)
    assert setup_wizard.PAD_DIALOG == 12
    assert callable(setup_wizard._chord)
    assert callable(setup_wizard._terminal_running)


def test_blindpilot_app_reexports_the_same_object():
    assert app.SetupWizard is setup_wizard.SetupWizard


def test_chord_spells_the_platform_modifier(monkeypatch):
    monkeypatch.setattr(setup_wizard.platform, "system", lambda: "Darwin")
    assert setup_wizard._chord("Ctrl+S") == "Cmd+S"
    monkeypatch.setattr(setup_wizard.platform, "system", lambda: "Windows")
    assert setup_wizard._chord("Ctrl+S") == "Ctrl+S"
