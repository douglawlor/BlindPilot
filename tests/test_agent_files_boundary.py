"""The agent-file store lives in agent_files, not the GUI monolith.

blindpilot_app re-exports the same objects, so call sites and tests
that reach them as ``app.agent_files`` keep working.
"""

import blindpilot_app as app
import agent_files

NAMES = (
    "AgentFiles",
    "CLAUDE_FILE_KINDS",
    "add_agent",
    "add_hermes_memory",
    "add_memory",
    "add_taste",
    "agent_files",
    "claude_files",
    "delete_entry",
    "delete_memory",
    "entry_text",
    "hermes_entries",
    "hermes_memories",
    "memory_folders",
    "save_entry",
    "taste_category",
    "tastes",
)


def test_agent_files_owns_the_store():
    for name in NAMES:
        assert callable(getattr(agent_files, name)) or name.isupper(), name
    assert agent_files.agent_files.__module__ == "agent_files"


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(agent_files, name), name


def test_front_matter_and_hermes_entries_behave_as_before(tmp_path):
    note = tmp_path / "note.md"
    note.write_text('---\nname: "demo"\ndescription: A demo\n---\nBody\n', encoding="utf-8")
    assert agent_files._front_matter(note) == {"name": "demo", "description": "A demo"}
    memories = tmp_path / "MEMORY.md"
    memories.write_text("first\n\u00a7\nsecond\n", encoding="utf-8")
    assert agent_files.hermes_entries(memories) == ["first", "second"]
