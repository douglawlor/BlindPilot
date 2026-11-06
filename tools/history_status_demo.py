"""Run with ``python -m tools.history_status_demo`` for a screen-reader check.

The first scan shows a damaged Codex database and a working JSONL fallback.
Ctrl+Enter reads the example conversation in a tab. Reopening the picker repairs
the example databases on its second scan.
All history and settings used here are temporary; no backend CLI is started.
"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import wx

import blindpilot_app as app
import session_history as history


def main() -> None:
    application = wx.App(False)
    with tempfile.TemporaryDirectory(prefix="blindpilot-history-demo-") as temporary:
        root = Path(temporary)
        codex = root / ".codex"
        rollout = codex / "sessions" / "example.jsonl"
        rollout.parent.mkdir(parents=True)
        records = [
            {"type": "session_meta", "payload": {"id": "example", "cwd": str(root)}},
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": "Example conversation"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "The fallback works.",
                },
            },
        ]
        rollout.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        for name in (history._CODEX_STATE_DB, history._CODEX_HISTORY_DB):
            (codex / name).write_text("Example damaged database", encoding="utf-8")

        scans = 0
        list_history = history.list_history

        def scan(*args, **kwargs):
            nonlocal scans
            scans += 1
            if scans == 2:
                for name, schema in (
                    (
                        history._CODEX_STATE_DB,
                        "CREATE TABLE threads (id, title, first_user_message, cwd, updated_at, "
                        "rollout_path, source, archived)",
                    ),
                    (
                        history._CODEX_HISTORY_DB,
                        "CREATE TABLE thread_items (turn_id, item_type, item_json, thread_id, rollout_ordinal)",
                    ),
                ):
                    path = codex / name
                    path.unlink()
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute(schema)
            return list_history(*args, **kwargs)

        with (
            patch.object(history, "_home", lambda: root),
            patch.object(history, "_LISTERS", {"codex": history._codex_entries}),
            patch.object(app, "list_history", scan),
            patch.object(app, "_load_config", lambda: {"backend": "codex", "app_mode": "agent"}),
            patch.object(app, "_save_config", lambda _config: True),
        ):
            parent = app.MainFrame(str(root))
            parent.SetTitle("History source status demonstration")
            try:
                parent.Show()
                wx.CallAfter(parent._open_history)
                application.MainLoop()
            finally:
                if parent:
                    parent.Destroy()
                application.Yield()


if __name__ == "__main__":
    main()
