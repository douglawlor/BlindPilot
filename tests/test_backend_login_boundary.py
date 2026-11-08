"""BackendLogin lives in backend_login, not the GUI monolith.

blindpilot_app re-exports BackendLogin, _first_login_url and
_login_speech, so SetupWizard and the tests keep working. The
contract is unchanged by the move: only a real sign-in page is
handed back -- never a loopback callback, and never with the
sentence's full stop stuck to it -- and terminal noise is cleaned
before anything is read aloud.
"""

import blindpilot_app as app
import backend_login


def test_backend_login_owns_the_class_and_helpers():
    assert callable(backend_login.BackendLogin)
    assert callable(backend_login._first_login_url)
    assert callable(backend_login._login_speech)


def test_blindpilot_app_reexports_the_same_objects():
    assert app.BackendLogin is backend_login.BackendLogin
    assert app._first_login_url is backend_login._first_login_url
    assert app._login_speech is backend_login._login_speech


def test_login_url_and_speech_behave_as_before():
    assert (
        backend_login._first_login_url("Starting local login server on http://localhost:1455.")
        == ""
    )
    assert (
        backend_login._first_login_url("Open this URL: https://freebuff.com/login?token=abc123.")
        == "https://freebuff.com/login?token=abc123"
    )
    assert backend_login._login_speech("\x1b[2GOpening browser\x1b[0m") == "Opening browser"
