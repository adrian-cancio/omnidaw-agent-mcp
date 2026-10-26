"""Disposable integration script.

Runs ONLY on an isolated disposable directory with explicit mock or live endpoints.
Never touches existing user project, never outputs tokens.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from omnidaw_agent.audio import verify_wav
from omnidaw_agent.client import Client, OmniError
from omnidaw_agent.engine import Engine
from omnidaw_agent.workflows import export_preview, loop_preview


async def run_integration(endpoint_path, out_dir):
    out = Path(out_dir).resolve()
    evidence = out / "evidence"
    evidence.mkdir(exist_ok=True)
    report = {"timestamp": time.time(), "evidence_dir": str(evidence), "steps": []}

    client = Client(endpoint_path)
    engine = Engine(client, allow_write=True, allow_risk=True, output_root=out)

    try:
        await engine.refresh()
        report["connection"] = "connected"
        report["capabilities"] = engine.client.capabilities
    except OmniError as e:
        report["connection"] = "failed"
        report["error"] = str(e)
        (evidence / "summary.json").write_text(json.dumps(report, indent=2))
        return report

    # 1. Inspect
    inspect_data = await engine.inspect()
    report["steps"].append({"step": "inspect", "track_count": inspect_data.get("track_count")})

    # 2. Loop preview & apply
    loop = await loop_preview(engine, seed=42, bpm=130)
    loop_res = await engine.apply(loop["plan_id"])
    report["steps"].append({"step": "loop_composed", "state": loop_res["state"]})

    # 3. Readback MIDI
    tracks = await engine.call("tracks.list")
    agent_tracks = [t for t in tracks if t.get("name", "").startswith("Agent ")]
    readback = {}
    for t in agent_tracks:
        notes = await engine.call("clips.get_notes", {"track": t["id"], "slot": 0})
        readback[t["name"]] = len(notes.get("notes", []))
    report["steps"].append({"step": "midi_readback", "counts": readback})

    # 4. Transport playback check
    await engine.call("transport.play")
    await asyncio.sleep(0.5)
    t_state = await engine.call("transport.get")
    await engine.call("transport.stop")
    report["steps"].append({"step": "playback", "playing": t_state.get("playing")})

    # 5. Undo / Redo check on isolated project
    undo1 = await engine.call("edit.undo")
    redo1 = await engine.call("edit.redo")
    report["steps"].append({"step": "history", "undo_label": undo1.get("label"), "redo_label": redo1.get("label")})

    # 6. Save new .omni project
    save_file = out / "disposable_project.omni"
    await engine.call("session.save", {"path": str(save_file)})
    report["steps"].append({"step": "save", "exists": save_file.exists()})

    # 7. Render WAV & verification
    wav_target = out / "render.wav"
    export_p = await export_preview(engine, path=str(wav_target), start_beat=0, end_beat=16)
    export_res = await engine.apply(export_p["plan_id"])
    report["steps"].append({"step": "export", "state": export_res["state"]})

    if wav_target.exists():
        wav_meta = verify_wav(wav_target, out, channels=2)
        report["steps"].append({"step": "verify_wav", "meta": wav_meta})

    (evidence / "summary.json").write_text(json.dumps(report, indent=2))
    await client.close()
    return report


def main():
    if len(sys.argv) < 3:
        print("Usage: run_integration.py <endpoint.json> <output_dir>")
        sys.exit(1)
    res = asyncio.run(run_integration(sys.argv[1], sys.argv[2]))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
