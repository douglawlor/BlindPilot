"""Unreadable history is distinct from empty history, including fallbacks."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_backend
import muse_backend
import session_history as history
from hermes_backend import wsl_sqlite_query


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_home", lambda: tmp_path)
    monkeypatch.delenv("OPENCODE_DATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(hermes_backend, "wsl_sqlite_query", lambda *_a: [])
    monkeypatch.setattr(muse_backend, "muse_index_db", lambda: None)
    monkeypatch.setattr(muse_backend, "wsl_muse_sqlite_query", lambda *_a: [])
    history._JSONL_HEAD_CACHE.clear()
    return tmp_path


def _claude(home, name="healthy", text="A healthy conversation"):
    path = home / ".claude" / "projects" / "project" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
    return path


def _opencode(home):
    path = home / ".local" / "share" / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_absent_and_empty_databases_are_not_failures(home):
    with history.history_diagnostics() as issues:
        assert history.list_history("opencode") == []
    assert issues == []
    with closing(sqlite3.connect(_opencode(home))) as db:
        db.execute(
            "CREATE TABLE session (id, title, directory, time_updated, parent_id, time_archived)"
        )
    with history.history_diagnostics() as issues:
        assert history.list_history("opencode") == []
    assert issues == []


@pytest.mark.parametrize("reason", ["corrupt", "unsupported", "locked"])
def test_database_failures_keep_other_backends_and_do_not_leak_data(home, reason):
    _claude(home)
    path = _opencode(home)
    lock = None
    if reason == "corrupt":
        path.write_text("PRIVATE database contents")
    else:
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE unrelated (private_data)")
        if reason == "locked":
            lock = sqlite3.connect(path)
            lock.execute("BEGIN EXCLUSIVE")
    try:
        with history.history_diagnostics() as issues:
            entries = history.list_history()
        assert any(entry.session_id == "healthy" for entry in entries)
        affected = [issue for issue in issues if issue.backend == "opencode"]
        assert len(affected) == 1
        assert affected[0].reason == reason
        message = affected[0].message
        assert "Reload" in message
        assert str(home) not in message
        assert "PRIVATE" not in message
        assert "unrelated" not in message
    finally:
        if lock:
            lock.close()


def test_failed_codex_database_keeps_jsonl_listing_and_replay(home):
    root = home / ".codex"
    root.mkdir()
    (root / history._CODEX_STATE_DB).write_text("damaged")
    (root / history._CODEX_HISTORY_DB).write_text("damaged")
    path = root / "sessions" / "2026" / "rollout.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "fallback", "cwd": "project"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "Fallback prompt"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    with history.history_diagnostics() as issues:
        entries = history.list_history("codex")
    assert [entry.session_id for entry in entries] == ["fallback"]
    assert any(issue.reason == "corrupt" for issue in issues)
    with history.history_diagnostics() as issues:
        turns = history.load_turns(entries[0])
    assert turns[0].prompt == "Fallback prompt"
    assert any(issue.reason == "corrupt" for issue in issues)


def test_partial_jsonl_is_retained_and_diagnostics_survive_repeated_scans(home):
    path = _claude(home)
    path.write_text("{broken private data\n" + path.read_text())
    for _ in range(2):
        with history.history_diagnostics() as issues:
            entries = history.list_history("claude")
        assert entries[0].session_id == "healthy"
        assert any(issue.reason == "corrupt" for issue in issues)
    with history.history_diagnostics() as issues:
        turns = history.load_turns(entries[0])
    assert turns[0].prompt == "A healthy conversation"
    assert any(issue.reason == "corrupt" for issue in issues)


def test_unreadable_file_is_retried_without_dropping_healthy_files(home, monkeypatch):
    denied = _claude(home, "denied")
    _claude(home)
    original = Path.open

    def open_file(path, *args, **kwargs):
        if path == denied:
            raise PermissionError(f"PRIVATE {path}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with history.history_diagnostics() as issues:
        entries = history.list_history("claude")
    assert [entry.session_id for entry in entries] == ["healthy"]
    assert any(issue.reason == "unreadable" for issue in issues)
    monkeypatch.setattr(Path, "open", original)
    with history.history_diagnostics() as issues:
        entries = history.list_history("claude")
    assert len(entries) == 2
    assert issues == []


def test_folder_enumeration_failure_is_not_empty_history(home, monkeypatch):
    root = home / ".codex" / "sessions"
    original = Path.iterdir

    def iterdir(path):
        if path == root:
            raise PermissionError("PRIVATE folder")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with history.history_diagnostics() as issues:
        assert history.list_history("codex") == []
    assert any(issue.reason == "unreadable" for issue in issues)


def test_concurrent_scans_do_not_exchange_diagnostics(home, monkeypatch):
    def failure(_cwd):
        raise PermissionError("PRIVATE")

    monkeypatch.setitem(history._LISTERS, "opencode", failure)
    monkeypatch.setitem(history._LISTERS, "claude", lambda _cwd: [])

    def scan(backend):
        with history.history_diagnostics() as issues:
            history.list_history(backend)
        return issues

    with ThreadPoolExecutor(max_workers=2) as pool:
        failed, healthy = list(pool.map(scan, ["opencode", "claude"]))
    assert len(failed) == 1
    assert healthy == []


def test_unsupported_freebuff_store_keeps_healthy_project_history(home):
    projects = home / ".config" / "freebuff-desktop" / "projects"
    bad = projects / "bad" / "desktop-v2.db"
    good = projects / "good" / "desktop-v2.db"
    for path in (bad, good):
        path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(bad)) as db:
        db.execute("CREATE TABLE threads (old_column)")
        db.execute("INSERT INTO threads VALUES ('private')")
        db.commit()
    with closing(sqlite3.connect(good)) as db:
        db.execute("CREATE TABLE threads (id, title, project_path, updated_at)")
        db.execute("INSERT INTO threads VALUES ('healthy', 'Good thread', 'project', 1)")
        db.commit()
    with history.history_diagnostics() as issues:
        entries = history.list_history("freebuff")
    assert [entry.session_id for entry in entries] == ["healthy"]
    assert any(issue.reason == "unsupported" for issue in issues)


def test_a_bad_codex_timestamp_does_not_discard_the_conversation(home):
    path = home / ".codex" / history._CODEX_STATE_DB
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE threads (id, title, first_user_message, cwd, updated_at, "
            "rollout_path, source, archived)"
        )
        db.execute(
            "INSERT INTO threads VALUES ('healthy', 'Good', '', '', 'invalid', '', 'cli', 0)"
        )
        db.commit()
    with history.history_diagnostics() as issues:
        entries = history.list_history("codex")
    assert entries[0].session_id == "healthy"
    assert entries[0].modified == 0
    assert any(issue.reason == "corrupt" for issue in issues)


def test_wsl_query_failure_is_reported_without_stderr(home, monkeypatch):
    # Exercise the actual helper; the home fixture normally disables WSL reads.
    monkeypatch.setattr(hermes_backend, "wsl_sqlite_query", wsl_sqlite_query)
    monkeypatch.setattr(hermes_backend, "wsl_exe", lambda: "wsl")
    monkeypatch.setattr(hermes_backend, "wsl_state_db", lambda: "/private/state.db")
    monkeypatch.setattr(
        hermes_backend.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stderr="PRIVATE path: database is locked", stdout=""
        ),
    )
    with history.history_diagnostics() as issues:
        assert history.list_history("hermes") == []
    assert issues[0].reason == "locked"
    assert "PRIVATE" not in issues[0].message
    assert "/private" not in issues[0].message
