"""Where the settings file lives, and where it must not.

`app_config` owns where the settings are and how they are read and written.
The functions there call each other, so a name patched on `app_config` is
what reaches them: `_load_config` reads `_config_path` and
`_legacy_config_path` from that module's namespace, and `_save_config` reads
`_config_path` and `_config_dir` from it.

The reverse also holds, and it is why these tests exist. `_load_config` and
`_save_config` are called from all over the application, and each caller
reaches the name in its own namespace. A test that patches one of them on
`blindpilot_app` reaches the callers, and does not reach the other two,
which is why the path helpers are patched on `app_config` instead.

The old module no longer carries the path helpers at all, so a patch aimed
at the wrong module raises instead of quietly changing nothing.
"""

from __future__ import annotations

import app_config
import blindpilot_app


def test_the_old_module_no_longer_carries_the_path_helpers():
    # These are the names whose calls are made from inside app_config. Left
    # on blindpilot_app, a patch aimed there would look like it worked and
    # change nothing, because the running code reads the name in app_config.
    for name in ("_config_path", "_legacy_config_path", "_config_dir"):
        assert not hasattr(blindpilot_app, name), name


def test_the_remote_hermes_settings_live_with_the_config_file():
    # The credential file is written beside the settings file, and it is a
    # secret. It has to be written where the settings are read from, or a
    # test that redirects the settings redirects only half of it and the key
    # lands in the real folder.
    assert blindpilot_app._RemoteHermes is app_config._RemoteHermes
    assert blindpilot_app.REMOTE_HERMES is app_config.REMOTE_HERMES
    assert app_config._RemoteHermes._key_path().parent == app_config._config_dir()


def test_the_settings_object_lives_with_the_config_file():
    # One settings object, built by the module that owns the file. A second
    # copy in the old module would read whichever `_load_config` it could
    # reach, and the two would disagree about what the file says.
    assert blindpilot_app._Settings is app_config._Settings
    assert blindpilot_app.SETTINGS is app_config.SETTINGS


def test_the_load_and_save_functions_are_shared_by_import():
    # The same function object under two names: a caller in either module
    # reaches the same code, and replacing it in one place reaches both.
    assert blindpilot_app._load_config is app_config._load_config
    assert blindpilot_app._save_config is app_config._save_config


def test_a_config_written_where_the_path_says_is_read_back(tmp_path, monkeypatch):
    # The round trip through the module's own namespace: _save_config resolves
    # _config_path from app_config, so this only works if the path helper is
    # patched where the call is made from.
    path = tmp_path / "config.json"
    monkeypatch.setattr(app_config, "_config_path", lambda: path)
    monkeypatch.setattr(app_config, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(app_config, "_legacy_config_path", lambda: tmp_path / "absent.json")

    assert app_config._save_config({"backend": "codex"}) is True
    assert app_config._load_config() == {"backend": "codex"}


def test_the_preference_names_are_reachable_from_both_places():
    # Constants are immutable, so a copy is as good as the original. The
    # tests and the dialogs read them as blindpilot_app.<name>, and that has
    # to keep working.
    assert blindpilot_app.APPEARANCE_SYSTEM == app_config.APPEARANCE_SYSTEM
    assert blindpilot_app.CUE_SECONDS_DEFAULT == app_config.CUE_SECONDS_DEFAULT
    assert blindpilot_app.NARRATION_MODES == app_config.NARRATION_MODES
