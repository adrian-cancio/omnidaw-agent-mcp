"""Tests for actual audio analysis without mock synthesis."""
import math
import random
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


def tone(seconds, dbfs, rate=48000, freq=440.0):
    amplitude = 32767 * 10**(dbfs/20)
    frames = int(seconds*rate)
    samples = []
    for index in range(frames):
        value = round(amplitude*math.sin(2*math.pi*freq*index/rate))
        samples += [value, value]
    return samples


def dither(seconds, dbfs=-130, rate=48000, seed=7):
    """Uniform noise with the given peak level, in a 32-bit container (needs width=4)."""
    rng = random.Random(seed)
    peak = round(2**31 * 10**(dbfs/20))
    return [round(peak*rng.uniform(-1, 1)) for _ in range(int(seconds*rate)*2)]


def test_dither_only_rejected(tmp_path):
    path = tmp_path / "dither.wav"
    write(path, dither(1.0), width=4)
    with pytest.raises(OmniError, match=r"silent or dither-only.*peak -\d+\.\d dBFS.*RMS -\d+\.\d dBFS"):
        verify_wav(path, tmp_path)


def test_tone_minus_20_passes(tmp_path):
    path = tmp_path / "tone.wav"
    write(path, tone(0.5, -20))
    meta = verify_wav(path, tmp_path)
    assert meta["non_silent"]
    assert meta["peak_dbfs"] == pytest.approx(-20, abs=0.1)
    assert -24 < meta["rms_dbfs"] < -22


def test_all_zero_rejected(tmp_path):
    path = tmp_path / "zero.wav"
    write(path, [0] * 48000 * 2)
    with pytest.raises(OmniError, match="silent"):
        verify_wav(path, tmp_path)


def test_long_quiet_section_passes(tmp_path):
    path = tmp_path / "quiet.wav"
    write(path, tone(3.0, -45))
    meta = verify_wav(path, tmp_path)
    assert meta["non_silent"]
    assert meta["peak_dbfs"] == pytest.approx(-45, abs=0.1)


def test_quiet_section_inside_mostly_silent_file_passes(tmp_path):
    path = tmp_path / "mixed.wav"
    # 32-bit container: tone() output is 16-bit scale, so rescale it.
    quiet = [s << 16 for s in tone(1.0, -45)]
    write(path, [0] * 48000 * 2 + quiet + dither(1.0), width=4)
    assert verify_wav(path, tmp_path)["non_silent"]


def test_silence_floor_is_configurable(tmp_path):
    path = tmp_path / "quiet.wav"
    write(path, tone(0.5, -45))
    with pytest.raises(OmniError, match="-40.0 dBFS silence floor"):
        verify_wav(path, tmp_path, silence_floor_dbfs=-40)
    assert verify_wav(path, tmp_path, silence_floor_dbfs=-60)["non_silent"]
    with pytest.raises(OmniError, match="finite number"):
        verify_wav(path, tmp_path, silence_floor_dbfs="loud")
    for bad in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(OmniError, match="finite number"):
            verify_wav(path, tmp_path, silence_floor_dbfs=bad)


def test_verify_wav_public_schema_exposes_silence_floor():
    from omnidaw_agent.server import TOOLS
    spec = TOOLS["agent_verify_wav"][1]
    assert spec["properties"]["silence_floor_dbfs"] == {"type": "number"}
    assert "silence_floor_dbfs" not in spec["required"]
    assert spec["additionalProperties"] is False
