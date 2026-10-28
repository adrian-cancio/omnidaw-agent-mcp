"""Resource search boundary tests, using disposable components and databases."""
import sqlite3
import sys

import pytest

from omnidaw_agent.client import OmniError
from omnidaw_agent.resource_search import ResourceSearch


@pytest.fixture
def configured(tmp_path):
    repo = tmp_path / "component"
    package = repo / "sample_indexer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "db.py").write_text(
        "LATEST_VERSION=1\ndef schema_version(conn):\n"
        " return conn.execute('PRAGMA user_version').fetchone()[0]\n"
    )
    (package / "reports.py").write_text(
        "def query(conn, **kwargs):\n"
        " assert conn.execute('PRAGMA query_only').fetchone()[0] == 1\n"
        " try: conn.execute('CREATE TABLE forbidden(x)')\n"
        " except Exception: pass\n"
        " else: raise AssertionError('writable')\n"
        " return conn.execute('SELECT rel_path, size, sha256, audio_descriptor FROM path').fetchall()\n"
    )
    root = tmp_path / "library"
    root.mkdir()
    (root / "Kick.wav").write_bytes(b"sound")
    db = tmp_path / "index.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript('PRAGMA user_version=1; CREATE TABLE path(root, rel_path, size, sha256, audio_descriptor, missing);')
        conn.execute('INSERT INTO path VALUES(?,?,?,?,?,0)',
                     (str(root), 'Kick.wav', 5, 'indexed-hash', '{"analysis":null}'))
    return dict(component_repo=repo, python_executable=sys.executable, database=db, library_root=root)


async def test_disabled():
    search = ResourceSearch()
    assert not search.status()["available"]
    with pytest.raises(OmniError, match="not configured"):
        await search.search({})


async def test_query_readonly_and_provenance(configured):
    before = configured['database'].read_bytes()
    result = await ResourceSearch(**configured).search({"path_contains": "KICK"})
    assert result['results'][0]['audio_descriptor'] == '{"analysis":null}'
    assert result['results'][0]['current_path'].endswith('Kick.wav')
    assert result['provenance']['identity_verified'] is False
    assert configured['database'].read_bytes() == before


@pytest.mark.parametrize('filters', [{"include_missing": False}, {"sr": True}, {"sr": 44100.0}, {"bpm_min": "140"}, {"key_provenance": "guess"}, {"bpm_min": float('nan')}, {"duration_min": 4, "duration_max": 2}, {"folder": "../outside"}])
async def test_invalid_filters(configured, filters):
    with pytest.raises(OmniError, match='filter'):
        await ResourceSearch(**configured).search(filters)


@pytest.mark.parametrize('field', ['component_repo', 'database', 'library_root', 'python_executable'])
async def test_missing_paths(configured, tmp_path, field):
    configured[field] = tmp_path / 'absent'
    adapter = ResourceSearch(**configured)
    assert not adapter.status()['available']
    with pytest.raises(OmniError):
        await adapter.search({})


async def test_corrupt_database(configured):
    configured['database'].write_bytes(b'not sqlite')
    with pytest.raises(OmniError):
        await ResourceSearch(**configured).search({})


async def test_schema_mismatch(configured):
    with sqlite3.connect(configured['database']) as conn:
        conn.execute('PRAGMA user_version=2')
    with pytest.raises(OmniError, match='schema'):
        await ResourceSearch(**configured).search({})


@pytest.mark.parametrize('mode', ['root', 'traversal', 'symlink', 'missing'])
async def test_scope(configured, tmp_path, mode):
    with sqlite3.connect(configured['database']) as conn:
        if mode == 'root':
            conn.execute('UPDATE path SET root=?', (str(tmp_path),))
        elif mode == 'traversal':
            conn.execute("UPDATE path SET rel_path='../outside'")
        elif mode == 'missing':
            conn.execute("UPDATE path SET rel_path='absent.wav'")
        else:
            outside = tmp_path / 'outside'
            outside.write_text('sound')
            (configured['library_root'] / 'Kick.wav').unlink()
            (configured['library_root'] / 'Kick.wav').symlink_to(outside)
    with pytest.raises(OmniError):
        await ResourceSearch(**configured).search({})


@pytest.mark.parametrize('code, kwargs, message', [
    ('import time; time.sleep(5)', {'timeout': 0.1}, 'timed out'),
    ("print('x'*100000)", {'max_output_bytes': 1000}, 'output'),
])
async def test_worker_bounds(configured, code, kwargs, message):
    (configured['component_repo'] / 'sample_indexer' / 'reports.py').write_text(code)
    with pytest.raises(OmniError, match=message):
        await ResourceSearch(**configured, **kwargs).search({})


@pytest.mark.parametrize('limit', [True, 0, 101, '10'])
async def test_invalid_limit(configured, limit):
    with pytest.raises(OmniError, match='limit'):
        await ResourceSearch(**configured).search({}, limit=limit)
