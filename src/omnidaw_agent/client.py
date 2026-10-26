# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Tomasz Głuc
# Copyright (c) 2026 omnidaw-agent-mcp contributors
"""Bounded async adaptation of the official JSON-lines client; never replays requests."""

import asyncio
import ipaddress
import json
import os
import sys
import time
from pathlib import Path

from jsonschema import Draft202012Validator

VERSION = "0.21.0"
LIMIT = 8 * 1024 * 1024


class OmniError(RuntimeError):
    pass


def endpoint_file():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "omni/api/endpoint.json"


def compatible(version):
    try:
        parts = tuple(int(p) for p in version.split("."))
        return len(parts) == 3 and parts[0] == 0 and parts >= (0, 21, 0)
    except (ValueError, AttributeError):
        return False


class Client:
    def __init__(self, endpoint=None, timeout=12):
        self.endpoint = Path(endpoint) if endpoint else endpoint_file()
        self.timeout = timeout
        self.reader = self.writer = None
        self.lock = asyncio.Lock()
        self.next_id = 0
        self.token = ""
        self.capabilities = []
        self.catalog = None
        self.generation = 0
        self.last_used = 0

    def sanitize(self, value):
        text = json.dumps(value, ensure_ascii=False)
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        return json.loads(text)

    async def close(self):
        writer, self.writer = self.writer, None
        self.reader = None
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass

    async def _rpc(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        frame = json.dumps({"jsonrpc": "2.0", "id": request_id,
                            "method": method, "params": params}, allow_nan=False).encode() + b"\n"
        if len(frame) > LIMIT:
            raise OmniError("Request exceeds frame limit")
        try:
            async with asyncio.timeout(self.timeout):
                self.writer.write(frame)
                await self.writer.drain()
                line = await self.reader.readline()
            if not line or len(line) > LIMIT or not line.endswith(b"\n"):
                raise ValueError()
            reply = json.loads(line)
            if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                    or type(reply.get("id")) is not int or reply["id"] != request_id
                    or ("result" in reply) == ("error" in reply)):
                raise ValueError()
        except (OSError, TimeoutError, ValueError, UnicodeError):
            await self.close()
            raise OmniError("Transport failed; outcome may be unknown. Inspect state before retrying.") from None
        self.last_used = time.monotonic()
        if "error" in reply:
            error = self.sanitize(reply["error"])
            raise OmniError(f"OMNi error: {json.dumps(error)}") from None
        return self.sanitize(reply["result"])

    async def _connect(self):
        try:
            if self.endpoint.stat().st_size > 16384:
                raise ValueError()
            ep = json.loads(self.endpoint.read_text())
            host, port, token = ep["host"], ep["port"], ep["token"]
            if (not ipaddress.ip_address(host).is_loopback or type(port) is not int
                    or not 0 < port < 65536 or not isinstance(token, str) or not token
                    or ep.get("protocol") != "omnidaw-api"):
                raise ValueError()
            self.token = token
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=LIMIT), self.timeout)
            hello = await self._rpc("api.hello", {"token": token, "api_version": VERSION,
                                    "client": {"name": "omnidaw-agent-mcp", "version": "0.1.0"}})
            if hello.get("protocol") != "omnidaw-api" or not compatible(hello.get("api_version")):
                raise OmniError("Incompatible OMNi protocol/version (requires >=0.21.0, major 0)")
            doc = await self._rpc("api.methods", {})
            if doc.get("protocol") != "omnidaw-api" or not compatible(doc.get("api_version")):
                raise OmniError("Incompatible catalog")
            names = set()
            for method in doc["methods"]:
                name = method["name"]
                if name in names or method["domain"] != name.split(".")[0]:
                    raise ValueError()
                names.add(name)
                Draft202012Validator.check_schema(method["params"])
            self.catalog = doc
            self.capabilities = hello.get("capabilities", [])
            self.generation += 1
        except OmniError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise OmniError("OMNi endpoint unavailable or invalid; enable the local API.") from None

    async def connect(self):
        async with self.lock:
            if self.writer is None:
                await self._connect()
            elif time.monotonic() - self.last_used > 5:
                await self._rpc("api.ping", {})

    async def call(self, method, params=None):
        async with self.lock:
            if self.writer is None:
                await self._connect()
            return await self._rpc(method, params or {})
