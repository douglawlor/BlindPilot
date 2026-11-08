"""MainFrame lives in main_frame, not the GUI monolith.

blindpilot_app re-exports the same class, so ``app.MainFrame``
keeps working while the frame itself sits with the shell helpers
it is built from.
"""

import blindpilot_app as app
import main_frame


def test_main_frame_owns_the_frame():
    assert main_frame.MainFrame.__module__ == "main_frame"


def test_blindpilot_app_reexports_the_same_class():
    assert app.MainFrame is main_frame.MainFrame


def test_the_two_version_literals_cannot_drift():
    """BlindPilot.spec reads APP_VERSION out of blindpilot_app.py by
    pattern, so that literal stays there; app_shell carries the same
    value for the modules that import it. They must agree."""
    import app_shell

    assert app.APP_VERSION == app_shell.APP_VERSION
