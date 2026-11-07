"""Earcons live in earcons, not the GUI monolith.

blindpilot_app re-exports Earcons and _faster_wav, so existing call
sites and tests -- including the subclass in test_progress_cue --
keep working. The contract is unchanged by the move: a faster copy
of a WAV differs only in its header's sample rate, and cues follow
the live SETTINGS switches.
"""

import wave

import blindpilot_app as app
import earcons


def test_earcons_owns_the_class_and_helper():
    assert callable(earcons.Earcons)
    assert callable(earcons._faster_wav)


def test_blindpilot_app_reexports_the_same_objects():
    assert app.Earcons is earcons.Earcons
    assert app._faster_wav is earcons._faster_wav


def _wav(path, rate=8000):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(1)
        out.setframerate(rate)
        out.writeframes(b"\x80" * 80)
    return str(path)


def test_faster_wav_only_raises_the_sample_rate(tmp_path):
    source = _wav(tmp_path / "cue.wav")
    faster = earcons._faster_wav(source, 1.5)
    with wave.open(faster, "rb") as got:
        assert got.getframerate() == 12000
        assert got.getnframes() == 80
    # Written once and reused.
    assert earcons._faster_wav(source, 1.5) == faster
