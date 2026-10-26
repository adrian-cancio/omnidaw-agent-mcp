"""Validate actual integer PCM WAV, without fabricating or substituting audio."""
import math
import wave
from pathlib import Path

from .client import OmniError


def verify_wav(path, root, expected_seconds=None, sample_rate=None, channels=2):
    target = Path(path).resolve()
    if root is None or not target.is_relative_to(Path(root).resolve()):
        raise OmniError("Verification restricted to operator output root")
    try:
        with wave.open(str(target), "rb") as wav:
            rate, count, width, frames = (wav.getframerate(), wav.getnchannels(),
                                          wav.getsampwidth(), wav.getnframes())
            if width not in (2, 3, 4) or count != channels or not frames:
                raise ValueError()
            if sample_rate is not None and rate != sample_rate:
                raise ValueError()
            duration = frames/rate
            if expected_seconds is not None and abs(duration-expected_seconds) > .1:
                raise ValueError()
            peak = energy = samples = 0
            while data := wav.readframes(8192):
                for offset in range(0, len(data), width):
                    value = int.from_bytes(data[offset:offset+width], "little", signed=True)
                    peak = max(peak, abs(value))
                    energy += value*value
                    samples += 1
            if peak == 0:
                raise OmniError("Rendered PCM is silent")
            scale = 2**(8*width-1)
            return {"path": str(target), "frames": frames, "sample_rate": rate, "channels": count,
                    "duration_seconds": duration, "peak": peak/scale,
                    "rms": math.sqrt(energy/samples)/scale, "non_silent": True}
    except (OSError, wave.Error, ValueError):
        raise OmniError("Invalid WAV format, duration, sample rate or channel count") from None
