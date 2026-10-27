"""Offline official-catalog enforcement and independently expected workflow timings."""
import json
from pathlib import Path

import pytest

from omnidaw_agent.client import OmniError
from omnidaw_agent.engine import Engine
from omnidaw_agent.workflows import arrange_timeline, repeat_preview


class Fake:
    capabilities = []
    generation = 1

    def __init__(self):
        self.catalog = json.loads((Path(__file__).parents[1] / 'src/omnidaw_agent/catalog.json').read_text())
        self.calls = []
        self.fail = None

    async def connect(self):
        pass

    async def call(self, name, params):
        self.calls.append((name, params))
        if len(self.calls) == self.fail:
            raise OmniError('unknown mutation outcome')
        if name == 'export.render':
            return {'job': 7}
        if name == 'jobs.wait':
            return {'state': 'done', 'progress': 1, 'result': {'rendered': True}}
        return {'accepted': True}


def engine(tmp_path):
    return Engine(Fake(), allow_write=True, output_root=tmp_path)


async def test_fractional_repetitions(tmp_path):
    e = engine(tmp_path)
    p = await repeat_preview(e, 'audio-track', 2, 1.25, 4, 0.5)
    assert [s['params']['start_beat'] for s in p['changes']] == [0.5, 1.75, 3, 4.25]
    assert [s['params']['length_beats'] for s in p['changes']] == [1.25] * 4
    assert not e.client.calls
    assert (await e.apply(p['plan_id']))['state'] == 'done'
    assert len(e.client.calls) == 4


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf')])
async def test_invalid_length(tmp_path, value):
    e = engine(tmp_path)
    with pytest.raises(OmniError):
        await repeat_preview(e, 0, 0, value, 2)
    assert not e.plans and not e.client.calls


@pytest.mark.parametrize('count,batches', [(31, [31]), (32, [32]), (33, [32, 1]), (64, [32, 32])])
async def test_boundaries(tmp_path, count, batches):
    e = engine(tmp_path)
    paths = [str(tmp_path / f'checkpoint{i}.omni') for i in range(len(batches)-1)]
    p = await repeat_preview(e, 0, 0, 0.5, count, checkpoint_paths=paths)
    assert p['batches'] == batches
    assert [s['method'] for s in p['changes']].count('session.save') == len(paths)
    assert (await e.apply(p['plan_id']))['state'] == 'done'


@pytest.mark.parametrize('failure', [2, 33])
async def test_partial_and_checkpoint_failure(tmp_path, failure):
    e = engine(tmp_path)
    p = await repeat_preview(e, 0, 0, 1, 34, checkpoint_paths=[str(tmp_path / 'c.omni')])
    e.client.fail = failure
    r = await e.apply(p['plan_id'])
    assert r['state'] == 'partial_failure'
    assert r['completed'] == list(range(failure - 1))
    assert r['failed_or_unknown'] == [failure - 1]
    assert r['pending'] == list(range(failure, 35))
    assert len(e.client.calls) == failure
    with pytest.raises(OmniError, match='consumed'):
        await e.apply(p['plan_id'])
    assert len(e.client.calls) == failure


async def test_markers_count_and_limits(tmp_path):
    e = engine(tmp_path)
    with pytest.raises(OmniError, match='checkpoint'):
        await repeat_preview(e, 0, 0, 1, 32, markers=[{'beat': 0, 'name': 'A'}])
    with pytest.raises(OmniError, match='128'):
        await repeat_preview(e, 0, 0, 1, 128, checkpoint_paths=[str(tmp_path / f'{i}.omni') for i in range(3)])
    with pytest.raises(OmniError, match='escapes'):
        await repeat_preview(e, 0, 0, 1, 33, checkpoint_paths=[str(tmp_path.parent / 'outside.omni')])
    with pytest.raises(OmniError):
        await arrange_timeline(e, [{'track': 0, 'slot': 0, 'start_beat': float('nan')}])


@pytest.mark.parametrize('waiting', [False, True])
async def test_progress_failure_never_relabels_or_replays(tmp_path, waiting):
    e = engine(tmp_path)
    steps = [{'method': 'arrangement.place_clip', 'params': {'track': 0, 'slot': 0, 'start_beat': 0}}]
    if waiting:
        steps = [{'method': 'export.render', 'params': {'path': str(tmp_path / 'mix.wav')}, 'wait': True}] + steps
    p = await e.preview(steps)
    notifications = []

    async def broken_progress(done, total):
        notifications.append((done, total))
        raise RuntimeError('notification unavailable')

    result = await e.apply(p['plan_id'], progress=broken_progress)
    assert result['state'] == 'done'
    assert all(entry['state'] == 'done' for entry in result['journal'])
    assert result['progress_warnings'] == [
        {'done': done, 'total': total, 'warning': 'Progress notification failed'}
        for done, total in notifications]
    assert len(notifications) == (3 if waiting else 1)
    assert [name for name, _ in e.client.calls] == (
        ['export.render', 'jobs.wait', 'arrangement.place_clip'] if waiting else ['arrangement.place_clip'])
    with pytest.raises(OmniError, match='consumed'):
        await e.apply(p['plan_id'], progress=broken_progress)
    assert len(e.client.calls) == (3 if waiting else 1)


async def test_checkpoint_extension_and_duplicate_paths(tmp_path):
    e = engine(tmp_path)
    with pytest.raises(OmniError, match='extension'):
        await repeat_preview(e, 0, 0, 1, 33, checkpoint_paths=[str(tmp_path / 'checkpoint.txt')])
    with pytest.raises(OmniError, match='distinct'):
        await repeat_preview(e, 0, 0, 1, 65, checkpoint_paths=[str(tmp_path / 'c.omni')] * 2)
    assert not e.plans and not e.client.calls


@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('appears_after_preview', [False, True])
async def test_checkpoint_existing_destination(tmp_path, overwrite, appears_after_preview):
    e = engine(tmp_path)
    e.allow_overwrite = overwrite
    target = tmp_path / 'c.omni'
    if appears_after_preview:
        p = await repeat_preview(e, 0, 0, 1, 33, checkpoint_paths=[str(target)])
        target.write_text('existing project')
        with pytest.raises(OmniError, match='exists'):
            await e.apply(p['plan_id'])
        with pytest.raises(OmniError, match='consumed'):
            await e.apply(p['plan_id'])
    else:
        target.write_text('existing project')
        with pytest.raises(OmniError, match='exists'):
            await repeat_preview(e, 0, 0, 1, 33, checkpoint_paths=[str(target)])
        assert not e.plans
    assert target.read_text() == 'existing project'
    assert not e.client.calls


async def test_checkpoint_save_domain_filtered(tmp_path):
    e = engine(tmp_path)
    e.domains = {'arrangement'}
    with pytest.raises(OmniError, match='domain-filtered.*session.save'):
        await repeat_preview(e, 0, 0, 1, 33, checkpoint_paths=[str(tmp_path / 'c.omni')])
    assert not e.plans and not e.client.calls


def test_master_catalog_support(tmp_path):
    e = engine(tmp_path)
    e.check('devices.list', {'chain': 'master'})
    e.check('devices.get', {'chain': 'master', 'slot': 0})
    with pytest.raises(OmniError, match='official schema'):
        e.check('params.list', {'chain': 'master'})
