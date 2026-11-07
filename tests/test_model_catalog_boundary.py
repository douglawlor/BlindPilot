"""The model catalog probe and cache live in model_dialog.

blindpilot_app re-exports probe_model_options, cached_model_options,
invalidate_model_options and _run_claude, so existing call sites and
tests keep working. Names the probe calls are patched on
model_dialog, where the calls are made from.

The contract is unchanged by the move: a cached result is dropped
when the binary that answered has changed, and invalidate clears
only the named backend's entries.
"""

import blindpilot_app as app
import model_dialog

NAMES = (
    "probe_model_options",
    "cached_model_options",
    "invalidate_model_options",
    "_run_claude",
    "_parse_model_aliases",
    "_parse_current_model",
)


def test_model_dialog_owns_the_catalog():
    for name in NAMES:
        assert callable(getattr(model_dialog, name)), name


def test_blindpilot_app_reexports_the_same_objects():
    for name in NAMES:
        assert getattr(app, name) is getattr(model_dialog, name), name


def test_parsers_behave_as_before():
    assert model_dialog._parse_model_aliases(
        "Usage: /model <name>. Available: sonnet, opus, or a full model ID."
    ) == ["sonnet", "opus"]
    assert model_dialog._parse_current_model("Current model: Opus 5 (effort: high)") == (
        "Opus 5",
        "high",
    )


def test_invalidate_clears_only_the_named_backend():
    model_dialog._probe_cache["claude:/x"] = (0.0, model_dialog.ModelOptions([], []), "b|0|1")
    model_dialog._probe_cache["codex:/x"] = (0.0, model_dialog.ModelOptions([], []), "b|0|1")
    try:
        model_dialog.invalidate_model_options("claude")
        assert "claude:/x" not in model_dialog._probe_cache
        assert "codex:/x" in model_dialog._probe_cache
    finally:
        model_dialog._probe_cache.clear()
