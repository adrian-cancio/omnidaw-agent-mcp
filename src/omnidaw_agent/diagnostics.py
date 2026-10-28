"""Optional CLI diagnostics; host writes remain in the existing single-use executor."""
import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from .audio import regular_reader, verify_wav
from .client import OmniError
from .workflows import export_preview


def sha256(path):
    digest = hashlib.sha256()
    with regular_reader(path) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def bounded_process(argv, *, cwd=None, timeout=90, maximum=2 * 1024 * 1024):
    process = await asyncio.create_subprocess_exec(*argv, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix")

    async def read(stream):
        chunks, size = [], 0
        while chunk := await stream.read(65536):
            size += len(chunk)
            if size > maximum:
                raise OmniError("Diagnostic output exceeds limit")
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.gather(read(process.stdout), read(process.stderr))
            code = await process.wait()
    except BaseException:
        if os.name == "posix":
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if code:
        raise OmniError(f"Diagnostic component failed (exit {code}); export retained")
    return stdout


class AudioDiag:
    def __init__(self, repo=None, python=None, timeout=90):
        self.repo = Path(repo).resolve() if repo else None
        # Preserve venv symlink invocation: resolving it selects the base Python.
        self.python = Path(python).expanduser().absolute() if python else Path(sys.executable)
        self.timeout = timeout

    def _is_package_installed(self):
        if not self.python or not self.python.is_file():
            return False
        if str(self.python) == sys.executable:
            import importlib.util
            return importlib.util.find_spec("audio_diag") is not None
        try:
            proc = subprocess.run(
                [str(self.python), "-I", "-B", "-c", "import audio_diag"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def status(self):
        if self.repo is not None:
            valid = bool(self.python and self.python.is_file()
                         and ((self.repo / "audio_diag/cli.py").is_file() or (self.repo / "audio_diag/__init__.py").is_file())
                         and ((self.repo / "report.schema.json").is_file() or (self.repo / "audio_diag/report.schema.json").is_file()))
            return {
                "available": valid,
                "source": "repo" if valid else None,
                "reason": "configured CLI (repo checkout); runtime checked on call" if valid
                          else "configured audio-diag repo path missing or invalid",
                "transport": "argv CLI JSON stdout",
            }
        installed = self._is_package_installed()
        return {
            "available": installed,
            "source": "package" if installed else None,
            "reason": "installed package; runtime checked on call" if installed
                      else "audio-diag not installed and repo not configured",
            "transport": "argv CLI JSON stdout",
        }

    def _load_schema(self):
        if self.repo:
            for candidate in (self.repo / "report.schema.json", self.repo / "audio_diag" / "report.schema.json"):
                if candidate.is_file():
                    return json.loads(candidate.read_text(encoding="utf-8"))
        try:
            import audio_diag
            if hasattr(audio_diag, "load_schema"):
                return audio_diag.load_schema()
        except Exception:
            pass
        try:
            from importlib import resources
            ref = resources.files("audio_diag").joinpath("report.schema.json")
            with ref.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
        if self.python and self.python.is_file():
            proc = subprocess.run(
                [str(self.python), "-B", "-m", "audio_diag", "schema"],
                cwd=self.repo if self.repo and self.repo.is_dir() else None,
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0:
                return json.loads(proc.stdout)
        raise OmniError("Unable to load audio-diag report schema")

    async def analyze(self, path, root, bpm=None, intents=None):
        if not self.status()["available"]:
            raise OmniError("audio-diag unavailable; configure component repo or install audio-diag")
        path = Path(path).resolve()
        if root is None or not path.is_relative_to(Path(root).resolve()) or not path.is_file():
            raise OmniError("Diagnostic input must be an existing file under output-root")
        before = await asyncio.to_thread(sha256, path)
        argv = [str(self.python), "-B", "-m", "audio_diag", "analyze", str(path)]
        if bpm is not None:
            argv += ["--bpm", str(bpm)]
        for intent in intents or []:
            argv += ["--intent=" + intent]
        try:
            cwd = self.repo if self.repo and self.repo.is_dir() else None
            raw = await bounded_process(argv, cwd=cwd, timeout=self.timeout)
            report = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            spec = self._load_schema()
            Draft202012Validator(spec).validate(report)
            if report["version"] != "0.1.0":
                raise ValueError("Unsupported audio-diag version")
            if report["file"]["sha256"] != before or Path(report["file"]["path"]).resolve() != path:
                raise ValueError("Analyzer source identity mismatch")
            if await asyncio.to_thread(sha256, path) != before:
                raise ValueError("Diagnostic input changed")
        except OmniError:
            raise
        except Exception as error:
            raise OmniError(f"Diagnostic analysis failed ({type(error).__name__}); export retained") from None
        return {"state": "analyzed", "sha256": before, "report": report,
                "interpretation": "Level 2 is heuristic, never an automatic defect; level 3 is not determinable."}


async def diagnose_preview(engine, adapter, path, save_path, start_beat, end_beat, bpm=None, intents=None):
    for name, value in (("start_beat", start_beat), ("end_beat", end_beat)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1000000:
            raise OmniError(f"Explicit finite {name} in 0..1000000 required")
    if end_beat <= start_beat:
        raise OmniError("end_beat must be greater than start_beat")
    if not adapter.status()["available"]:
        raise OmniError("audio-diag unavailable")
    if not Path(save_path).suffix.lower() == ".omni":
        raise OmniError("New .omni checkpoint required")
    if Path(save_path).exists():
        raise OmniError("Checkpoint must be new")
    engine.check_files("session.save", {"path": save_path})
    engine.check("session.save", {"path": save_path}, enforce=False)
    preview = await export_preview(engine, path=path, start_beat=start_beat, end_beat=end_beat)
    plan = engine.plans[preview["plan_id"]]
    steps = plan["steps"]
    # Timeline preparation can change state: checkpoint immediately before render.
    steps.insert(len(steps) - 1, {"method": "session.save", "params": {"path": save_path}, "wait": True})
    plan["diagnostic"] = {"path": path, "bpm": bpm, "intents": intents or []}
    preview["changes"] = steps
    preview["diagnostic"] = {"component": "audio-diag", "after": "completed job and PCM verification",
                             "reports": "JSON stdout only; no report writes"}
    return preview


async def export_and_diagnose(engine, adapter, plan_id, path=None, progress=None):
    # Only server-owned plan metadata authorizes this completion path.
    plan = getattr(engine, "plans", {}).get(plan_id)
    binding = plan.get("diagnostic") if plan else None
    if binding is None and path is None:
        raise OmniError("Expected diagnostic preview plan")
    result = await engine.apply(plan_id, progress=progress)
    envelope = {"export": result, "recovery": "Never replay export for diagnostic failure. Inspect retained job/artifact."}
    if result["state"] != "done":
        return dict(envelope, state="export_failed_or_unknown")
    exports = [entry for entry in result.get("journal", []) if isinstance(entry, dict) and entry.get("method") == "export.render"]
    if len(exports) != 1 or not isinstance(exports[0].get("result"), dict) or exports[0]["result"].get("state") != "done" or not exports[0]["result"].get("job"):
        return dict(envelope, state="export_outcome_unknown")
    binding = binding or {"path": path}
    path = binding["path"]
    envelope["artifact"] = {"path": path, "job": exports[0]["result"]["job"]}
    try:
        envelope["wav"] = await asyncio.to_thread(verify_wav, path=path, root=engine.output_root)
        envelope["artifact"]["sha256"] = await asyncio.to_thread(sha256, path)
    except Exception as error:
        return dict(envelope, state="wav_rejected", error=str(error))
    try:
        envelope["diagnostic"] = await adapter.analyze(path, engine.output_root,
            bpm=binding.get("bpm"), intents=binding.get("intents"))
        if envelope["diagnostic"]["sha256"] != envelope["artifact"]["sha256"]:
            raise OmniError("Artifact changed between verification and analysis")
    except Exception as error:
        return dict(envelope, state="analysis_failed", error=str(error))
    return dict(envelope, state="done")
