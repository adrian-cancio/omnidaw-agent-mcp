"""Optional, bounded read-only adapter to a startup-configured sample-indexer.

The component is trusted local code, not a sandbox. No discovery, migrations,
indexing, environment configuration, or content hashing is performed here.
"""
import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from .client import OmniError

_STRINGS = ('path_contains', 'ext', 'folder', 'key')
_INTS = ('sr', 'channels', 'midi_notes_min', 'midi_notes_max', 'pitch_min', 'pitch_max')
_NUMBERS = tuple(
    f'{name}_{bound}'
    for name in ('duration', 'f0', 'sub_peak', 'decay', 'stereo_corr_low', 'lufs',
                 'true_peak', 'centroid', 'rolloff', 'flatness', 'bpm', 'midi_density')
    for bound in ('min', 'max')
) + ('bpm_confidence_min',)
FILTER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        **{k: {'type': 'string', 'minLength': 1, 'maxLength': 1024} for k in _STRINGS},
        **{k: {'type': 'integer', 'minimum': 0} for k in _INTS},
        **{k: {'type': 'number'} for k in _NUMBERS},
        **{k: {'enum': ['est', 'name', 'either']} for k in ('bpm_provenance', 'key_provenance')},
        **{k: {'type': 'boolean'} for k in ('include_errors', 'only_errors')},
    },
}

# Executed by an explicitly configured interpreter with isolated import startup.
# Do not call component db.connect/open_db: those enable writable WAL/migrations.
_WORKER = r'''
import json, sqlite3, sys
from pathlib import Path
repo, database, root, request = sys.argv[1:]
if repo:
    sys.path.insert(0, repo)
try:
    from sample_indexer import db, reports
except ImportError as exc:
    raise ImportError(f"sample_indexer module not importable: {exc}") from exc
request = json.loads(request)
root = Path(root).resolve(strict=True)
with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True, timeout=1) as conn:
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.create_function('casefold', 1, lambda v: v.casefold() if v is not None else None, deterministic=True)
    if db.schema_version(conn) != db.LATEST_VERSION:
        raise ValueError('component schema version mismatch')
    # reports.query has no root predicate and returns no root column. Fail closed
    # for any mixed-root index rather than misattribute rows or duplicate search.
    for row in conn.execute('SELECT DISTINCT root FROM path'):
        if Path(row[0]).resolve(strict=True) != root:
            raise ValueError('database root outside authorized library scope')
    results = []
    for row in reports.query(conn, limit=request['limit'], **request['filters']):
        item = dict(row)
        rel = Path(item['rel_path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('resource path escapes library scope')
        current = (root / rel).resolve(strict=True)
        if not current.is_relative_to(root) or not current.is_file():
            raise ValueError('resource path outside library scope or not a file')
        item['current_path'] = str(current)
        item['current_size'] = current.stat().st_size
        item['indexed_size_matches'] = item['current_size'] == item.get('size')
        results.append(item)
    print(json.dumps({'results': results, 'provenance': {
        'component': 'sample_indexer.reports.query', 'read_only': True,
        'library_root': str(root), 'identity_verified': False,
        'identity_basis': 'indexed SHA-256; current contents not rehashed',
        'metadata_basis': 'indexed nullable measurements and provisional path/name labels; descriptors retained verbatim',
        'freshness': 'current path existence/containment and size checked, metadata freshness unknown',
    }}, allow_nan=False))
'''


class ResourceSearch:
    """Paths are constructor-only authority, never accepted in search filters.

    status() checks configuration presence, not database health. search() validates
    schema/roots in the worker on every invocation and raises OmniError on failure.
    """

    def __init__(self, *, component_repo=None, python_executable=None, database=None,
                 library_root=None, timeout=10.0, max_output_bytes=2 * 1024 * 1024):
        self.repo = Path(component_repo).expanduser().absolute() if component_repo is not None else None
        # Preserve venv symlink invocation: resolving it selects the base Python.
        self.python = Path(python_executable).expanduser().absolute() if python_executable is not None else Path(sys.executable)
        self.database = Path(database).expanduser().absolute() if database is not None else None
        self.library_root = Path(library_root).expanduser().absolute() if library_root is not None else None
        self._paths = (self.repo, self.python, self.database, self.library_root)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError('max_output_bytes must be positive')
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def _is_package_installed(self):
        if not self.python or not self.python.is_file():
            return False
        if str(self.python) == sys.executable:
            import importlib.util
            return importlib.util.find_spec("sample_indexer") is not None
        try:
            proc = subprocess.run(
                [str(self.python), "-I", "-B", "-c", "import sample_indexer"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def status(self):
        if self.database is None or self.library_root is None:
            return {'available': False, 'source': None, 'reason': 'resource search not configured'}
        if not self.database.is_file() or not self.library_root.is_dir():
            return {'available': False, 'source': None, 'reason': 'configured resource search path missing'}
        if not self.python.is_file():
            return {'available': False, 'source': None, 'reason': 'configured resource search path missing'}
        if self.repo is not None:
            try:
                valid = (self.repo.is_dir() and (self.repo / 'sample_indexer' / 'reports.py').is_file()
                         and (self.repo / 'sample_indexer' / 'db.py').is_file())
            except OSError:
                valid = False
            return {
                'available': bool(valid),
                'source': 'repo' if valid else None,
                'reason': 'configured repo checkout (database health checked on search)' if valid else 'configured resource search path missing',
            }
        installed = self._is_package_installed()
        return {
            'available': installed,
            'source': 'package' if installed else None,
            'reason': 'installed package (database health checked on search)' if installed else 'sample_indexer package not installed and repo not configured',
        }

    async def search(self, filters, limit=10):
        """Return {results: [component rows + current path/size], provenance: {...}}."""
        if not isinstance(filters, dict) or list(Draft202012Validator(FILTER_SCHEMA).iter_errors(filters)):
            raise OmniError('invalid or unsupported resource search filter', kind='validation_error')
        for key, value in filters.items():
            if key in _INTS and type(value) is not int:
                raise OmniError('integer resource search filter required', kind='validation_error')
            if isinstance(value, (int, float)) and (isinstance(value, float) and not math.isfinite(value)):
                raise OmniError('non-finite resource search filter', kind='validation_error')
            if key.endswith('_min'):
                maximum = filters.get(key[:-4] + '_max')
                if maximum is not None and value > maximum:
                    raise OmniError('inverted resource search filter bounds', kind='validation_error')
        if 'folder' in filters:
            folder = Path(filters['folder'])
            if folder.is_absolute() or '..' in folder.parts:
                raise OmniError('folder filter must be library-relative', kind='validation_error')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OmniError('resource search limit must be an integer from 1 to 100', kind='validation_error')
        state = self.status()
        if not state['available']:
            raise OmniError(state['reason'], kind='resource_search_unavailable')
        repo_arg = str(self.repo) if self.repo is not None else ""
        request = json.dumps({'filters': filters, 'limit': limit}, allow_nan=False)
        proc = None
        async def run():
            nonlocal proc
            proc = await asyncio.create_subprocess_exec(
                str(self.python), '-I', '-B', '-c', _WORKER, repo_arg, str(self.database), str(self.library_root), request,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            budget = 0

            async def read(stream):
                nonlocal budget
                chunks = []
                while chunk := await stream.read(65536):
                    budget += len(chunk)
                    if budget > self.max_output_bytes:
                        raise OmniError('resource search worker output exceeded bound', kind='resource_search_error')
                    chunks.append(chunk)
                return b''.join(chunks)

            tasks = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
            try:
                out, err = await asyncio.gather(*tasks)
                await proc.wait()
                if proc.returncode:
                    raise OmniError('resource search worker failed: ' + err.decode('utf-8', errors='replace')[-2000:], kind='resource_search_error')
                return json.loads(out)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        try:
            return await asyncio.wait_for(run(), self.timeout)
        except TimeoutError as exc:
            raise OmniError('resource search worker timed out', kind='resource_search_error') from exc
        except (OSError, ValueError) as exc:
            raise OmniError(f'resource search worker failed: {exc}', kind='resource_search_error') from exc
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
