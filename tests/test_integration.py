"""Fake OMNi over real loopback TCP; actual official SDK stdio subprocess tests.

Everything here talks to an in-process FAKE host. Passing these tests does not
establish behavior against a real OMNi host (see scripts/run_integration.py).
"""
import asyncio
import json
import socket
import subprocess
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
                if (name == "api.hello" and state.get("hello_error")) or (
                        name == "arrangement.get" and state.get("arrangement_error")):
                    code = -32001 if name == "api.hello" else -32003
                    error = {"code": code, "message": "rejected secret-token"}
                    writer.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": error}).encode()+b'\n')
                    await writer.drain()
                    continue
                if name == "api.hello" and state.get("hello_silent"):
                    await asyncio.sleep(.3)
                    break
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


async def test_phase5_public_tools(host, tmp_path):
    path, calls, state = host
    catalog = Engine(Client(path)).catalog
    state['methods'] = [m for m in catalog['methods'] if m['name'] in {'clips.write_midi', 'arrangement.place_clip', 'session.save', 'markers.add'}]
    params = StdioServerParameters(command=sys.executable, args=['-m', 'omnidaw_agent.server', '--endpoint', str(path), '--allow-write', '--allow-risk', '--output-root', str(tmp_path)])
    # One note: 240 ticks at PPQ 480 => independently expected half beat.
    import struct
    events = b'\x00\x90\x3c\x64\x81\x70\x80\x3c\x00\x00\xff\x2f\x00'
    source = tmp_path / 'synthetic.mid'
    source.write_bytes(b'MThd' + struct.pack('>IHHH', 6, 0, 1, 480) + b'MTrk' + struct.pack('>I', len(events)) + events)
    async with MCPClient(params) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {'agent_midi_preview', 'agent_repeat_preview'} <= tools.keys()
        for name, args in [('agent_midi_preview', {'path': str(source), 'track': 0, 'slot': 0}),
                           ('agent_repeat_preview', {'track': 0, 'slot': 0, 'native_length_beats': 0.5, 'count': 3})]:
            response = await client.call_tool(name, args)
            assert not response.is_error
            result = json.loads(response.content[0].text)
            if name == 'agent_midi_preview':
                assert result['changes'][0]['params']['notes'] == [{'pitch': 60, 'velocity': 100, 'start': 0, 'duration': 0.5}]
            applied = await client.call_tool('agent_apply', {'plan_id': result['plan_id']})
            assert json.loads(applied.content[0].text)['state'] == 'done', applied.content[0].text
        assert (await client.call_tool('agent_repeat_preview', {'count': 2})).is_error
    assert calls.count('clips.write_midi') == 1
    assert calls.count('arrangement.place_clip') == 3


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
        assert len((await client.list_tools()).tools) == 115
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


# ---------------------------------------------------------------------------
# Hardening increment: discovery/connection diagnostics, explicit export range,
# fail-closed arrangement inspection. Fake host unless stated otherwise.
# ---------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def good_endpoint(port=1234, **overrides):
    ep = {"protocol": "omnidaw-api", "host": "127.0.0.1", "port": port, "token": "secret-token"}
    ep.update(overrides)
    return ep


async def connect_error(path, timeout=1):
    client = Client(path, timeout=timeout)
    with pytest.raises(OmniError) as error:
        await client.connect()
    assert client.writer is None
    return error.value


async def test_diag_discovery_missing_is_not_host_absent(tmp_path):
    err = await connect_error(tmp_path / "nope" / "endpoint.json")
    assert err.kind == "discovery_missing"
    assert err.host_state == "unknown"
    text = str(err).lower()
    assert "not found" in text or "absent" in text
    assert "does not prove" in text


@pytest.mark.parametrize("content,reason", [
    ("{not json", "json"),
    ("[1, 2]", "object"),
    (json.dumps({"protocol": "omnidaw-api", "host": "127.0.0.1", "port": 1}), "token"),
    (json.dumps(good_endpoint(host="192.0.2.1")), "loopback"),
    (json.dumps(good_endpoint(host="not-an-ip")), "host"),
    (json.dumps(good_endpoint(port=0)), "port"),
    (json.dumps(good_endpoint(port="1234")), "port"),
    (json.dumps(good_endpoint(token="")), "token"),
    (json.dumps(good_endpoint(protocol="other")), "protocol"),
    pytest.param("x" * 20000, "large", id="oversized"),
])
async def test_diag_discovery_invalid_variants(tmp_path, content, reason):
    path = tmp_path / "endpoint.json"
    path.write_text(content)
    err = await connect_error(path)
    assert err.kind == "discovery_invalid"
    assert reason in str(err).lower()
    assert "secret-token" not in str(err)
    assert err.host_state == "unknown"


async def test_diag_discovery_unreadable(tmp_path):
    path = tmp_path / "endpoint.json"
    path.mkdir()  # exists but cannot be read as a file
    err = await connect_error(path)
    assert err.kind == "discovery_unreadable"


async def test_diag_connection_refused(tmp_path):
    path = tmp_path / "endpoint.json"
    path.write_text(json.dumps(good_endpoint(port=free_port())))
    err = await connect_error(path)
    assert err.kind == "connection_refused"
    assert err.host_state == "not_listening"
    assert "secret-token" not in str(err)


async def test_diag_connection_timeout_mocked(tmp_path, monkeypatch):
    path = tmp_path / "endpoint.json"
    path.write_text(json.dumps(good_endpoint()))

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)
    monkeypatch.setattr(asyncio, "open_connection", hang)
    err = await connect_error(path, timeout=.05)
    assert err.kind == "connection_timeout"


async def test_diag_handshake_timeout(host):
    path, calls, state = host
    state["hello_silent"] = True
    err = await connect_error(path, timeout=.05)
    assert err.kind == "handshake_timeout"
    assert err.host_state == "listening"
    assert "secret-token" not in str(err)


async def test_diag_handshake_rejected_redacts_token(host):
    path, calls, state = host
    state["hello_error"] = True
    err = await connect_error(path)
    assert err.kind == "handshake_rejected"
    assert err.host_state == "listening"
    assert "secret-token" not in str(err)
    assert "-32001" in str(err)


async def test_diag_incompatible_kind(host):
    path, _, state = host
    state["version"] = "1.0.0"
    err = await connect_error(path)
    assert err.kind == "incompatible"


async def test_diag_loopback_failure_makes_no_calls(host):
    path, calls, _ = host
    ep = json.loads(path.read_text())
    ep["host"] = "192.0.2.1"
    path.write_text(json.dumps(ep))
    err = await connect_error(path)
    assert err.kind == "discovery_invalid"
    assert not calls


async def test_diag_status_and_cli_report_classification(tmp_path):
    missing = tmp_path / "absent" / "endpoint.json"
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "omnidaw_agent.server", "--endpoint", str(missing)])
    async with MCPClient(params) as client:
        status = json.loads((await client.call_tool("agent_status", {})).content[0].text)
        assert status["connection_kind"] == "discovery_missing"
        assert status["host_state"] == "unknown"
        assert "NOT verified" in status["catalog_provenance"]
    out = subprocess.run([sys.executable, "-m", "omnidaw_agent.server", "--endpoint", str(missing), "--diagnose"],
                         capture_output=True, text=True, timeout=30, check=True)
    report = json.loads(out.stdout)
    assert report["connected"] is False
    assert report["error_kind"] == "discovery_missing"
    assert report["host_state"] == "unknown"
    assert report["endpoint_file"] == str(missing)
    assert "NOT verified" in report["provenance"]


async def test_diag_cli_connected_report(host):
    path, _, _ = host
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "omnidaw_agent.server", "--endpoint", str(path), "--diagnose",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == 0, stderr.decode()
    report = json.loads(stdout)
    assert report["connected"] is True
    assert b"secret-token" not in stdout


EXPORT_METHODS = [("transport.stop", "control"), ("transport.seek", "control"), ("arrangement.play", "control"),
                  ("arrangement.get", "read"), ("export.render", "control"), ("export.stems", "control")]


@pytest.fixture
async def export_engine(host, tmp_path):
    path, calls, state = host
    state["methods"].extend(method(n, e) for n, e in EXPORT_METHODS)
    state["capabilities"] = ["jobs", "render"]
    out_dir = tmp_path / "export_out"
    out_dir.mkdir()
    stems = out_dir / "stems"
    stems.mkdir()
    client = Client(path)
    engine = Engine(client, allow_write=True, output_root=out_dir)
    yield engine, calls, state, out_dir, stems
    await client.close()


@pytest.mark.parametrize("kind", ["mix", "stems"])
@pytest.mark.parametrize("start,end", [
    (0, None), (0, float("nan")), (0, float("inf")), (0, float("-inf")),
    (0, 0), (8, 8), (16, 4), (0, -1), (0, True), (0, "16"),
    (float("nan"), 16), (-1, 16), (True, 16),
])
async def test_export_preview_refuses_bad_range_before_any_host_call(export_engine, kind, start, end):
    engine, calls, _, out_dir, stems = export_engine
    kwargs = {"path": str(out_dir / "mix.wav")} if kind == "mix" else {"folder": str(stems)}
    with pytest.raises(OmniError, match="end_beat|start_beat"):
        if end is None:
            await export_preview(engine, start_beat=start, **kwargs)
        else:
            await export_preview(engine, start_beat=start, end_beat=end, **kwargs)
    assert calls == []
    assert engine.plans == {}


@pytest.mark.parametrize("kind", ["mix", "stems"])
async def test_export_preview_has_no_implicit_end_beat(export_engine, kind):
    engine, calls, _, out_dir, stems = export_engine
    kwargs = {"path": str(out_dir / "mix.wav")} if kind == "mix" else {"folder": str(stems)}
    with pytest.raises(OmniError, match="end_beat"):
        await export_preview(engine, **kwargs)
    assert engine.plans == {}


@pytest.mark.parametrize("kind", ["mix", "stems"])
async def test_export_preview_explicit_range_forwarded_verbatim(export_engine, kind):
    engine, _, state, out_dir, stems = export_engine
    state["arrangement_get"] = {"playback": "arrangement"}
    kwargs = {"path": str(out_dir / "mix.wav")} if kind == "mix" else {"folder": str(stems)}
    preview = await export_preview(engine, start_beat=4, end_beat=512.5, **kwargs)
    last = preview["changes"][-1]
    assert last["method"] == ("export.render" if kind == "mix" else "export.stems")
    assert last["params"]["start_beat"] == 4 and last["params"]["end_beat"] == 512.5


def test_version_locations_agree():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    declared = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
    assert f'"version": "{declared}"' in (root / "src/omnidaw_agent/client.py").read_text()


def test_export_preview_public_schema_requires_end_beat():
    from omnidaw_agent.server import TOOLS
    spec = TOOLS["agent_export_preview"][1]
    assert "end_beat" in spec["required"]
    for key in ("start_beat", "end_beat"):
        assert spec["properties"][key]["minimum"] == 0
        assert spec["properties"][key]["maximum"] == 1000000


async def test_export_sdk_rejects_missing_end_beat(host, tmp_path):
    path, calls, state = host
    state["methods"].extend(method(n, e) for n, e in EXPORT_METHODS)
    state["capabilities"] = ["jobs", "render"]
    out = tmp_path / "o"
    out.mkdir()
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "omnidaw_agent.server", "--endpoint", str(path), "--allow-write", "--output-root", str(out)])
    async with MCPClient(params) as client:
        res = await client.call_tool("agent_export_preview", {"path": str(out / "m.wav")})
        assert res.is_error
        assert not any(c.startswith("transport.") or c.startswith("export.") for c in calls)


@pytest.mark.parametrize("failure", ["rpc_error", "drop", "timeout", "malformed"])
@pytest.mark.parametrize("kind", ["mix", "stems"])
async def test_export_preview_fails_closed_when_arrangement_inspection_fails(export_engine, kind, failure):
    engine, calls, state, out_dir, stems = export_engine
    engine.client.timeout = .05
    await engine.refresh()
    if failure == "rpc_error":
        state["arrangement_error"] = True
    else:
        state["mode"] = failure
    kwargs = {"path": str(out_dir / "mix.wav")} if kind == "mix" else {"folder": str(stems)}
    with pytest.raises(OmniError, match="arrangement"):
        await export_preview(engine, start_beat=0, end_beat=16, **kwargs)
    assert engine.plans == {}
    assert not any(c.startswith("transport.") or c.startswith("export.") or c == "arrangement.play" for c in calls)


async def test_export_preview_session_mode_still_prepares_arrangement_steps(export_engine):
    engine, _, state, out_dir, _ = export_engine
    state["arrangement_get"] = {"playback": "session"}
    preview = await export_preview(engine, path=str(out_dir / "mix.wav"), start_beat=0, end_beat=16)
    assert [s["method"] for s in preview["changes"]] == [
        "transport.stop", "transport.seek", "arrangement.play", "transport.stop", "export.render"]


@pytest.mark.parametrize("value", [None, [], {}, {"playback": "unknown"}])
async def test_export_unknown_playback_fails_closed(export_engine, value):
    engine, _, state, out_dir, _ = export_engine
    state["arrangement_get"] = value
    with pytest.raises(OmniError, match="unknown playback"):
        await export_preview(engine, path=str(out_dir / "mix.wav"), end_beat=32)
    assert not engine.plans


async def test_export_unavailable_inspection_fails_closed(export_engine):
    engine, _, state, out_dir, _ = export_engine
    state["methods"] = [m for m in state["methods"] if m["name"] != "arrangement.get"]
    with pytest.raises(OmniError, match="unavailable"):
        await export_preview(engine, path=str(out_dir / "mix.wav"), end_beat=32)
    assert not engine.plans


async def test_status_provenance_not_live_after_disconnection(host):
    path, _, _ = host
    client = Client(path)
    engine = Engine(client)
    await engine.refresh()
    assert engine.provenance.startswith("live")
    await client.close()
    missing = path.parent / "missing.json"
    client.endpoint = missing
    with pytest.raises(OmniError):
        await engine.refresh()
    assert "cached" in engine.provenance and "NOT verified" in engine.provenance
    assert client.capabilities == []


async def test_diag_invalid_handshake_catalog(host):
    path, _, state = host
    state["methods"] = [method("session.info"), method("session.info")]
    error = await connect_error(path)
    assert error.kind == "handshake_invalid"
    assert "api.methods" in str(error)
    assert "secret-token" not in str(error)
