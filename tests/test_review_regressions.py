"""Independent review regressions; blocking-reader probes run in killable processes."""
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from omnidaw_agent.audio import verify_wav
from omnidaw_agent.client import OmniError
from omnidaw_agent.diagnostics import AudioDiag


def test_configured_venv_invocation_preserved(tmp_path):
    root = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    executable = root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    adapter = AudioDiag(python=executable)
    prefix = subprocess.check_output([str(adapter.python), '-c', 'import sys; print(sys.prefix)'], text=True).strip()
    assert Path(prefix) == root


@pytest.mark.asyncio
async def test_option_like_intents_are_exact_values(tmp_path):
    repo = tmp_path / 'component'
    package = repo / 'audio_diag'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'cli.py').write_text('')
    (repo / 'report.schema.json').write_text('{"type":"object"}')
    (package / '__main__.py').write_text('''import argparse, hashlib, json
p=argparse.ArgumentParser()
p.add_argument('command'); p.add_argument('file'); p.add_argument('--intent',action='append')
a=p.parse_args()
print(json.dumps({'version':'0.1.0','file':{'path':a.file,'sha256':hashlib.sha256(open(a.file,'rb').read()).hexdigest()},'intents':a.intent}))
''')
    source = tmp_path / 'input.wav'
    source.write_bytes(b'identity fixture; mock CLI only')
    intents = ['--breakdown', '-x', 'ordinary text']
    result = await AudioDiag(repo, sys.executable).analyze(source, tmp_path, intents=intents)
    assert result['report']['intents'] == intents


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO required')
def test_fifo_rejected_without_blocking_open(tmp_path):
    fifo = tmp_path / 'input.wav'
    os.mkfifo(fifo)
    code = '''from omnidaw_agent.audio import verify_wav
from omnidaw_agent.client import OmniError
import sys
try: verify_wav(sys.argv[1],sys.argv[2])
except OmniError: sys.exit(0)
sys.exit(1)
'''
    try:
        result = subprocess.run([sys.executable, '-c', code, str(fifo), str(tmp_path)], timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail('FIFO verification blocked instead of rejecting nonregular input')
    assert result.returncode == 0


@pytest.mark.parametrize('kind', ['missing', 'directory', 'escape'])
def test_invalid_reader_paths(tmp_path, kind):
    root = tmp_path / 'allowed'
    root.mkdir()
    target = root / 'input.wav'
    if kind == 'directory':
        target.mkdir()
    elif kind == 'escape':
        outside = tmp_path / 'outside.wav'
        outside.write_bytes(b'not audio')
        target.symlink_to(outside)
    with pytest.raises(OmniError):
        verify_wav(target, root)
