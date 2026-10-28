"""Validate actual integer PCM WAV, without fabricating or substituting audio."""
import math
import os
import stat
import wave
from contextlib import contextmanager
from pathlib import Path

from .client import OmniError

DEFAULT_SILENCE_FLOOR_DBFS = -80.0


def _dbfs(level):
    return 20*math.log10(level)


@contextmanager
def regular_reader(path):
    """Reject special files before opening; fstat also rejects replacement races.

    O_NONBLOCK prevents a FIFO swapped in after stat from blocking open on POSIX.
    O_NOFOLLOW refuses a final-component symlink swapped in after resolution.
    This is not a filesystem I/O deadline or a sandbox against hostile ancestor
    directory replacement; the operator must control the authorized root.
    """
    target = Path(path)
    if not stat.S_ISREG(target.stat().st_mode):
        raise OmniError("Input must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OmniError("Input must be a regular file")
        with os.fdopen(fd, "rb") as source:
            fd = None
            yield source
    finally:
        if fd is not None:
            os.close(fd)


def verify_wav(path, root, expected_seconds=None, sample_rate=None, channels=2,
               silence_floor_dbfs=DEFAULT_SILENCE_FLOOR_DBFS):
    if (isinstance(silence_floor_dbfs, bool) or not isinstance(silence_floor_dbfs, (int, float))
            or not math.isfinite(silence_floor_dbfs)):
        raise OmniError("silence_floor_dbfs must be a finite number")
    target = Path(path).resolve()
    if root is None or not target.is_relative_to(Path(root).resolve()):
        raise OmniError("Verification restricted to operator output root")
    try:
        with regular_reader(target) as source, wave.open(source, "rb") as wav:
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
            rms = math.sqrt(energy/samples)/scale
            peak_dbfs = _dbfs(peak/scale)
            # rms > 0 whenever peak > 0, so both logarithms are defined here.
            rms_dbfs = _dbfs(rms)
            if peak_dbfs < silence_floor_dbfs:
                raise OmniError(
                    f"Rendered PCM is silent or dither-only: peak {peak_dbfs:.1f} dBFS, "
                    f"RMS {rms_dbfs:.1f} dBFS, below the {silence_floor_dbfs:.1f} dBFS silence floor")
            return {"path": str(target), "frames": frames, "sample_rate": rate, "channels": count,
                    "duration_seconds": duration, "peak": peak/scale, "rms": rms,
                    "peak_dbfs": peak_dbfs, "rms_dbfs": rms_dbfs, "non_silent": True}
    except (OSError, wave.Error, ValueError):
        raise OmniError("Invalid WAV format, duration, sample rate or channel count") from None
