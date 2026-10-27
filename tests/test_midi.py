"""Synthetic SMF fixtures with independently specified tick/beat expectations."""

from unittest.mock import AsyncMock, patch

import pytest

from omnidaw_agent import midi
from omnidaw_agent.client import OmniError


def chunk(kind, payload):
    return kind + len(payload).to_bytes(4, "big") + payload


def smf(*tracks, fmt=0, ppq=480):
    return chunk(b"MThd", fmt.to_bytes(2, "big") + len(tracks).to_bytes(2, "big")
                 + ppq.to_bytes(2, "big")) + b"".join(chunk(b"MTrk", t) for t in tracks)


EOT = b"\x00\xff\x2f\x00"
SIMPLE = b"\x00\x90\x3c\x64\x83\x60\x80\x3c\x00" + EOT


def riff_part(kind, payload):
    return kind + len(payload).to_bytes(4, "little") + payload + (b"\x00" if len(payload) % 2 else b"")


def rmid(body):
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"RMID" + body


def test_exact_ticks_beats_running_zero_and_fifo():
    # Two overlapping C4 notes, releases at ticks 480 and 720, FIFO.
    data = smf(b"\x00\x90\x3c\x64\x81\x70\x3c\x50"
               b"\x81\x70\x3c\x00\x81\x70\x3c\x00" + EOT)
    result = midi.parse_midi(data)
    assert result["notes"] == [
        {"pitch": 60, "velocity": 100, "start": 0, "duration": 1},
        {"pitch": 60, "velocity": 80, "start": 0.5, "duration": 1},
    ]
    assert [(n["start_tick"], n["end_tick"]) for n in result["source_notes"]] == [(0, 480), (240, 720)]
    assert result["length_beats"] == 1.5


def test_tracks_channels_and_metadata():
    conductor = b"\x00\xff\x51\x03\x07\xa1\x20" + EOT
    notes = (b"\x00\xc1\x05\x00\xb1\x40\x7f\x00\x91\x3c\x40"
             b"\x00\x92\x3c\x50\x83\x60\x81\x3c\x00"
             b"\x83\x60\x82\x3c\x00" + EOT)
    result = midi.parse_midi(smf(conductor, notes, SIMPLE, fmt=1))
    assert [(n["track"], n["channel"], n["end_tick"]) for n in result["source_notes"]] == [(1, 1, 480), (1, 2, 960), (2, 0, 480)]
    assert result["metadata"][0]["data_hex"] == "07a120"
    assert {m["type"] for m in result["metadata"]} == {"meta", "program", "controller"}
    assert len(result["warnings"]) == 3


def test_rmid_odd_padding_and_unknown_chunks():
    data = smf(SIMPLE)
    result = midi.parse_midi(rmid(riff_part(b"JUNK", b"x") + riff_part(b"data", data)))
    assert result["container"] == "RMID"
    assert result["notes"][0]["duration"] == 1


@pytest.mark.parametrize("data", [
    b"", smf(SIMPLE, fmt=2), smf(SIMPLE, ppq=0), smf(SIMPLE, ppq=0xE728),
    smf(SIMPLE)[:-1], smf(SIMPLE) + b"x", smf(EOT),
    smf(b"\x00\x3c\x64" + EOT),
    smf(b"\x81\x80\x80\x80\x00\x90\x3c\x64" + EOT),
    smf(b"\x00\x90\x3c\x80" + EOT),
    smf(b"\x00\x80\x3c\x00" + EOT),
    smf(b"\x00\x90\x3c\x64" + EOT),
    smf(b"\x00\x90\x3c\x64\x00\x80\x3c\x00" + EOT),
    smf(SIMPLE + EOT), smf(SIMPLE[:-4]),
    smf(b"\x00\xff\x51\x02\x01\x02" + SIMPLE),
    smf(b"\x00\xff\x51\x03\x00\x00\x00" + SIMPLE),
    smf(b"\x00\x90\x3c\x64\x00\xff\x01\x00\x01\x3c\x00" + EOT),
    rmid(riff_part(b"JUNK", b"x")),
    rmid(riff_part(b"data", smf(SIMPLE)) * 2),
    rmid(riff_part(b"JUNK", b"x"))[:-1],
    rmid(b"data\x01\x00\x00\x00x"),
])
def test_malformed_or_unsupported(data):
    with pytest.raises(OmniError):
        midi.parse_midi(data)


@pytest.mark.parametrize("event", [b"\xf0\x00", b"\xf7\x00", b"\xf8", b"\xa0\x3c\x40", b"\xd0\x40", b"\xe0\x00\x40"])
def test_reject_lossy_events(event):
    with pytest.raises(OmniError):
        midi.parse_midi(smf(b"\x00" + event + SIMPLE))


def test_every_truncation_boundary():
    raw = smf(SIMPLE)
    wrapped = rmid(riff_part(b"JUNK", b"odd") + riff_part(b"data", raw))
    for data in (raw, wrapped):
        for end in range(len(data)):
            with pytest.raises(OmniError):
                midi.parse_midi(data[:end])


def test_cross_channel_or_track_release_rejected():
    for data in (
        smf(b"\x00\x90\x3c\x64\x01\x81\x3c\x00" + EOT),
        smf(b"\x00\x90\x3c\x64" + EOT,
            b"\x01\x80\x3c\x00" + EOT, fmt=1),
    ):
        with pytest.raises(OmniError):
            midi.parse_midi(data)


def test_read_size_limit(tmp_path, monkeypatch):
    path = tmp_path / "large.mid"
    path.write_bytes(smf(SIMPLE))
    monkeypatch.setattr(midi, "MAX_FILE_BYTES", 8)
    with pytest.raises(OmniError, match="size limit"):
        midi.read_midi(path, tmp_path)


def test_limits(monkeypatch):
    data = smf(SIMPLE)
    monkeypatch.setattr(midi, "MAX_FILE_BYTES", len(data) - 1)
    with pytest.raises(OmniError, match="size limit"):
        midi.parse_midi(data)
    monkeypatch.setattr(midi, "MAX_FILE_BYTES", len(data))
    monkeypatch.setattr(midi, "MAX_EVENTS", 2)
    with pytest.raises(OmniError, match="event limit"):
        midi.parse_midi(data)
    monkeypatch.setattr(midi, "MAX_EVENTS", 3)
    monkeypatch.setattr(midi, "MAX_NOTES", 0)
    with pytest.raises(OmniError, match="note limit"):
        midi.parse_midi(data)


def test_input_policy_precedes_open(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.mid"
    outside.write_bytes(smf(SIMPLE))
    link = root / "link.mid"
    link.symlink_to(outside)
    with patch("pathlib.Path.open", side_effect=AssertionError("must not read")):
        for path, policy in [(outside, root), (link, root), (outside, None)]:
            with pytest.raises(OmniError, match="output root"):
                midi.read_midi(path, policy)


async def test_preview_and_rejection_before_preview(tmp_path):
    class Engine:
        output_root = tmp_path
        preview = AsyncMock(return_value={"plan_id": "test"})

    engine = Engine()
    path = tmp_path / "notes.mid"
    path.write_bytes(smf(SIMPLE))
    result = await midi.midi_preview(engine, path, "destination", 2)
    steps, label = engine.preview.call_args.args
    assert result["plan_id"] == "test"
    assert steps == [{"method": "clips.write_midi", "params": {
        "track": "destination", "slot": 2, "mode": "replace", "length_beats": 1,
        "notes": [{"pitch": 60, "velocity": 100, "start": 0, "duration": 1}]}}]
    assert "tempo unchanged" in label
    engine.preview.reset_mock()
    path.write_bytes(b"broken")
    with pytest.raises(OmniError):
        await midi.midi_preview(engine, path, 0, 0)
    engine.preview.assert_not_awaited()


@pytest.mark.parametrize("track,slot", [(True, 0), (-1, 0), ("", 0), (0, True), (0, 256)])
async def test_invalid_destination(track, slot):
    with pytest.raises(OmniError):
        await midi.midi_preview(None, "unused", track, slot)
