"""Bounded, strict local SMF/RMID note import; no host tempo changes."""

from collections import defaultdict, deque
from pathlib import Path

from .client import OmniError

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_NOTES = 4096
MAX_TRACKS = 256


def _fail(message):
    raise OmniError("Invalid or unsupported MIDI: " + message)


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            _fail("truncated structure")
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def number(self, count):
        return int.from_bytes(self.take(count), "big")

    def vlq(self):
        value = 0
        for _ in range(4):
            byte = self.number(1)
            value = (value << 7) | (byte & 127)
            if not byte & 128:
                return value
        _fail("VLQ exceeds four bytes")

    def remaining(self):
        return len(self.data) - self.pos


def _unwrap(data):
    if not data.startswith(b"RIFF"):
        return data, "SMF"
    reader = _Reader(data)
    reader.take(4)
    size = int.from_bytes(reader.take(4), "little")
    if size != reader.remaining() or reader.take(4) != b"RMID":
        _fail("invalid RIFF/RMID size or type")
    midi = None
    while reader.remaining():
        kind = reader.take(4)
        count = int.from_bytes(reader.take(4), "little")
        payload = reader.take(count)
        if count & 1:
            reader.take(1)
        if kind == b"data":
            if midi is not None:
                _fail("duplicate RMID data chunk")
            midi = payload
    if midi is None:
        _fail("missing RMID data chunk")
    return midi, "RMID"


def parse_midi(data):
    """Parse bounded bytes into exact tick records and quarter-note beat notes.

    Formats 0/1 only. Track-local channel/pitch pairs use FIFO note pairing.
    Controller/program/tempo/meta events are reported, not applied. Pressure,
    pitch bend, SysEx and system events cannot be represented and are rejected.
    """
    if len(data) > MAX_FILE_BYTES:
        _fail("file size limit exceeded")
    data, container = _unwrap(data)
    reader = _Reader(data)
    if reader.take(4) != b"MThd" or reader.number(4) != 6:
        _fail("expected six-byte MThd")
    fmt, tracks, ppq = reader.number(2), reader.number(2), reader.number(2)
    if fmt not in (0, 1):
        _fail("only SMF formats 0 and 1 are supported")
    if not 1 <= tracks <= MAX_TRACKS or (fmt == 0 and tracks != 1):
        _fail("invalid track count")
    if not ppq or ppq & 0x8000:
        _fail("SMPTE or zero division is unsupported; PPQ required")
    notes, metadata = [], []
    events = end_tick = 0
    for track in range(tracks):
        if reader.take(4) != b"MTrk":
            _fail("expected MTrk")
        stream = _Reader(reader.take(reader.number(4)))
        tick, running, ended = 0, None, False
        active = defaultdict(deque)
        while stream.remaining():
            events += 1
            if events > MAX_EVENTS:
                _fail("event limit exceeded")
            tick += stream.vlq()
            if tick / ppq > 1024:
                _fail("clip exceeds 1024 beats")
            status = stream.number(1)
            if status < 128:
                if running is None:
                    _fail("running status without channel status")
                stream.pos -= 1
                status = running
            if status == 0xFF:
                running = None
                kind = stream.number(1)
                payload = stream.take(stream.vlq())
                if kind >= 128:
                    _fail("invalid meta type")
                sizes = {0x00: 2, 0x20: 1, 0x21: 1, 0x2F: 0,
                         0x51: 3, 0x54: 5, 0x58: 4, 0x59: 2}
                if kind in sizes and len(payload) != sizes[kind]:
                    _fail("invalid meta event length")
                if kind == 0x51 and int.from_bytes(payload, "big") == 0:
                    _fail("zero tempo")
                metadata.append({"track": track, "tick": tick, "type": "meta",
                                 "meta_type": kind, "data_hex": payload.hex()})
                if kind == 0x2F:
                    if stream.remaining():
                        _fail("events after end of track")
                    ended = True
                    break
                continue
            if not 0x80 <= status <= 0xEF:
                _fail("SysEx/system events are unsupported")
            running = status
            kind, channel = status >> 4, status & 15
            count = 1 if kind in (0xC, 0xD) else 2
            values = list(stream.take(count))
            if any(value >= 128 for value in values):
                _fail("invalid channel data byte")
            if kind in (0xA, 0xD, 0xE):
                _fail("pressure/pitch bend would be lost")
            if kind in (0xB, 0xC):
                metadata.append({"track": track, "channel": channel, "tick": tick,
                                 "type": "controller" if kind == 0xB else "program",
                                 "data": values})
                continue
            pitch, velocity = values
            key = channel, pitch
            if kind == 0x9 and velocity:
                active[key].append((tick, velocity))
            else:
                if not active[key]:
                    _fail("unmatched note off")
                start, attack = active[key].popleft()
                duration = tick - start
                if duration / ppq < 0.0001:
                    _fail("zero or unrepresentable note duration")
                notes.append({"track": track, "channel": channel, "pitch": pitch,
                              "start_tick": start, "end_tick": tick, "velocity": attack})
                if len(notes) > MAX_NOTES:
                    _fail("note limit exceeded")
        if not ended or any(active.values()):
            _fail("missing end of track or unterminated notes")
        end_tick = max(end_tick, tick)
    if reader.remaining():
        _fail("trailing chunks or bytes")
    if not notes:
        _fail("no notes")
    notes.sort(key=lambda note: (note["start_tick"], note["track"], note["channel"], note["pitch"]))
    warnings = ["Source tempo and metadata are reported only; project tempo is unchanged."]
    if any(item["type"] in {"controller", "program"} for item in metadata):
        warnings.append("Controller/program events are not applied; sustain and instrument behavior may differ.")
    if len({(note["track"], note["channel"]) for note in notes}) > 1:
        warnings.append("Source tracks/channels are merged into one destination clip.")
    return {"container": container, "format": fmt, "track_count": tracks, "ppq": ppq,
            "event_count": events, "end_tick": end_tick, "source_notes": notes,
            "notes": [{"pitch": n["pitch"], "velocity": n["velocity"],
                       "start": n["start_tick"] / ppq,
                       "duration": (n["end_tick"] - n["start_tick"]) / ppq} for n in notes],
            "length_beats": max(0.25, end_tick / ppq),
            "metadata": metadata, "warnings": warnings}


def read_midi(path, root):
    """Resolve and authorize input before any file read; cap the read itself."""
    try:
        if not Path(path).is_absolute():
            raise OmniError("MIDI input requires an absolute local path")
        target = Path(path).resolve()
        if root is None or not target.is_relative_to(Path(root).resolve()):
            raise OmniError("MIDI input restricted to operator output root")
        if not target.is_file():
            raise OmniError("MIDI input must be a regular file")
        with target.open("rb") as source:
            data = source.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError, RuntimeError) as error:
        raise OmniError(f"Cannot read MIDI input: {error}") from None
    return {"path": str(target), **parse_midi(data)}


async def midi_preview(engine, path, track, slot):
    """Fully validate local input before asking the engine for a replace preview."""
    if not ((type(track) is int and track >= 0) or (isinstance(track, str) and track)):
        raise OmniError("track must be a nonnegative index or nonempty id")
    if type(slot) is not int or not 0 <= slot <= 255:
        raise OmniError("slot must be an integer in 0..255")
    report = read_midi(path, engine.output_root)
    preview = await engine.preview([{"method": "clips.write_midi", "params": {
        "track": track, "slot": slot, "mode": "replace", "notes": report["notes"],
        "length_beats": report["length_beats"]}}], "Local MIDI notes; replaces destination clip; tempo unchanged")
    return {**preview, "midi": report}
