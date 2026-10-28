"""Real MCP SDK/server subprocess coverage using explicitly FAKE host and analyzer.

The analyzer is a tiny executable Python component, not the real audio-diag DSP.
These tests establish orchestration/identity/schema behavior, not audio quality.
"""
import asyncio
import hashlib
import json
import math
import struct
import sys
import wave

import pytest
from mcp import Client as MCPClient
from mcp.client.stdio import StdioServerParameters
from test_integration import method


@pytest.fixture
async def diagnostic_host(tmp_path):
    calls = []
    methods = [method("arrangement.get"), method("jobs.wait"),
               method("session.save", "control"), method("export.render", "control")]

    async def handle(reader, writer):
        try:
            while line := await reader.readline():
                request = json.loads(line)
                name, params = request["method"], request.get("params", {})
                calls.append((name, params))
                if name == "api.hello":
                    result = {"protocol": "omnidaw-api", "api_version": "0.21.0",
                              "capabilities": ["jobs", "render"]}
                elif name == "api.methods":
                    result = {"protocol": "omnidaw-api", "api_version": "0.21.0", "methods": methods}
                elif name == "arrangement.get":
                    result = {"playback": "arrangement"}
                elif name == "session.save":
                    from pathlib import Path
                    Path(params["path"]).write_text("FAKE checkpoint")
                    result = {"ok": True}
                elif name == "export.render":
                    # Synthetic non-silent PCM, physically written by the fake host.
                    with wave.open(params["path"], "wb") as wav:
                        wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                        wav.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i * 2 * math.pi * 440 / 48000))) * 2
                                                 for i in range(4800)))
                    result = {"job": "fake-render-job"}
                elif name == "jobs.wait":
                    result = {"state": "done", "job": "fake-render-job", "result": {"ok": True}}
                else:
                    raise AssertionError(name)
                writer.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(json.dumps({"protocol": "omnidaw-api", "host": "127.0.0.1",
                                    "port": server.sockets[0].getsockname()[1], "token": "fake-token"}))
    async with server:
        yield endpoint, calls


@pytest.fixture
def fake_analyzer(tmp_path):
    repo = tmp_path / "fake-component"
    package = repo / "audio_diag"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text('"""FAKE component availability marker."""\n')
    (package / "__main__.py").write_text('''
import hashlib
import json
import sys
from pathlib import Path
path = Path(sys.argv[2]).resolve()
report = {"version": "0.1.0", "file": {"path": str(path),
          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
          "fake_component": True, "argv": sys.argv[1:]}
print(json.dumps(report))
''')
    schema = {"type": "object", "required": ["version", "file", "fake_component"],
              "properties": {"version": {"const": "0.1.0"},
                             "file": {"type": "object", "required": ["path", "sha256"]},
                             "fake_component": {"const": True}}}
    (repo / "report.schema.json").write_text(json.dumps(schema))
    return repo


def server_params(endpoint, root, component=None):
    args = ["-m", "omnidaw_agent.server", "--endpoint", str(endpoint),
            "--allow-write", "--allow-risk", "--output-root", str(root)]
    if component:
        args += ["--audio-diag-repo", str(component), "--audio-diag-python", sys.executable]
    return StdioServerParameters(command=sys.executable, args=args)


def payload(response):
    assert not response.is_error, response.content
    return json.loads(response.content[0].text)


async def test_optional_component_absent_and_invalid_sdk_schema(diagnostic_host, tmp_path):
    endpoint, calls = diagnostic_host
    async with MCPClient(server_params(endpoint, tmp_path)) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert "agent_diagnose_wav" not in tools
        status = payload(await client.call_tool("agent_status", {}))
        assert status["optional_integrations"]["audio_diag"]["available"] is False
        args = {"path": str(tmp_path / "mix.wav"), "save_path": str(tmp_path / "checkpoint.omni"),
                "start_beat": 0, "end_beat": 4}
        assert (await client.call_tool("agent_export_and_diagnose_preview", args)).is_error
        assert (await client.call_tool("agent_export_and_diagnose_preview", {**args, "end_beat": "four"})).is_error
    assert not any(name in {"session.save", "export.render"} for name, _ in calls)


@pytest.mark.parametrize("invalid_report", [False, True], ids=["valid-fake-report", "schema-rejected"])
async def test_sdk_checkpoint_render_job_and_diagnose(diagnostic_host, fake_analyzer, tmp_path, invalid_report):
    endpoint, calls = diagnostic_host
    if invalid_report:
        # Deliberately require an absent field: valid JSON is not a valid report.
        (fake_analyzer / "report.schema.json").write_text(json.dumps({"type": "object", "required": ["absent"]}))
    wav, checkpoint = tmp_path / "mix.wav", tmp_path / "checkpoint.omni"
    async with MCPClient(server_params(endpoint, tmp_path, fake_analyzer)) as client:
        assert "agent_diagnose_wav" in {tool.name for tool in (await client.list_tools()).tools}
        args = {"path": str(wav), "save_path": str(checkpoint), "start_beat": 0,
                "end_beat": 4, "bpm": 150, "intents": ["hard techno"]}
        invalid = await client.call_tool("agent_export_and_diagnose_preview", {**args, "bpm": 2})
        assert invalid.is_error
        preview = payload(await client.call_tool("agent_export_and_diagnose_preview", args))
        assert [step["method"] for step in preview["changes"]] == ["session.save", "export.render"]
        assert not checkpoint.exists() and not wav.exists()
        result = payload(await client.call_tool("agent_apply", {"plan_id": preview["plan_id"]}))
        assert result["export"]["state"] == "done"
        assert result["state"] == ("analysis_failed" if invalid_report else "done")
        assert checkpoint.read_text() == "FAKE checkpoint"
        digest = hashlib.sha256(wav.read_bytes()).hexdigest()
        assert result["artifact"]["sha256"] == digest
        assert result["artifact"]["job"] == "fake-render-job"
        if invalid_report:
            assert "ValidationError" in result["error"]
        else:
            report = result["diagnostic"]["report"]
            assert report["file"] == {"path": str(wav), "sha256": digest}
            assert report["argv"] == ["analyze", str(wav), "--bpm", "150", "--intent=hard techno"]
        assert (await client.call_tool("agent_apply", {"plan_id": preview["plan_id"]})).is_error
    operations = [(name, params) for name, params in calls if name in {"session.save", "export.render", "jobs.wait"}]
    assert [name for name, _ in operations] == ["session.save", "export.render", "jobs.wait"]
    assert operations[1][1]["start_beat"] == 0
    assert operations[1][1]["end_beat"] == 4
