"""The history diagnostics module is a public surface, and it never crashes."""

import history_status
from history_status import (
    HistoryIssue,
    backend_scope,
    current_issues,
    history_diagnostics,
    read_error,
    record_issues,
    report_issue,
    source_is_dir,
    source_is_file,
)


def test_every_public_name_is_importable():
    # These are the module's contract with the readers and the window. A
    # private name in here means somebody reaches past the edge of the API.
    for name in history_status.__all__:
        assert hasattr(history_status, name), name


def test_an_unknown_reason_still_gets_a_sentence():
    # A reason nobody has written advice for must not raise: this text is
    # built on the way to the screen, and an exception here would take the
    # explanation down with it.
    issue = HistoryIssue("codex", "history database", "something new")
    assert "something new" in issue.message
    assert "Reload" in issue.message


def test_every_known_reason_gets_its_own_advice():
    reasons = ("locked", "unreadable", "corrupt", "unsupported", "missing", "too large")
    messages = {HistoryIssue("codex", "history database", reason).message for reason in reasons}
    assert len(messages) == len(reasons)


def test_an_issue_is_recorded_under_the_backend_being_read():
    with history_diagnostics() as issues, backend_scope("codex"):
        report_issue("locked", "history database")
    assert len(issues) == 1
    assert issues[0].backend == "codex"
    assert issues[0].reason == "locked"
    assert issues[0].source == "history database"


def test_the_same_issue_is_not_reported_twice():
    with history_diagnostics() as issues, backend_scope("codex"):
        report_issue("locked", "history database")
        report_issue("locked", "history database")
    assert len(issues) == 1


def test_an_issue_outside_a_backend_is_not_recorded():
    # Without a backend to name, there is nothing to say about it.
    with history_diagnostics() as issues:
        report_issue("locked", "history database")
    assert issues == []


def test_merged_issues_reach_the_caller_without_duplicates():
    with history_diagnostics() as issues:
        record_issues([HistoryIssue("codex", "history database", "locked")])
        record_issues(
            [
                HistoryIssue("codex", "history database", "locked"),
                HistoryIssue("hermes", "history database", "corrupt"),
            ]
        )
    assert len(issues) == 2


def test_current_issues_is_empty_outside_a_scan():
    assert current_issues() == []


def test_a_read_failure_is_classified_not_raised():
    with history_diagnostics() as issues, backend_scope("codex"):
        read_error(OSError("disk gone"), "history database")
    assert issues[0].reason == "unreadable"


def test_an_absent_source_is_not_a_failure(tmp_path):
    missing = tmp_path / "not-here.sqlite"
    with history_diagnostics() as issues, backend_scope("codex"):
        assert source_is_file(missing) is False
        assert source_is_dir(missing) is False
    assert issues == []


def test_a_folder_is_not_a_file(tmp_path):
    with history_diagnostics() as issues, backend_scope("codex"):
        assert source_is_file(tmp_path) is False
        assert source_is_dir(tmp_path) is True
    assert any(issue.reason == "unsupported" for issue in issues)
