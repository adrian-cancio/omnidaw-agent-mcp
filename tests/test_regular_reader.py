"""Descriptor-level special-file race checks on POSIX."""
import os

import pytest

from omnidaw_agent.audio import regular_reader
from omnidaw_agent.client import OmniError


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO required')
def test_replacement_after_stat_rejected_by_fstat(tmp_path, monkeypatch):
    import omnidaw_agent.audio as audio
    regular = tmp_path / 'regular'
    regular.write_bytes(b'content')
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    real_open = os.open

    def replaced_open(path, flags):
        assert flags & os.O_NONBLOCK
        return real_open(fifo, flags)

    monkeypatch.setattr(audio.os, 'open', replaced_open)
    with pytest.raises(OmniError, match='regular file'):
        with regular_reader(regular):
            pytest.fail('must not yield special-file descriptor')
