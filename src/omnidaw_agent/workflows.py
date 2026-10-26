"""Small workflow builders using official methods, UUID references and native generators."""

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


async def export_preview(engine, path=None, folder=None, start_beat=0, end_beat=16):
    await engine.refresh()
    if not {"render", "jobs"}.issubset(engine.client.capabilities):
        raise OmniError("Unsupported: host lacks render/jobs capabilities")
    if bool(path) == bool(folder):
        raise OmniError("Provide exactly one path (mix) or folder (stems)")
    params = {"start_beat": start_beat, "end_beat": end_beat, "bit_depth": 24}
    params.update({"path": path, "fast": True} if path else {"folder": folder})
    engine.check_files("export.render" if path else "export.stems", params)
    return await engine.preview([step("export.render" if path else "export.stems", params, "export", True)],
                                "Render WAV/stems; wait for job; verify files separately with agent_verify_wav")
