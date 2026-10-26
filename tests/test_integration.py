"""Fake OMNi over real loopback TCP; actual official SDK stdio subprocess tests."""
import asyncio
import json
import sys

import pytest
from mcp import Client as MCPClient
from mcp.client.stdio import StdioServerParameters

from omnidaw_agent.client import Client, OmniError, compatible
from omnidaw_agent.engine import Engine
from omnidaw_agent.workflows import arrange_timeline, export_preview


def method(name, effect="read"):
    return {"name": name, "domain": name.split(".")[0], "title": name, "summary": name,
            "effect": effect, "idempotent": effect == "read", "params": {"type": "object"}}


@pytest.fixture
async def host(tmp_path):
    calls = []
    state = {"mode": "normal", "methods": [method("session.info"), method("tracks.create", "edit"),
        method("tracks.delete", "destructive"), method("clips.write_midi", "edit"),
        method("jobs.wait"), method("arrangement.build_song", "edit")]}
    async def handle(reader, writer):
        try:
            while line := await reader.readline():
                req = json.loads(line)
                name = req["method"]
                calls.append(name)
                if name == "api.hello":
                    result = {"protocol": "omnidaw-api", "api_version": state.get("version", "0.21.0"), "capabilities": state.get("capabilities", ["jobs"])}
                elif name == "api.methods":
                    result = {"protocol": "omnidaw-api", "api_version": "0.21.0", "methods": state["methods"]}
                elif state["mode"] == "drop":
                    break
                elif state["mode"] == "timeout":
                    await asyncio.sleep(.2)
                    break
                elif state["mode"] == "malformed":
                    writer.write(b'secret-token invalid json\n')
                    await writer.drain()
                    continue
                elif name == "jobs.wait":
                    result = {"state": state.get("job", "done"), "progress": .5, "result": {"ok": True}, "error": "failed"}
                elif name == "arrangement.get":
                    result = state.get("arrangement_get", {"playback": "session"})
                else:
                    result = {"ok": True, "id": "uuid-1"}
                reply = {"jsonrpc": "2.0", "id": req["id"] + (1 if state["mode"] == "wrong_id" and name != "api.hello" else 0), "result": result}
                writer.write(json.dumps(reply).encode()+b'\n')
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(json.dumps({"protocol": "omnidaw-api", "host": "127.0.0.1", "port": server.sockets[0].getsockname()[1], "token": "secret-token"}))
    async with server:
        yield endpoint, calls, state


@pytest.mark.parametrize("mode", ["drop", "timeout", "malformed", "wrong_id"])
async def test_transport_no_replay(host, mode):
    path, calls, state = host
    client = Client(path, timeout=.05)
    await client.connect()
    state["mode"] = mode
    with pytest.raises(OmniError) as error:
        await client.call("tracks.create")
    assert "secret-token" not in str(error.value)
    assert calls.count("tracks.create") == 1
    state["mode"] = "normal"
    state["methods"].append(method("new.method"))
    await client.connect()
    assert client.generation == 2
    assert "new.method" in [m["name"] for m in client.catalog["methods"]]
    await client.close()


async def test_version(host):
    path, _, state = host
    state["version"] = "1.0.0"
    client = Client(path)
    with pytest.raises(OmniError, match="Incompatible"):
        await client.connect()
    assert client.writer is None


@pytest.mark.parametrize("version,expected", [("0.21.0", True), ("0.22.1", True), ("0.20.9", False), ("1.0.0", False), ("bad", False)])
def test_compatibility(version, expected):
    assert compatible(version) is expected


async def test_loopback_restriction(host):
    path, calls, _ = host
    ep = json.loads(path.read_text())
    ep["host"] = "192.0.2.1"
    path.write_text(json.dumps(ep))
    with pytest.raises(OmniError):
        await Client(path).connect()
    assert not calls


async def test_policy_domains_and_preview(host):
    path, calls, _ = host
    client = Client(path)
    engine = Engine(client)
    with pytest.raises(OmniError, match="denies"):
        await engine.call("tracks.delete", {})
    with pytest.raises(OmniError, match="denies"):
        await engine.call("clips.write_midi", {})
    engine.allow_write = True
    with pytest.raises(OmniError, match="risk|destructive"):
        await engine.call("clips.write_midi", {})
    engine.domains = {"session"}
    with pytest.raises(OmniError, match="filtered"):
        await engine.preview([{"method": "tracks.create"}])
    assert "tracks.delete" not in calls
    await client.close()


async def test_partial_failure_and_consumed_plan(host):
    path, _, _state = host
    client = Client(path)
    engine = Engine(client, allow_write=True)
    preview = await engine.preview([{"method": "tracks.create", "as": "track"},
                                   {"method": "tracks.create", "params": {"id": {"$ref": "missing.id"}}}])
    result = await engine.apply(preview["plan_id"])
    assert result["state"] == "partial_failure"
    assert result["journal"][0]["state"] == "done"
    with pytest.raises(OmniError, match="consumed"):
        await engine.apply(preview["plan_id"])
    await client.close()


@pytest.mark.parametrize("job", ["done", "failed", "running"])
async def test_jobs(host, job):
    path, _, state = host
    state["job"] = job
    client = Client(path)
    engine = Engine(client)
    if job == "done":
        assert (await engine.wait(1, .1))["state"] == "done"
    else:
        with pytest.raises(OmniError, match="failed|deadline"):
            await engine.wait(1, .1)
    await client.close()


async def test_actual_sdk_subprocess(host):
    path, _, _ = host
    params = StdioServerParameters(command=sys.executable, args=["-m", "omnidaw_agent.server", "--endpoint", str(path), "--domains", "session"])
    async with MCPClient(params) as client:
        tools = (await client.list_tools()).tools
        names = [t.name for t in tools]
        assert "session_info" in names and "tracks_create" not in names
        assert not (await client.call_tool("session_info", {})).is_error
        assert (await client.call_tool("unknown", {})).is_error
        assert (await client.call_tool("agent_preview", {"steps": [{"method": "tracks.create"}]})).is_error


async def test_offline_sdk(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "omnidaw_agent.server", "--endpoint", str(tmp_path/"absent")])
    async with MCPClient(params) as client:
        # 102 native tools from real bundled catalog + 11 agent_* composite workflows = 113 tools
        assert len((await client.list_tools()).tools) == 113
        result = await client.call_tool("agent_status", {})
        assert "NOT verified" in result.content[0].text


async def test_allow_overwrite_policy(host, tmp_path):
    path, calls, state = host
    state["methods"].extend([
        method("session.save", "control"),
        method("export.render", "control"),
        method("export.stems", "control"),
    ])
    output_root = tmp_path / "out"
    output_root.mkdir()
    target_omni = output_root / "song.omni"
    target_omni.write_text("existing omni")

    client = Client(path)
    try:
        # Default policy: an existing target is refused, and so is overwrite: true
        # (allow_risk is granted so the file policy, not the risk gate, is under test).
        engine_no_ow = Engine(client, allow_write=True, allow_risk=True, output_root=output_root)
        with pytest.raises(OmniError, match="never overwritten"):
            await engine_no_ow.call("session.save", {"path": str(target_omni)})
        with pytest.raises(OmniError, match="never overwritten"):
            await engine_no_ow.call("session.save", {"path": str(target_omni), "overwrite": True})
        # Without allow_risk, overwrite: true is also gated as a risky replacement
        engine_no_risk = Engine(client, allow_write=True, output_root=output_root)
        with pytest.raises(OmniError, match="allow-risk"):
            await engine_no_risk.call("session.save", {"path": str(target_omni), "overwrite": True})

        # --allow-overwrite: overwrite: true is accepted without --allow-risk
        engine_ow = Engine(client, allow_write=True, output_root=output_root, allow_overwrite=True)
        res = await engine_ow.call("session.save", {"path": str(target_omni), "overwrite": True})
        assert res.get("ok") is True

        target_wav = output_root / "render.wav"
        target_wav.write_text("existing wav")
        res_wav = await engine_ow.call("export.render", {"path": str(target_wav), "overwrite": True})
        assert res_wav.get("ok") is True

        # Existing file without overwrite: true is still refused
        with pytest.raises(OmniError, match="never overwritten"):
            await engine_ow.call("export.render", {"path": str(target_wav)})

        # A directory target is never overwritable
        with pytest.raises(OmniError, match="never overwritten"):
            await engine_ow.call("session.save", {"path": str(output_root), "overwrite": True})

        # Targets outside output_root are denied even with allow_overwrite
        escape_file = tmp_path / "escape.omni"
        escape_file.write_text("escape")
        with pytest.raises(OmniError, match="escapes"):
            await engine_ow.call("session.save", {"path": str(escape_file), "overwrite": True})

        # Stems folders are not covered by the flag
        stems_folder = output_root / "stems"
        stems_folder.mkdir()
        engine_stems = Engine(client, allow_write=True, allow_risk=True, output_root=output_root, allow_overwrite=True)
        with pytest.raises(OmniError, match="not supported"):
            await engine_stems.call("export.stems", {"folder": str(stems_folder), "overwrite": True})
    finally:
        await client.close()


async def test_file_capability_status_and_diagnostics(host):
    path, calls, state = host
    state["methods"].extend([
        method("audio.import", "edit"),
        method("audio.transcribe", "read"),
        method("audio.split_stems", "control"),
    ])
    # Host capabilities default to ["jobs"], missing "files"
    client = Client(path)
    engine = Engine(client, allow_write=True, allow_risk=True)
    await engine.refresh()
    assert engine.file_permissions == "disabled"

    # Calling audio.import reports exact instructions
    with pytest.raises(OmniError) as err_import:
        await engine.call("audio.import", {"track": 0, "slot": 0, "path": "/test.wav"})
    assert 'Let clients read audio files' in str(err_import.value)
    assert 'OMNI_API_FILES=1' in str(err_import.value)

    # Calling audio.transcribe reports exact instructions
    with pytest.raises(OmniError) as err_transcribe:
        await engine.call("audio.transcribe", {"track": 0, "slot": 0, "path": "/test.wav"})
    assert 'Let clients read audio files' in str(err_transcribe.value)
    assert 'OMNI_API_FILES=1' in str(err_transcribe.value)

    # Calling audio.split_stems reports exact instructions
    with pytest.raises(OmniError) as err_stems:
        await engine.call("audio.split_stems", {"folder": "/tmp/stems", "path": "/test.wav"})
    assert 'Let clients read audio files' in str(err_stems.value)
    assert 'OMNI_API_FILES=1' in str(err_stems.value)

    # Grant "files" capability
    state["capabilities"] = ["jobs", "files"]
    await client.close()
    client2 = Client(path)
    engine2 = Engine(client2, allow_write=True, allow_risk=True)
    await engine2.refresh()
    assert engine2.file_permissions == "enabled"
    # audio.transcribe now succeeds without capability error
    res = await engine2.call("audio.transcribe", {"track": 0, "slot": 0, "path": "/test.wav"})
    assert res.get("ok") is True

    await client2.close()


async def test_export_preview_silence_prevention(host, tmp_path):
    path, calls, state = host
    state["methods"].extend([
        method("transport.stop", "control"),
        method("transport.seek", "control"),
        method("arrangement.play", "control"),
        method("arrangement.get", "read"),
        method("export.render", "control"),
    ])
    state["capabilities"] = ["jobs", "render"]
    out_dir = tmp_path / "export_out"
    out_dir.mkdir()
    wav_target = out_dir / "render.wav"

    client = Client(path)
    engine = Engine(client, allow_write=True, output_root=out_dir)

    # 1. When host is not in arrangement mode (e.g. session mode)
    state["arrangement_get"] = {"playback": "session"}
    preview_session = await export_preview(engine, path=str(wav_target), start_beat=0, end_beat=16)
    step_methods = [s["method"] for s in preview_session["changes"]]
    assert step_methods == [
        "transport.stop",
        "transport.seek",
        "arrangement.play",
        "transport.stop",
        "export.render",
    ]
    assert preview_session["changes"][1]["params"] == {"beat": 0}
    assert preview_session["changes"][2]["params"] == {"from_beat": 0}

    # 2. When host is already in arrangement mode
    state["arrangement_get"] = {"playback": "arrangement"}
    preview_arr = await export_preview(engine, path=str(wav_target), start_beat=0, end_beat=16)
    step_methods_arr = [s["method"] for s in preview_arr["changes"]]
    assert step_methods_arr == ["export.render"]

    await client.close()


async def test_arrange_timeline_workflow(host):
    path, calls, state = host
    state["methods"].extend([
        method("arrangement.place_clip", "edit"),
        method("markers.add", "edit"),
    ])
    client = Client(path)
    engine = Engine(client, allow_write=True)

    placements = [
        {"track": 0, "slot": 0, "start_beat": 0, "length_beats": 16},
        {"track": "uuid-drums", "slot": 1, "start_beat": 16, "length_beats": 32},
    ]
    markers = [
        {"beat": 0, "name": "Intro"},
        {"beat": 16, "name": "Drop", "color": "#ff0000"},
    ]
    preview = await arrange_timeline(engine, placements=placements, markers=markers)
    steps = preview["changes"]
    assert len(steps) == 4
    assert steps[0]["method"] == "arrangement.place_clip"
    assert steps[0]["params"] == {"track": 0, "slot": 0, "start_beat": 0, "length_beats": 16}
    assert steps[1]["method"] == "arrangement.place_clip"
    assert steps[1]["params"] == {"track": "uuid-drums", "slot": 1, "start_beat": 16, "length_beats": 32}
    assert steps[2]["method"] == "markers.add"
    assert steps[2]["params"] == {"beat": 0, "name": "Intro"}
    assert steps[3]["method"] == "markers.add"
    assert steps[3]["params"] == {"beat": 16, "name": "Drop", "color": "#ff0000"}

    await client.close()


async def test_subprocess_improvements(host, tmp_path):
    path, _, state = host
    state["methods"].extend([
        method("arrangement.place_clip", "edit"),
        method("markers.add", "edit"),
    ])
    out_root = tmp_path / "sdk_out"
    out_root.mkdir()
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "omnidaw_agent.server",
            "--endpoint", str(path),
            "--allow-write",
            "--allow-overwrite",
            "--output-root", str(out_root),
        ],
    )
    async with MCPClient(params) as client:
        status_res = await client.call_tool("agent_status", {})
        status_text = status_res.content[0].text
        status_data = json.loads(status_text)
        assert status_data["policy"]["allow_overwrite"] is True
        assert status_data["file_permissions"] == "disabled"

        # Call agent_arrange_timeline via MCP
        res = await client.call_tool("agent_arrange_timeline", {
            "placements": [{"track": 0, "slot": 0, "start_beat": 0, "length_beats": 8}],
            "markers": [{"beat": 0, "name": "Start"}],
        })
        assert not res.is_error
        timeline_plan = json.loads(res.content[0].text)
        assert len(timeline_plan["changes"]) == 2
