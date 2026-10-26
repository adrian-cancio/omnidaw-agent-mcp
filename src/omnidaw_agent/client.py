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
    def __init__(self, message, kind="operation_error", host_state="unknown"):
        super().__init__(message)
        self.kind = kind
        self.host_state = host_state


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
        except (OSError, TimeoutError, ValueError, UnicodeError) as error:
            await self.close()
            kind = "transport_timeout" if isinstance(error, TimeoutError) else "transport_failed"
            raise OmniError("Transport failed; outcome may be unknown. Inspect state before retrying.",
                            kind=kind) from None
        self.last_used = time.monotonic()
        if "error" in reply:
            error = self.sanitize(reply["error"])
            raise OmniError(f"OMNi error: {json.dumps(error)}", kind="rpc_error", host_state="responding") from None
        return self.sanitize(reply["result"])

    def _discovery(self):
        # Bound the read itself, including a file that grows after it is opened.
        try:
            with self.endpoint.open("rb") as stream:
                raw = stream.read(16385)
        except FileNotFoundError:
            raise OmniError(
                "Discovery file not found. This does not prove OMNi is absent; host state is unknown. "
                "Check the host's local API discovery or supply --endpoint with an existing discovery file.",
                kind="discovery_missing") from None
        except OSError:
            raise OmniError("Discovery file could not be read; check its path and read permissions.",
                            kind="discovery_unreadable") from None

        def invalid(reason):
            # Never echo raw discovery values: any of them could contain a token.
            return OmniError("Invalid discovery file: " + reason, kind="discovery_invalid")

        if len(raw) > 16384:
            raise invalid("file too large (limit 16384 bytes)")
        try:
            ep = json.loads(raw)
        except (ValueError, UnicodeError):
            raise invalid("malformed JSON") from None
        if not isinstance(ep, dict):
            raise invalid("expected a JSON object")
        if ep.get("protocol") != "omnidaw-api":
            raise invalid("protocol must be omnidaw-api")
        host, port, token = ep.get("host"), ep.get("port"), ep.get("token")
        try:
            if not isinstance(host, str):
                raise ValueError()
            address = ipaddress.ip_address(host)
        except ValueError:
            raise invalid("host must be a literal loopback IP address") from None
        if not address.is_loopback:
            raise invalid("host must be loopback; remote addresses are refused")
        if type(port) is not int or not 0 < port < 65536:
            raise invalid("port must be an integer in 1..65535")
        if not isinstance(token, str) or not token:
            raise invalid("token must be a non-empty string")
        return host, port, token

    async def _connect(self):
        # Do not retain capabilities/catalog from a previous, now disconnected host.
        self.catalog, self.capabilities = None, []
        host, port, token = self._discovery()
        self.token = token
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=LIMIT), self.timeout)
        except ConnectionRefusedError:
            raise OmniError("Connection refused at the discovered loopback endpoint; nothing accepted this connection. "
                            "Discovery may be stale; this does not prove OMNi is absent.",
                            kind="connection_refused", host_state="not_listening") from None
        except TimeoutError:
            raise OmniError("Connection to the discovered loopback endpoint timed out; host state is unknown.",
                            kind="connection_timeout") from None
        except OSError:
            raise OmniError("Connection to the discovered loopback endpoint failed; check local API availability.",
                            kind="connection_failed") from None
        phase = "api.hello"
        try:
            hello = await self._rpc("api.hello", {"token": token, "api_version": VERSION,
                                    "client": {"name": "omnidaw-agent-mcp", "version": "0.2.0"}})
            if not isinstance(hello, dict):
                raise ValueError()
            if hello.get("protocol") != "omnidaw-api" or not compatible(hello.get("api_version")):
                raise OmniError("Incompatible OMNi protocol/version (requires >=0.21.0, major 0)",
                                kind="incompatible", host_state="responding")
            phase = "api.methods"
            doc = await self._rpc("api.methods", {})
            if not isinstance(doc, dict):
                raise ValueError()
            if doc.get("protocol") != "omnidaw-api" or not compatible(doc.get("api_version")):
                raise OmniError("Incompatible catalog", kind="incompatible", host_state="responding")
            names = set()
            for method in doc["methods"]:
                name = method["name"]
                if name in names or method["domain"] != name.split(".")[0]:
                    raise ValueError()
                names.add(name)
                Draft202012Validator.check_schema(method["params"])
            capabilities = hello.get("capabilities", [])
            if not isinstance(capabilities, list) or any(not isinstance(c, str) for c in capabilities):
                raise ValueError()
            self.catalog = doc
            self.capabilities = capabilities
            self.generation += 1
        except OmniError as error:
            await self.close()
            if error.kind == "incompatible":
                raise
            kind = ("handshake_timeout" if error.kind == "transport_timeout" else
                    "handshake_rejected" if error.kind == "rpc_error" else "handshake_failed")
            raise OmniError(f"Handshake {phase} failed: {error}", kind=kind, host_state="listening") from None
        except Exception:
            await self.close()
            raise OmniError(f"Handshake {phase} failed: invalid response or catalog.",
                            kind="handshake_invalid", host_state="responding") from None

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
