"""Fake OMNi over real loopback TCP; actual official SDK stdio subprocess tests."""
import asyncio
import json
import sys

import pytest
from mcp import Client as MCPClient
from mcp.client.stdio import StdioServerParameters

from omnidaw_agent.client import Client, OmniError, compatible
from omnidaw_agent.engine import Engine


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
                    result = {"protocol": "omnidaw-api", "api_version": state.get("version", "0.21.0"), "capabilities": ["jobs"]}
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
        # 102 native tools from real bundled catalog + 10 agent_* composite workflows = 112 tools
        assert len((await client.list_tools()).tools) == 112
        result = await client.call_tool("agent_status", {})
        assert "NOT verified" in result.content[0].text
