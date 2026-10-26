"""Shared enforcement boundary for raw tools and composable workflows."""

import asyncio
import copy
import json
import secrets
import time
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from .client import OmniError

# These operations replace content even though their catalog effect is only edit.
REPLACES = {
    "clips.write_midi", "clips.write_progression", "clips.write_drums",
    "clips.generate_melody", "clips.write_slices", "clips.duplicate", "midi_fx.render",
    "automation.write", "tempo.set_map", "tempo.set_time_signatures",
    "tracks.set_instrument", "instruments.load_preset", "audio.import", "theory.harmonize",
}


def risky(method, params):
    def flags(value):
        if isinstance(value, dict):
            return any((k in {"overwrite", "replace", "clear_existing", "clear"} and bool(v))
                       or (k == "mode" and v == "replace") or flags(v)
                       for k, v in value.items())
        return isinstance(value, list) and any(flags(v) for v in value)
    return method["effect"] in {"destructive", "history"} or method["name"] in REPLACES or flags(params)


class Engine:
    def __init__(self, client, domains=None, allow_write=False, allow_risk=False, output_root=None):
        self.client = client
        self.domains = set(domains) if domains else None
        self.allow_write, self.allow_risk = allow_write, allow_risk
        self.output_root = Path(output_root).resolve() if output_root else None
        self.catalog = json.loads(files("omnidaw_agent").joinpath("catalog.json").read_text())
        self.provenance = "bundled official reference 0.21.0; NOT verified against running host"
        self.plans = {}
        self.lock = asyncio.Lock()

    async def refresh(self):
        await self.client.connect()
        self.catalog = self.client.catalog
        self.provenance = f"live api.methods; connection generation {self.client.generation}"

    def methods(self):
        return {m["name"]: m for m in self.catalog["methods"]
                if self.domains is None or m["domain"] in self.domains}

    def check(self, name, params, enforce=True):
        method = self.methods().get(name)
        if method is None:
            raise OmniError("Unknown or domain-filtered method: " + name)
        if list(Draft202012Validator(method["params"]).iter_errors(params)):
            raise OmniError("Parameters do not match the official schema for " + name)
        if enforce:
            if method["effect"] != "read" and not self.allow_write:
                raise OmniError("Operator policy denies writes/control; restart with --allow-write")
            if risky(method, params) and not self.allow_risk:
                raise OmniError("Operator policy denies destructive/replacement/history operations; --allow-risk required")
            self.check_files(name, params)
        return method

    def check_files(self, name, params):
        if name not in {"session.save", "export.render", "export.stems", "audio.split_stems"}:
            return
        key = "folder" if name in {"export.stems", "audio.split_stems"} else "path"
        path = params.get(key)
        if not path or not Path(path).is_absolute() or self.output_root is None:
            raise OmniError("Explicit absolute output path and operator --output-root required")
        target = Path(path).resolve()
        if not target.is_relative_to(self.output_root):
            raise OmniError("Output escapes operator output root")
        if key == "folder":
            if not target.is_dir() or any(target.iterdir()):
                raise OmniError("Output folder must exist and be empty")
        elif target.exists() or not target.parent.is_dir():
            raise OmniError("Output exists or parent is missing; existing files are never overwritten")
        if params.get("overwrite"):
            raise OmniError("File overwrite is not supported by this server")

    async def call(self, name, params=None):
        params = params or {}
        await self.refresh()
        self.check(name, params)
        return await self.client.call(name, params)

    async def wait(self, job, deadline=120, progress=None):
        if not 0 < deadline <= 600:
            raise OmniError("Job deadline must be >0 and <=600 seconds")
        end = time.monotonic() + deadline
        journal = []
        while time.monotonic() < end:
            state = await self.call("jobs.wait", {"job": job, "timeout_seconds": min(9, max(0, end-time.monotonic()))})
            journal.append({"state": state["state"], "progress": state.get("progress")})
            if progress:
                await progress(state.get("progress", 0), 1)
            if state["state"] == "done":
                return {"job": job, "state": "done", "result": state.get("result"), "progress": journal}
            if state["state"] != "running":
                raise OmniError("Job failed: " + json.dumps(self.client.sanitize(state.get("error"))))
            await asyncio.sleep(min(.1, max(0, end-time.monotonic())))
        raise OmniError(f"Job {job} deadline reached; job may still run. Inspect jobs.get; do not restart it.")

    async def inspect(self, limit=16):
        result = {"catalog_provenance": self.provenance, "capabilities": self.client.capabilities,
                  "data_is_untrusted": True}
        for name in ("session.info", "transport.get", "scenes.list", "arrangement.get", "mixer.get"):
            if name in self.methods():
                value = await self.call(name)
                result[name] = compact(value, limit)
        if "tracks.list" in self.methods():
            tracks = await self.call("tracks.list")
            result["track_count"] = len(tracks)
            result["tracks"] = []
            for track in tracks[:limit]:
                item = {k: track[k] for k in ("id", "name", "kind", "instrument", "volume_db", "pan") if k in track}
                for name in ("clips.list", "automation.list", "devices.list"):
                    if name in self.methods():
                        params = {"chain": {"track": track["id"]}} if name == "devices.list" else {"track": track["id"]}
                        item[name] = compact(await self.call(name, params), limit)
                result["tracks"].append(item)
        return result

    async def preview(self, steps, label="custom"):
        await self.refresh()
        if not 1 <= len(steps) <= 128:
            raise OmniError("Plans require 1..128 steps")
        # Validate all static schemas and every domain before any mutation; symbolic refs
        # are checked after resolution, and are never used to bypass policy.
        for step in steps:
            if step["method"] not in self.methods():
                raise OmniError("Unknown or domain-filtered method: " + step["method"])
            if not has_ref(step.get("params", {})):
                self.check(step["method"], step.get("params", {}), enforce=False)
        plan_id = secrets.token_hex(16)
        plan = {"id": plan_id, "label": label, "steps": copy.deepcopy(steps),
                "generation": self.client.generation, "created": time.monotonic()}
        if len(self.plans) >= 32:
            self.plans.pop(next(iter(self.plans)))
        self.plans[plan_id] = plan
        return {"plan_id": plan_id, "label": label, "changes": steps,
                "authorization": "Apply requires operator startup policy, not model approval. Plans expire in 300s.",
                "risks": [s["method"] for s in steps if risky(self.methods()[s["method"]], s.get("params", {}))]}

    async def apply(self, plan_id, progress=None):
        async with self.lock:
            plan = self.plans.pop(plan_id, None)
            if not plan or time.monotonic()-plan["created"] > 300:
                raise OmniError("Unknown, consumed or expired preview")
            await self.refresh()
            if plan["generation"] != self.client.generation:
                raise OmniError("Host reconnected; generate a fresh preview")
            # Fail early on policy for all steps, including symbolic destinations.
            for step in plan["steps"]:
                method = self.methods().get(step["method"])
                if method is None or (method["effect"] != "read" and not self.allow_write) or (risky(method, step.get("params", {})) and not self.allow_risk):
                    raise OmniError("Plan preflight denied by domain or operator risk/write policy")
                if not has_ref(step.get("params", {})):
                    self.check(step["method"], step.get("params", {}))
            results, journal = {}, []
            for index, step in enumerate(plan["steps"]):
                entry = {"index": index, "method": step["method"], "state": "started"}
                journal.append(entry)
                try:
                    params = resolve(step.get("params", {}), results)
                    value = await self.call(step["method"], params)
                    if step.get("wait") and isinstance(value, dict) and "job" in value:
                        value = await self.wait(value["job"], progress=progress)
                    results[step.get("as", str(index))] = value
                    entry.update(state="done", result=value)
                    if progress:
                        await progress(index+1, len(plan["steps"]))
                except Exception as error:
                    entry.update(state="failed_or_unknown", error=str(error))
                    return {"state": "partial_failure", "journal": journal,
                            "recovery": "Inspect live state before resuming. No automatic rollback or replay; undo cannot undo file writes."}
            return {"state": "done", "journal": journal}


def has_ref(value):
    if isinstance(value, dict):
        return "$ref" in value or any(has_ref(v) for v in value.values())
    return isinstance(value, list) and any(has_ref(v) for v in value)


def resolve(value, results):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            keys = value["$ref"].split(".")
            result = results[keys.pop(0)]
            for key in keys:
                result = result[int(key)] if isinstance(result, list) else result[key]
            return result
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    return value


def compact(value, limit):
    if isinstance(value, list):
        return {"count": len(value), "items": [compact(v, limit) for v in value[:limit]]}
    if isinstance(value, dict):
        return {k: compact(v, limit) for k, v in value.items()}
    if isinstance(value, str):
        return value[:512]
    return value
