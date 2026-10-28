"""Packaging and modular dependency tests: package-installed vs repo checkout."""
import json
import os
import sqlite3
import struct
import venv
import wave

import pytest

from omnidaw_agent.diagnostics import AudioDiag
from omnidaw_agent.resource_search import ResourceSearch


@pytest.fixture
def packaged_env(tmp_path):
    """A test environment with sample_indexer and audio_diag in its site-packages."""
    env_dir = tmp_path / "pkg_env"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(env_dir)
    exe = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    # Find site-packages in the test venv
    if os.name == "nt":
        site_packages = env_dir / "Lib" / "site-packages"
    else:
        lib_dir = env_dir / "lib"
        py_dirs = [d for d in lib_dir.iterdir() if d.name.startswith("python")]
        site_packages = py_dirs[0] / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    # 1. Install mock sample_indexer in site-packages
    si_pkg = site_packages / "sample_indexer"
    si_pkg.mkdir()
    (si_pkg / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (si_pkg / "db.py").write_text(
        "LATEST_VERSION = 1\n"
        "def schema_version(conn):\n"
        "    return conn.execute('PRAGMA user_version').fetchone()[0]\n"
    )
    (si_pkg / "reports.py").write_text(
        "def query(conn, **kwargs):\n"
        "    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1\n"
        "    return conn.execute('SELECT rel_path, size, sha256, audio_descriptor FROM path').fetchall()\n"
    )

    # 2. Install mock audio_diag in site-packages
    ad_pkg = site_packages / "audio_diag"
    ad_pkg.mkdir()
    schema_dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["tool", "version", "file"],
        "properties": {
            "tool": {"const": "audio-diag"},
            "version": {"type": "string"},
            "file": {
                "type": "object",
                "required": ["path", "sha256"],
                "properties": {
                    "path": {"type": "string"},
                    "sha256": {"type": "string"},
                },
            },
        },
    }
    (ad_pkg / "report.schema.json").write_text(json.dumps(schema_dict))
    (ad_pkg / "__init__.py").write_text(
        '__version__ = "0.1.0"\n'
        "import json, os\n"
        "def load_schema():\n"
        "    p = os.path.join(os.path.dirname(__file__), 'report.schema.json')\n"
        "    with open(p, 'r', encoding='utf-8') as f:\n"
        "        return json.load(f)\n"
    )
    (ad_pkg / "__main__.py").write_text(
        "import argparse, hashlib, json, sys, os\n"
        "p = argparse.ArgumentParser()\n"
        "sub = p.add_subparsers(dest='command', required=True)\n"
        "sub.add_parser('schema')\n"
        "a = sub.add_parser('analyze')\n"
        "a.add_argument('file')\n"
        "a.add_argument('--bpm', type=float)\n"
        "a.add_argument('--intent', action='append', default=[])\n"
        "args = p.parse_args()\n"
        "if args.command == 'schema':\n"
        "    p = os.path.join(os.path.dirname(__file__), 'report.schema.json')\n"
        "    sys.stdout.write(open(p).read() + '\\n')\n"
        "elif args.command == 'analyze':\n"
        "    h = hashlib.sha256(open(args.file, 'rb').read()).hexdigest()\n"
        "    sys.stdout.write(json.dumps({'tool': 'audio-diag', 'version': '0.1.0', "
        "       'file': {'path': args.file, 'sha256': h}}) + '\\n')\n"
    )

    # 3. Create sample library and database
    library = tmp_path / "lib"
    library.mkdir()
    (library / "Kick.wav").write_bytes(b"sample_content")
    db_path = tmp_path / "index.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            "PRAGMA user_version=1;\n"
            "CREATE TABLE path(root, rel_path, size, sha256, audio_descriptor, missing);\n"
        )
        conn.execute(
            "INSERT INTO path VALUES(?, ?, ?, ?, ?, 0)",
            (str(library), "Kick.wav", 14, "fake-sha", '{"status":"ok"}'),
        )

    # 4. Create a valid stereo WAV for audio diagnostics
    wav_path = library / "test.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        w.writeframes(struct.pack("<hh", 4000, -4000) * 4410)

    return {
        "python": exe,
        "database": db_path,
        "library": library,
        "wav": wav_path,
    }


@pytest.mark.asyncio
async def test_resource_search_package_installed(packaged_env):
    """ResourceSearch works with installed package when component_repo is None."""
    search = ResourceSearch(
        component_repo=None,
        python_executable=packaged_env["python"],
        database=packaged_env["database"],
        library_root=packaged_env["library"],
    )
    status = search.status()
    assert status["available"] is True
    assert status["source"] == "package"

    result = await search.search({"path_contains": "Kick"})
    assert len(result["results"]) == 1
    assert result["results"][0]["rel_path"] == "Kick.wav"
    assert result["provenance"]["component"] == "sample_indexer.reports.query"


@pytest.mark.asyncio
async def test_resource_search_repo_fallback(packaged_env, tmp_path):
    """ResourceSearch works with component_repo fallback."""
    custom_repo = tmp_path / "custom_repo"
    pkg = custom_repo / "sample_indexer"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "db.py").write_text(
        "LATEST_VERSION = 1\n"
        "def schema_version(c): return 1\n"
    )
    (pkg / "reports.py").write_text(
        "def query(conn, **kw):\n"
        "    return [{'rel_path': 'Kick.wav', 'size': 14, 'sha256': 'repo-sha', 'audio_descriptor': 'custom'}]\n"
    )

    search = ResourceSearch(
        component_repo=custom_repo,
        python_executable=packaged_env["python"],
        database=packaged_env["database"],
        library_root=packaged_env["library"],
    )
    status = search.status()
    assert status["available"] is True
    assert status["source"] == "repo"

    result = await search.search({})
    assert result["results"][0]["audio_descriptor"] == "custom"


@pytest.mark.asyncio
async def test_audio_diag_package_installed(packaged_env):
    """AudioDiag works with installed package when repo is None."""
    diag = AudioDiag(
        repo=None,
        python=packaged_env["python"],
    )
    status = diag.status()
    assert status["available"] is True
    assert status["source"] == "package"

    # Schema loaded via package/cli without self.repo
    schema = diag._load_schema()
    assert schema.get("title") == "audio-diag v1 report" or "tool" in schema.get("properties", {})

    # Analyze executed without requiring cwd=self.repo
    result = await diag.analyze(packaged_env["wav"], packaged_env["library"])
    assert result["state"] == "analyzed"
    assert result["report"]["tool"] == "audio-diag"
    assert result["report"]["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_audio_diag_repo_fallback(packaged_env, tmp_path):
    """AudioDiag works with repo checkout fallback."""
    custom_repo = tmp_path / "custom_diag_repo"
    pkg = custom_repo / "audio_diag"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "cli.py").write_text("# marker\n")
    (custom_repo / "report.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"version": {"type": "string"}}})
    )
    (pkg / "__main__.py").write_text(
        "import argparse, hashlib, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "sub = p.add_subparsers(dest='command', required=True)\n"
        "a = sub.add_parser('analyze')\n"
        "a.add_argument('file')\n"
        "a.add_argument('--bpm', type=float)\n"
        "a.add_argument('--intent', action='append', default=[])\n"
        "args = p.parse_args()\n"
        "h = hashlib.sha256(open(args.file, 'rb').read()).hexdigest()\n"
        "sys.stdout.write(json.dumps({'version': '0.1.0', 'file': {'path': args.file, 'sha256': h}}) + '\\n')\n"
    )

    diag = AudioDiag(
        repo=custom_repo,
        python=packaged_env["python"],
    )
    status = diag.status()
    assert status["available"] is True
    assert status["source"] == "repo"

    schema = diag._load_schema()
    assert "properties" in schema

    result = await diag.analyze(packaged_env["wav"], packaged_env["library"])
    assert result["state"] == "analyzed"


def test_unconfigured_status_and_reasons():
    """Unconfigured instances report informative availability and reasons."""
    diag = AudioDiag(repo=None, python="/nonexistent/bin/python")
    st = diag.status()
    assert st["available"] is False
    assert st["source"] is None

    search = ResourceSearch(database=None, library_root=None)
    st = search.status()
    assert st["available"] is False
    assert st["source"] is None
    assert "not configured" in st["reason"]


@pytest.mark.asyncio
async def test_server_tools_registered_with_packaged_dependencies(packaged_env):
    """Server registers search and diagnostics tools with packaged dependencies."""
    from omnidaw_agent.server import available_tools

    diag = AudioDiag(repo=None, python=packaged_env["python"])
    search = ResourceSearch(
        component_repo=None,
        python_executable=packaged_env["python"],
        database=packaged_env["database"],
        library_root=packaged_env["library"],
    )

    assert diag.status()["available"] is True
    assert diag.status()["source"] == "package"
    assert search.status()["available"] is True
    assert search.status()["source"] == "package"

    tools = available_tools(diag, search)
    assert "agent_resource_search" in tools
    assert "agent_export_and_diagnose_preview" in tools
    assert "agent_diagnose_wav" in tools

