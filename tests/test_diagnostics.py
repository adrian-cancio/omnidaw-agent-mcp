"""Diagnostics safety: real local subprocess/WAV checks; mocked host and CLI labeled."""
import asyncio
import hashlib
import json
import struct
import sys
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnidaw_agent.client import OmniError
from omnidaw_agent.diagnostics import (
    AudioDiag,
    bounded_process,
    diagnose_preview,
    export_and_diagnose,
)


@pytest.fixture
def wav_file(tmp_path):
    """Real, non-silent stereo PCM; not a host render or musical-quality claim."""
    path = tmp_path / "render.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<hh", 4096, -4096) * 480)
    return path


@pytest.fixture
def mock_cli(tmp_path):
    """Synthetic analyzer package run by real Python, not upstream audio-diag."""
    repo = tmp_path / "mock-analyzer"
    package = repo / "audio_diag"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("# Mock CLI availability marker.\n")
    (repo / "report.schema.json").write_text(json.dumps({"type": "object"}))

    def configure(source):
        (package / "__main__.py").write_text(source)
        return AudioDiag(repo=repo, python=sys.executable, timeout=2)

    return configure


def mock_engine(tmp_path, journal):
    """Mock host executor: no DAW calls and no export writes."""
    return SimpleNamespace(
        output_root=tmp_path,
        plans={"plan": {"diagnostic": {"path": str(tmp_path / "render.wav")}}},
        apply=AsyncMock(return_value={"state": "done", "journal": journal}),
    )


def test_optional_absence():
    assert not AudioDiag().status()["available"]


async def test_unknown_export_never_analyzes_or_replays():
    engine = SimpleNamespace(apply=AsyncMock(return_value={
        "state": "partial_failure", "journal": [{"state": "failed_or_unknown"}],
    }))
    adapter = SimpleNamespace(analyze=AsyncMock())
    result = await export_and_diagnose(engine, adapter, "plan", "/output.wav")
    assert result["state"] == "export_failed_or_unknown"
    engine.apply.assert_awaited_once_with("plan", progress=None)
    adapter.analyze.assert_not_awaited()


async def test_absent_diagnostic_fails_explicitly(tmp_path):
    with pytest.raises(OmniError, match="unavailable"):
        await AudioDiag().analyze(tmp_path / "x.wav", tmp_path)


async def test_bounded_process_success_and_nonzero(tmp_path):
    assert await bounded_process(
        [sys.executable, "-c", "print('ok')"], cwd=tmp_path,
    ) == b"ok\n"
    with pytest.raises(OmniError, match="exit 7.*export retained"):
        await bounded_process([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_bounded_process_rejects_oversized_stream(tmp_path, stream):
    with pytest.raises(OmniError, match="output exceeds limit"):
        await bounded_process(
            [sys.executable, "-c", f"import sys; sys.{stream}.write('x' * 65536)"],
            cwd=tmp_path, maximum=1024,
        )


async def test_bounded_process_times_out(tmp_path):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bounded_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path, timeout=0.1,
        ), timeout=5)


@pytest.mark.parametrize("raw", ["not JSON", '{"value": NaN}', '[]'])
async def test_mock_cli_malformed_report_retains_wav(mock_cli, wav_file, raw):
    adapter = mock_cli(f"print({raw!r})\n")
    original = wav_file.read_bytes()
    with pytest.raises(OmniError, match="analysis failed.*export retained"):
        await adapter.analyze(wav_file, wav_file.parent)
    assert wav_file.read_bytes() == original


@pytest.mark.parametrize("mismatch", ["version", "hash", "path", None])
async def test_mock_cli_identity_checks(mock_cli, wav_file, mismatch):
    digest = hashlib.sha256(wav_file.read_bytes()).hexdigest()
    report = {"version": "0.1.0", "file": {"path": str(wav_file), "sha256": digest}}
    if mismatch == "version":
        report["version"] = "999"
    elif mismatch == "hash":
        report["file"]["sha256"] = "0" * 64
    elif mismatch == "path":
        report["file"]["path"] = str(wav_file.with_name("other.wav"))
    adapter = mock_cli(f"print({json.dumps(report)!r})\n")
    if mismatch:
        with pytest.raises(OmniError, match="analysis failed.*export retained"):
            await adapter.analyze(wav_file, wav_file.parent)
    else:
        result = await adapter.analyze(wav_file, wav_file.parent)
        assert result["state"] == "analyzed"
        assert result["sha256"] == digest
        assert result["report"] == report


async def test_mock_cli_cannot_analyze_outside_root(mock_cli, wav_file):
    adapter = mock_cli("raise AssertionError('must not launch')\n")
    with pytest.raises(OmniError, match="under output-root"):
        await adapter.analyze(wav_file, wav_file.parent / "restricted")


async def test_completed_export_analysis_failure_never_replays(tmp_path, wav_file):
    engine = mock_engine(tmp_path, [{"method": "export.render", "result": {
        "state": "done", "job": "job-1",
    }}])
    adapter = SimpleNamespace(analyze=AsyncMock(side_effect=OmniError("mock analyzer failed")))
    original = wav_file.read_bytes()
    result = await export_and_diagnose(engine, adapter, "plan")
    assert result["state"] == "analysis_failed"
    assert result["wav"]["non_silent"] is True
    assert result["artifact"] == {
        "path": str(wav_file), "job": "job-1", "sha256": hashlib.sha256(original).hexdigest(),
    }
    assert "Never replay" in result["recovery"]
    engine.apply.assert_awaited_once()
    adapter.analyze.assert_awaited_once()
    assert wav_file.read_bytes() == original


@pytest.mark.parametrize("journal", [
    [],
    [{"method": "export.render", "result": {"state": "running", "job": "job-1"}}],
    [{"method": "export.render", "result": {"state": "done"}}],
    [{"method": "export.render", "result": {"state": "done", "job": ""}}],
    [{"method": "export.render", "result": {"state": "done", "job": "j"}}] * 2,
])
async def test_unconfirmed_job_is_unknown_without_analysis(tmp_path, journal):
    engine = mock_engine(tmp_path, journal)
    adapter = SimpleNamespace(analyze=AsyncMock())
    result = await export_and_diagnose(engine, adapter, "plan")
    assert result["state"] == "export_outcome_unknown"
    engine.apply.assert_awaited_once()
    adapter.analyze.assert_not_awaited()


@pytest.mark.parametrize("entry", [
    {"method": "export.render", "result": None},
    {"result": {"state": "done", "job": "job-1"}},
])
async def test_malformed_job_envelope_is_unknown(tmp_path, entry):
    engine = mock_engine(tmp_path, [entry])
    adapter = SimpleNamespace(analyze=AsyncMock())
    result = await export_and_diagnose(engine, adapter, "plan")
    assert result["state"] == "export_outcome_unknown"
    engine.apply.assert_awaited_once()
    adapter.analyze.assert_not_awaited()


async def test_invalid_wav_never_reaches_analyzer(tmp_path):
    (tmp_path / "render.wav").write_bytes(b"not a WAV")
    engine = mock_engine(tmp_path, [{"method": "export.render", "result": {
        "state": "done", "job": "job-1",
    }}])
    adapter = SimpleNamespace(analyze=AsyncMock())
    result = await export_and_diagnose(engine, adapter, "plan")
    assert result["state"] == "wav_rejected"
    adapter.analyze.assert_not_awaited()
    engine.apply.assert_awaited_once()


@pytest.mark.parametrize("start,end", [(0, None), (-1, 4), (4, 4), (8, 4), (0, float("inf")), (False, 4)])
async def test_invalid_range_never_inspects_host(tmp_path, start, end):
    engine = SimpleNamespace(check_files=Mock(), refresh=AsyncMock())
    adapter = SimpleNamespace(status=lambda: {"available": True})
    with pytest.raises(OmniError):
        await diagnose_preview(engine, adapter, str(tmp_path / "render.wav"),
                               str(tmp_path / "checkpoint.omni"), start, end)
    engine.refresh.assert_not_awaited()


@pytest.mark.parametrize("existing", [False, True])
async def test_checkpoint_rejection_precedes_host_inspection(tmp_path, existing):
    save = tmp_path / ("existing.omni" if existing else "wrong.wav")
    if existing:
        save.write_bytes(b"existing user session")
    engine = SimpleNamespace(check_files=Mock(), refresh=AsyncMock())
    adapter = SimpleNamespace(status=lambda: {"available": True})
    with pytest.raises(OmniError, match="Checkpoint must be new|New .omni checkpoint required"):
        await diagnose_preview(engine, adapter, str(tmp_path / "render.wav"), str(save), 0, 4)
    engine.check_files.assert_not_called()
    engine.refresh.assert_not_awaited()
    if existing:
        assert save.read_bytes() == b"existing user session"
