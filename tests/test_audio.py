"""Tests for actual audio analysis without mock synthesis."""
import struct
import wave

import pytest

from omnidaw_agent.audio import verify_wav
from omnidaw_agent.client import OmniError


def write(path, samples, rate=48000, width=2, channels=2):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        fmt = f"<{len(samples)}h" if width == 2 else f"<{len(samples)}i"
        wav.writeframes(struct.pack(fmt, *samples))


def test_audio_verification(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    valid = root / "valid.wav"
    write(valid, [0, 10000] * 4800)
    meta = verify_wav(valid, root, expected_seconds=0.1, sample_rate=48000)
    assert meta["non_silent"] and meta["frames"] == 4800
    silent = root / "silent.wav"
    write(silent, [0, 0] * 4800)
    with pytest.raises(OmniError, match="silent"):
        verify_wav(silent, root)
    escape = tmp_path / "escape.wav"
    write(escape, [0, 1000] * 4800)
    with pytest.raises(OmniError, match="restricted"):
        verify_wav(escape, root)
