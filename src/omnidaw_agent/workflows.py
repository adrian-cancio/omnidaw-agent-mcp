"""Small workflow builders using official methods, UUID references and native generators."""

import math

from .client import OmniError


def step(method, params=None, alias=None, wait=False):
    result = {"method": method, "params": params or {}}
    if alias:
        result["as"] = alias
    if wait:
        result["wait"] = True
    return result


def ref(path):
    return {"$ref": path}


async def loop_preview(engine, seed=1, bpm=140):
    instruments = await engine.call("instruments.list")
    def choose(fragment):
        found = [i["uri"] for i in instruments if fragment in i.get("uri", "")]
        if not found:
            raise OmniError(f"Unsupported: live instrument catalog lacks {fragment}")
        return found[0]
    synth, piano, drums = choose("omni-synth"), choose("piano-synth"), choose("tomic")
    key = {"root": "F", "mode": "minor"}
    progression = {"key": key, "chords": "i VI III VII"}
    steps = [step("transport.set_tempo", {"bpm": bpm})]
    for role, instrument in [("chords", piano), ("bass", synth), ("melody", synth), ("drums", drums)]:
        steps.append(step("tracks.create", {"kind": "midi", "name": "Agent " + role,
                                             "instrument": instrument}, role))
    for role, perform in [("chords", {"pattern": "pad"}), ("bass", {"chords": False, "bass": True})]:
        steps.append(step("clips.write_progression", {"track": ref(role+".id"), "slot": 0,
                                                       "progression": {**progression, "perform": perform}}))
    steps.append(step("clips.generate_melody", {"track": ref("melody.id"), "slot": 0,
        "key": key, "style": "synth_arp", "bars": 4, "seed": seed, "register": "high",
        "chords_from": {"track": ref("chords.id"), "slot": 0}}))
    steps.append(step("clips.write_drums", {"track": ref("drums.id"), "slot": 0,
        "lanes": [{"channel": "kick", "steps": "x...x...x...x..."*4},
                  {"channel": "snare", "steps": "....X.......X..."*4},
                  {"channel": "hihat", "steps": "x.x.x.x.x.x.x.x."*4, "velocity": 70}]}))
    for role in ("chords", "bass", "melody", "drums"):
        steps.append(step("tracks.update", {"track": ref(role+".id"), "volume_db": -12}))
        steps.append(step("arrangement.place_clip", {"track": ref(role+".id"), "slot": 0,
                                                      "start_beat": 0, "length_beats": 16}))
    for role in ("chords", "bass", "melody"):
        steps.append(step("clips.get_notes", {"track": ref(role+".id"), "slot": 0}, role+"_notes"))
    return await engine.preview(steps, "Four-bar electronic loop; adds four new tracks, no scene-wide launch")


async def arrange_preview(engine, sections, transforms=None, automation=None, save_path=None):
    steps = []
    for transform in transforms or []:
        steps.append(step("clips.duplicate", {k: v for k, v in transform.items() if k != "transform"}))
        if transform.get("transform"):
            steps.append(step("clips.transform", {"track": transform.get("to_track", transform["track"]),
                "slot": transform["to_slot"], **transform["transform"]}))
    steps.append(step("arrangement.build_song", {"sections": sections, "clear_existing": False}))
    for lane in automation or []:
        steps.append(step("automation.write", lane))
    if save_path:
        steps.append(step("session.save", {"path": save_path}))
    return await engine.preview(steps, "Sections, clip variations, automation and optional new save")


async def measure(engine, target="spotify", seconds=15, source="master"):
    await engine.refresh()
    if not {"analysis", "jobs"}.issubset(engine.client.capabilities):
        raise OmniError("Unsupported: host lacks analysis/jobs capabilities")
    targets = await engine.call("master.targets")
    started = await engine.call("analysis.capture", {"source": source, "seconds": seconds})
    measured = await engine.wait(started["job"], deadline=90)
    result = {"measurement": measured, "targets": targets, "perceptual_judgment": False,
              "observation": "Numerical analysis only; inspect clipping, spectral balance, stereo and masking before edits."}
    if source == "master":
        result["verdict"] = await engine.call("master.check", {"job": started["job"], "target": target})
        result["proposal"] = await engine.preview([
            step("master.build_chain", {"job": started["job"], "target": target}),
            step("analysis.capture", {"source": "master", "seconds": seconds}, "after", True),
            step("master.check", {"job": ref("after.job"), "target": target})],
            "Measured mastering proposal; refuses an existing limiter; remeasure after apply")
    return result


async def export_preview(engine, path=None, folder=None, start_beat=0, end_beat=None):
    # Validate direct Python callers too, before inspection or plan creation. Never
    # guess song length from unknown live fields or silently default to four bars.
    for name, value in (("start_beat", start_beat), ("end_beat", end_beat)):
        if (type(value) not in (int, float) or not 0 <= value <= 1000000
                or not math.isfinite(value)):
            raise OmniError(f"Explicit finite {name} in 0..1000000 is required")
    if end_beat <= start_beat:
        raise OmniError("end_beat must be greater than start_beat")
    await engine.refresh()
    if not {"render", "jobs"}.issubset(engine.client.capabilities):
        raise OmniError("Unsupported: host lacks render/jobs capabilities")
    if bool(path) == bool(folder):
        raise OmniError("Provide exactly one path (mix) or folder (stems)")
    params = {"start_beat": start_beat, "end_beat": end_beat, "bit_depth": 24}
    params.update({"path": path, "fast": True} if path else {"folder": folder})
    engine.check_files("export.render" if path else "export.stems", params)

    steps = []
    # Preserve the existing timeline preparation sequence. arrangement.play also
    # starts playback; stop afterwards. This cannot guarantee audible content:
    # session clips must be placed on the timeline and output verified separately.
    if "arrangement.get" not in engine.methods():
        raise OmniError("Cannot inspect arrangement: arrangement.get is unavailable or domain-filtered")
    try:
        arr = await engine.call("arrangement.get")
    except OmniError as error:
        raise OmniError(f"Cannot inspect arrangement; no export plan created: {error}",
                        kind=error.kind, host_state=error.host_state) from None
    if not isinstance(arr, dict) or arr.get("playback") not in {"arrangement", "session"}:
        raise OmniError("Cannot inspect arrangement: unknown playback state; no export plan created")
    is_arrangement = arr["playback"] == "arrangement"

    if not is_arrangement:
        steps.extend([
            step("transport.stop"),
            step("transport.seek", {"beat": 0}),
            step("arrangement.play", {"from_beat": 0}),
            step("transport.stop"),
        ])

    steps.append(step("export.render" if path else "export.stems", params, "export", True))
    return await engine.preview(steps,
                                "Render WAV/stems; wait for job; verify files separately with agent_verify_wav")


async def arrange_timeline(engine, placements, markers=None, checkpoint_paths=None):
    steps = []
    for placement in placements:
        for key in ("start_beat", "length_beats"):
            if key in placement:
                value = placement[key]
                if (type(value) not in (int, float) or not math.isfinite(value)
                        or value < 0 or (key == "length_beats" and value <= 0)):
                    raise OmniError(f"{key} must be finite and within positive timeline bounds")
        params = {k: v for k, v in placement.items() if k in {"track", "slot", "start_beat", "length_beats"}}
        steps.append(step("arrangement.place_clip", params))
    for marker in markers or []:
        for key in ("beat", "end_beat"):
            if key in marker and (type(marker[key]) not in (int, float)
                                  or not math.isfinite(marker[key]) or marker[key] < 0):
                raise OmniError(f"Marker {key} must be finite and nonnegative")
        params = {k: v for k, v in marker.items() if k in {"beat", "name", "color", "end_beat"}}
        steps.append(step("markers.add", params))
    if not steps:
        raise OmniError("At least one placement or marker is required")
    batches = [steps[i:i + 32] for i in range(0, len(steps), 32)]
    if checkpoint_paths is not None:
        if len(checkpoint_paths) != len(batches) - 1:
            raise OmniError("Provide exactly one checkpoint path between batches")
        from pathlib import Path

        if len({Path(path).resolve() for path in checkpoint_paths}) != len(checkpoint_paths):
            raise OmniError("Checkpoint paths must be distinct")
        combined = []
        for index, batch in enumerate(batches):
            combined.extend(batch)
            if index < len(batches) - 1:
                if Path(checkpoint_paths[index]).suffix != ".omni":
                    raise OmniError("Checkpoint destination must have .omni extension")
                params = {"path": checkpoint_paths[index]}
                engine.check_files("session.save", params)
                combined.append(step("session.save", params))
        steps = combined
    elif len(batches) > 1:
        raise OmniError("Multiple batches require explicit checkpoint_paths between batches")
    result = await engine.preview(steps, "Timeline batches of at most 32 mutations; checkpoints between batches")
    result["batches"] = [len(batch) for batch in batches]
    return result


async def repeat_preview(engine, track, slot, native_length_beats, count, start_beat=0,
                         markers=None, checkpoint_paths=None):
    # Native length is operator-supplied, not guessed from undocumented host fields.
    from fractions import Fraction

    if type(count) is not int or not 1 <= count <= 128:
        raise OmniError("count must be an integer in 1..128")
    for name, value in (("native_length_beats", native_length_beats), ("start_beat", start_beat)):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise OmniError(f"{name} must be finite and nonnegative")
    if native_length_beats <= 0:
        raise OmniError("native_length_beats must be positive")
    start, length = Fraction(str(start_beat)), Fraction(str(native_length_beats))
    placements = [{"track": track, "slot": slot, "start_beat": float(start + i * length),
                   "length_beats": float(length)} for i in range(count)]
    return await arrange_timeline(engine, placements, markers, checkpoint_paths)
