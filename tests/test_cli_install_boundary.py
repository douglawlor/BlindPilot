"""The installer is patched where it is called.

The installer calls its own helpers from inside cli_install. A patch aimed at
blindpilot_app rebinds the name in that module and the installer carries on
with the real one, which for install_backend means running the real
installer: the test does not fail, it installs something. That happened
while this module was being moved, and a test hung on a real install
instead of failing.

A name the installer calls among its own, and that nothing outside the
installer calls, is therefore never patched on blindpilot_app. The names
both the installer and the wizard call are the exception: the wizard lives
in blindpilot_app and reaches them there.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import sys

import pytest

import blindpilot_app
import cli_install

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _module_names(path: pathlib.Path) -> set[str]:
    """The top-level names a module defines."""
    tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _called_by_itself(path: pathlib.Path) -> set[str]:
    """The names a module calls among its own."""
    tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
    defined = _module_names(path)
    internal = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in defined and sub.id != node.name:
                internal.add(sub.id)
    return internal


def _used_in(path: pathlib.Path) -> set[str]:
    """Every name a module refers to."""
    tree = ast.parse(path.read_text(encoding="utf-8"), path.as_posix())
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _load_test_module(name: str):
    """Load a test module under a name pytest will not collect."""
    path = ROOT / "tests" / f"{name}.py"
    alias = f"_probe_{name}"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[alias]
        return None
    return module


def test_the_installer_is_never_patched_on_the_old_module():
    """A name only the installer calls cannot be reached from there."""
    installer = ROOT / "cli_install.py"
    app = ROOT / "blindpilot_app.py"

    internal = _called_by_itself(installer)
    # The wizard and the window call some of these too, from blindpilot_app,
    # and a patch there is the right one for those.
    called_outside = _used_in(app)
    exclusive = internal - called_outside

    offenders = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'setattr\(\s*(?:blindpilot_app|app)\s*,\s*"([^"]+)"', text):
            name = match.group(1)
            if name in exclusive:
                offenders.append(f"{path.name}: {name}")

    assert not offenders, (
        "these are called only by the installer, so a patch aimed at "
        "blindpilot_app would leave the real one running: " + ", ".join(offenders)
    )


def test_the_swap_helpers_choose_the_installer_module():
    """The helpers that swap a name for the length of a with-block have to
    resolve the installer's names to cli_install. A helper that chose the
    old module would swap a name nobody reads."""
    checked = 0
    for name in ("test_cli_install", "test_hermes_install"):
        module = _load_test_module(name)
        if module is None:
            continue
        helper = getattr(module, "_Patch", None)
        if helper is None:
            continue
        assert helper._module("_run_logged_process") is cli_install, name
        assert helper._module("_find_npm") is cli_install, name
        checked += 1
    assert checked, "no swap helper was found to check"


def test_a_patch_on_the_installer_module_is_what_the_installer_sees(monkeypatch):
    """The reason every patch site names cli_install, proved end to end.

    The installer calls these helpers from inside cli_install. A patch on
    blindpilot_app rebinds the name there and the installer carries on with
    the real one, which for install_backend is a real npm install.
    """
    ran: list[list[str]] = []

    def fail(name):
        def boom(*args, **kwargs):
            pytest.fail(f"{name} was read from blindpilot_app, not cli_install")

        return boom

    for name, value in (
        ("_find_npm", lambda: "npm"),
        ("_npm_install_argv", lambda backend: ["npm", "install", backend]),
        ("_npm_environment", lambda npm: {}),
        ("_run_logged_process", lambda argv, log, env=None: ran.append(list(argv)) or 0),
        ("_add_to_process_path", lambda path: None),
        ("_managed_backend_binary", lambda backend: None),
        ("_drop_codex_server", lambda log: None),
        ("find_backend_cli", lambda backend: "C:/managed/codex.cmd"),
        ("ensure_on_path", lambda path: None),
    ):
        monkeypatch.setattr(cli_install, name, value)
        # The same name on the old module must not be the one that runs.
        monkeypatch.setattr(blindpilot_app, name, fail(name), raising=False)

    cli_install.install_backend("codex", lambda line: None)

    # The install ran through the patched helper, and not through the copy
    # of it on the old module.
    assert ran[0] == ["npm", "install", "codex"]


def test_the_version_the_downloads_announce_is_the_one_the_packaging_reads():
    """The User-Agent carries the version, and the spec reads that version
    out of blindpilot_app.py by pattern, so the two have to agree."""
    app_source = (ROOT / "blindpilot_app.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"$', app_source, re.MULTILINE)
    assert match is not None, "the packaging spec would not find a version"
    assert cli_install._user_agent() == f"BlindPilot/{match.group(1)}"
