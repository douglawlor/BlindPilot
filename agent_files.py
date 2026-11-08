"""Where each backend keeps its files about you, on disk.

Instructions, memories, skills, agents, commands and settings for
every backend, plus the editing helpers the agent-files window uses.
Extracted from ``blindpilot_app.py`` so the on-disk layout can change
without touching the GUI; blindpilot_app re-exports the same names.
Per issue #136, no real screen-reader verification is represented
as completed for this extraction.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent_backends import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_COMMANDCODE,
    BACKEND_COPILOT,
    BACKEND_GEMINI,
    BACKEND_HERMES,
    BACKEND_LABELS,
    BACKEND_OPENCODE,
    settings_files,
)
from session_history import claude_project_slug


CLAUDE_FILE_KINDS = ("Instructions", "Memories", "Skills", "Agents", "Commands", "Settings")


def _front_matter(path: Path) -> dict[str, str]:
    """The one-line `key: value` fields of a Markdown file's YAML header."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(16384)
    except OSError:
        return {}
    match = re.match(r"---\r?\n(.*?)\r?\n---", head, re.S)
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines() if match else ():
        key, sep, value = line.partition(":")
        value = value.strip()
        try:
            value = json.loads(value) if value[:1] == '"' else value.strip("'")
        except ValueError:
            value = value.strip('"')
        if sep and key[:1].strip() and value not in ("", ">", "|", ">-", "|-"):
            fields[key.strip()] = value
    return fields


def claude_files(cwd: Optional[str]) -> dict[str, list[tuple[str, str]]]:
    """Every file Claude Code keeps about you, as {kind: [(path, label)]}.

    Your own instructions, memories, skills, agents, commands and settings
    under the config folder, plus the ones the project in `cwd` adds, each
    kind sorted by label so a letter jumps to the names starting with it.
    Labels are a file's front-matter name and description where it has them.
    """
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    found: dict[str, list[tuple[str, str]]] = {kind: [] for kind in CLAUDE_FILE_KINDS}
    seen: set[str] = set()
    memory_dirs = {path.parent for path in home.glob("projects/*/memory/*.md")}

    def add(kind: str, paths) -> None:
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            fields = _front_matter(path) if path.suffix == ".md" else {}
            name = fields.get("name") or (
                path.parent.name if path.name == "SKILL.md" else path.name
            )
            label = f"{name}: {fields['description']}" if "description" in fields else name
            if kind != "Memories":
                label += f", in {path.parent}"
            elif len(memory_dirs) > 1:
                label += f", {path.parent.parent.name}"
            found[kind].append((str(path), label))

    claude_dirs = [home]
    settings_dirs = [home]
    if cwd:
        here = Path(cwd)
        settings_dirs.append(here / ".claude")
        # Claude Code reads instructions, skills and agents from the working
        # folder and every one above it; settings only from the folder itself.
        walking = True  # .claude folders: up to the repository root, never home
        for folder in (here, *here.parents):
            add(
                "Instructions",
                [folder / name for name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")],
            )
            walking = walking and folder != Path.home()
            if walking:
                claude_dirs.append(folder / ".claude")
                walking = not (folder / ".git").exists()
        add("Settings", [here / ".mcp.json"])
    for folder in claude_dirs:
        add("Instructions", [folder / "CLAUDE.md", *folder.glob("rules/**/*.md")])
        add("Skills", folder.glob("skills/*/SKILL.md"))
        add("Agents", folder.glob("agents/**/*.md"))
        add("Commands", folder.glob("commands/**/*.md"))
    for folder in settings_dirs:
        add("Settings", [folder / "settings.json", folder / "settings.local.json"])
    add("Memories", home.glob("projects/*/memory/*.md"))
    for files in found.values():
        files.sort(key=lambda entry: entry[1].casefold())
    return found


def memory_folders(cwd: Optional[str]) -> list[Path]:
    """Where a new memory can go: every memory folder Claude Code has, and
    the active tab's own (first, made on save) when it has none yet."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    folders = sorted({path.parent for path in home.glob("projects/*/memory/*.md")})
    if cwd:
        own = home / "projects" / claude_project_slug(cwd) / "memory"
        if own in folders:
            folders.remove(own)
        folders.insert(0, own)
    return folders


def _replace_agent_file(path: Path, text: str) -> None:
    """Keep the original agent file if writing its replacement fails."""
    path = path.resolve()
    temporary = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(text)
        if path.exists():
            shutil.copymode(path, temporary_path)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def add_memory(folder: Path, name: str, description: str, kind: str, body: str) -> Path:
    """Write a memory as Claude Code does and list it in MEMORY.md.

    The file name is the name made safe; an existing one is never replaced.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "memory"
    path = folder / f"{slug}.md"
    if path.exists():
        raise FileExistsError(f"A memory called {slug} already exists")
    description = " ".join(description.split())
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {slug}\ndescription: {json.dumps(description)}\n"
        f"metadata:\n  type: {kind}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )
    index = folder / "MEMORY.md"
    old = index.read_text(encoding="utf-8") if index.exists() else "# Memory index\n"
    _replace_agent_file(index, f"{old.rstrip()}\n- [{slug}]({slug}.md) — {description}\n")
    return path


def delete_memory(path: Path) -> None:
    """Remove a memory and the MEMORY.md lines that point to it."""
    path.unlink()
    index = path.parent / "MEMORY.md"
    if path.name == "MEMORY.md" or not index.exists():
        return
    lines = index.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [
        line
        for line in lines
        if f"({path.name})" not in line and not line.startswith(f"- {path.stem}:")
    ]
    if kept != lines:
        _replace_agent_file(index, "".join(kept))


@dataclass
class AgentFiles:
    """One kind of file one backend keeps, for What Agents Know.

    `files` are (key, label). A key is a path, or for tastes and Hermes
    memories "path\\nindex": one taste line or one Hermes entry of that file.
    `editable` says what Add and Delete do: "memory" (a Claude Code memory
    file), "taste" (a Command Code taste line), "hermes" (a Hermes entry),
    "agent" (a global agent file), or "" for read and edit only. `home` is
    where Add writes.
    """

    backend: str
    kind: str
    files: list[tuple[str, str]]
    editable: str = ""
    home: Optional[Path] = None


def _commandcode_home() -> Path:
    return Path.home() / ".commandcode"


def _hermes_home() -> Path:
    from hermes_backend import hermes_home

    return hermes_home()


def _plain_files(paths, folder: bool = True) -> list[tuple[str, str]]:
    """Existing files once each, labelled by front-matter name and description."""
    out: dict[str, tuple[str, str]] = {}
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key in out or not path.is_file():
            continue
        fields = _front_matter(path) if path.suffix == ".md" else {}
        name = fields.get("name") or (path.parent.name if path.name == "SKILL.md" else path.name)
        label = f"{name}: {fields['description']}" if "description" in fields else name
        out[key] = (str(path), f"{label}, in {path.parent}" if folder else label)
    return sorted(out.values(), key=lambda entry: entry[1].casefold())


def taste_category(path: Path, home: Path) -> str:
    """A taste file's category: its folder under taste/, or "general"."""
    rel = path.parent.relative_to(home / "taste")
    return rel.as_posix() if rel.parts else "general"


def tastes(home: Path) -> list[tuple[str, str]]:
    """Every Command Code taste, one per bullet line of a taste/**/taste.md."""
    out = []
    for path in sorted((home / "taste").glob("**/taste.md")):
        category = taste_category(path, home)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for at, line in enumerate(lines):
            if line.startswith("- "):
                out.append((f"{path}\n{at}", f"{category}: {line[2:].strip()}"))
    return sorted(out, key=lambda entry: entry[1].casefold())


def add_taste(home: Path, category: str, text: str) -> Path:
    """Append one taste line to its category's taste.md, made if new."""
    category = re.sub(r"[^a-z0-9/-]+", "-", category.strip().casefold()).strip("-/")
    folder = home / "taste" / category if category and category != "general" else home / "taste"
    path = folder / "taste.md"
    old = path.read_text(encoding="utf-8").rstrip() + "\n" if path.exists() else ""
    if not old.strip():
        old = f"# {(category or 'general').capitalize()} taste\n\n"
    folder.mkdir(parents=True, exist_ok=True)
    _replace_agent_file(path, f"{old}- {' '.join(text.split())}\n")
    return path


_HERMES_SEPARATOR = re.compile(r"^\s*§\s*$", re.M)


def hermes_entries(path: Path) -> list[str]:
    """The entries of a Hermes memory file, which Hermes separates with §."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return [entry.strip() for entry in _HERMES_SEPARATOR.split(text)]


def hermes_memories(home: Path) -> list[tuple[str, str]]:
    out = []
    for path in sorted((home / "memories").glob("*.md")):
        for at, entry in enumerate(hermes_entries(path)):
            if entry:
                out.append((f"{path}\n{at}", f"{path.stem}: {' '.join(entry.split())}"))
    return out


def add_hermes_memory(path: Path, text: str) -> None:
    entries = [e for e in hermes_entries(path) if e] if path.exists() else []
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_agent_file(path, "\n§\n".join([*entries, text.strip()]))


_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def add_agent(home: Path, backend: str, name: str, description: str, instructions: str) -> Path:
    """Create one agent in its backend's global agents folder without replacing a file."""
    if not _AGENT_NAME.fullmatch(name) or name.casefold() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(
            "Name must use letters, numbers, hyphens or underscores, and not a reserved device name"
        )
    if backend == BACKEND_COMMANDCODE and name.casefold() in {
        "explore",
        "plan",
        "review",
        "general",
    }:
        raise ValueError("This is a reserved Command Code agent name")
    description = " ".join(description.split())
    instructions = instructions.strip()
    if not description or not instructions:
        raise ValueError("An agent needs a description and instructions")
    if backend == BACKEND_CODEX:
        filename = f"{name}.toml"
        content = (
            f"name = {json.dumps(name)}\n"
            f"description = {json.dumps(description, ensure_ascii=False)}\n"
            f"developer_instructions = {json.dumps(instructions, ensure_ascii=False)}\n"
        )
    elif backend in (BACKEND_COMMANDCODE, BACKEND_OPENCODE, BACKEND_COPILOT):
        # Copilot CLI reads custom agents only from *.agent.md files.
        filename = f"{name}.agent.md" if backend == BACKEND_COPILOT else f"{name}.md"
        front = "mode: subagent\n" if backend == BACKEND_OPENCODE else f"name: {name}\n"
        content = f"---\n{front}description: {json.dumps(description, ensure_ascii=False)}\n---\n\n{instructions}\n"
    else:
        raise ValueError("This backend does not support adding agents here")
    path = home / "agents" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as file:
        file.write(content)
    return path


def entry_text(key: str, editable: str) -> tuple[str, str]:
    """The file a key names, and the text to read for it."""
    path, _, at = key.partition("\n")
    if not at:
        return path, Path(path).read_text(encoding="utf-8", errors="replace")
    if editable == "hermes":
        return path, hermes_entries(Path(path))[int(at)]
    line = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[int(at)]
    return path, line[2:].strip()


def delete_entry(key: str, editable: str) -> None:
    """Delete a Claude Code memory, one taste line, one Hermes entry, or an agent file."""
    path, _, at = key.partition("\n")
    if editable == "memory":
        delete_memory(Path(path))
        return
    if editable == "agent":
        Path(path).unlink()
        return
    file = Path(path)
    if editable == "hermes":
        entries = hermes_entries(file)
        del entries[int(at)]
        text = "\n§\n".join(e for e in entries if e)
    else:
        lines = file.read_text(encoding="utf-8").splitlines(keepends=True)
        del lines[int(at)]
        text = "".join(lines)
    _replace_agent_file(file, text)


def save_entry(key: str, editable: str, text: str) -> None:
    """Write back what was edited: a whole file, one taste line, or one Hermes entry."""
    path, _, at = key.partition("\n")
    file = Path(path)
    if not at:
        _replace_agent_file(file, text)
        return
    if not text.strip():
        raise ValueError("it would be empty; Delete removes it instead")
    if editable == "hermes":
        entries = hermes_entries(file)
        entries[int(at)] = text.strip()
        new = "\n§\n".join(e for e in entries if e)
    else:
        lines = file.read_text(encoding="utf-8").splitlines(keepends=True)
        end = "\n" if lines[int(at)].endswith("\n") else ""
        lines[int(at)] = f"- {' '.join(text.split())}{end}"
        new = "".join(lines)
    _replace_agent_file(file, new)


def agent_files(cwd: Optional[str]) -> list[AgentFiles]:
    """What every backend keeps about you on this computer, kind by kind.

    Kinds that are empty are left out, except the ones Add can start: Claude
    Code memories always, Command Code tastes and Hermes memories once that
    backend's folder exists. Settings come from Backend Settings' own list.
    """
    project = Path(cwd) if cwd else None
    here = [project / "AGENTS.md"] if project else []
    claude = claude_files(cwd)
    cc, codex = _commandcode_home(), Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    hermes, gemini = _hermes_home(), Path.home() / ".gemini"
    copilot = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
    opencode = Path.home() / ".config" / "opencode"
    groups = [
        AgentFiles(BACKEND_LABELS[BACKEND_CLAUDE], kind, claude[kind]) for kind in CLAUDE_FILE_KINDS
    ]
    groups[1].editable = "memory"
    groups += [
        AgentFiles(
            BACKEND_LABELS[BACKEND_COMMANDCODE],
            "Tastes",
            tastes(cc) if cc.is_dir() else [],
            "taste",
            cc,
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_COMMANDCODE],
            "Instructions",
            _plain_files([cc / "AGENTS.md", *here]),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_COMMANDCODE],
            "Skills",
            _plain_files(cc.glob("skills/*/SKILL.md")),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_COMMANDCODE],
            "Agents",
            _plain_files(cc.glob("agents/*.md"), False),
            "agent",
            cc,
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_COMMANDCODE],
            "Project Agents",
            _plain_files((project / ".commandcode" / "agents").glob("*.md")) if project else [],
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_HERMES],
            "Memories",
            hermes_memories(hermes) if hermes.is_dir() else [],
            "hermes",
            hermes,
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_HERMES],
            "Skills",
            _plain_files(hermes.glob("skills/**/SKILL.md")),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_CODEX],
            "Instructions",
            _plain_files(
                [codex / "AGENTS.md", codex / "AGENTS.override.md", *codex.glob("rules/*"), *here]
            ),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_CODEX],
            "Memories",
            _plain_files(codex.glob("memories/**/*.md"), False),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_CODEX], "Skills", _plain_files(codex.glob("skills/*/SKILL.md"))
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_CODEX],
            "Agents",
            _plain_files(codex.glob("agents/*.toml"), False),
            "agent",
            codex,
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_GEMINI],
            "Instructions",
            _plain_files([gemini / "GEMINI.md", *([project / "GEMINI.md"] if project else [])]),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_GEMINI], "Skills", _plain_files(gemini.glob("skills/*/SKILL.md"))
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_COPILOT],
            "Instructions",
            _plain_files(
                [
                    copilot / "copilot-instructions.md",
                    *([project / ".github" / "copilot-instructions.md"] if project else []),
                    *here,
                ]
            ),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_COPILOT],
            "Skills",
            _plain_files(copilot.glob("skills/*/SKILL.md")),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_COPILOT],
            "Agents",
            _plain_files(copilot.glob("agents/*.md"), False),
            "agent",
            copilot,
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_OPENCODE],
            "Instructions",
            _plain_files([opencode / "AGENTS.md", *here]),
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_OPENCODE],
            "Agents",
            # Name the live folders explicitly; agent* would also match backups.
            _plain_files([*opencode.glob("agent/*.md"), *opencode.glob("agents/*.md")], False),
            "agent",
            opencode,
        ),
        AgentFiles(
            BACKEND_LABELS[BACKEND_OPENCODE],
            "Project Agents",
            _plain_files(
                [
                    *((project / ".opencode" / "agent").glob("*.md") if project else []),
                    *((project / ".opencode" / "agents").glob("*.md") if project else []),
                ]
            ),
        ),
    ]
    for backend in BACKEND_LABELS:
        if backend == BACKEND_CLAUDE:
            continue  # claude_files has these, with the rules Claude Code reads them by
        found = _plain_files(
            entry.path
            for entry in settings_files(cwd)
            if entry.backend == backend and entry.path.suffix != ".md"
        )
        groups.append(AgentFiles(BACKEND_LABELS[backend], "Settings", found))
    order = list(BACKEND_LABELS.values())
    groups.sort(key=lambda g: order.index(g.backend))  # stable: kinds keep their order
    return [
        g
        for g in groups
        if g.files or g.editable in ("memory", "agent") or (g.home and g.home.is_dir())
    ]
