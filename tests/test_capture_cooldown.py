"""Test capture_with_cooldown helper and batch limits."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from omnidaw_agent.engine import Engine
from omnidaw_agent.client import OmniError


@pytest.mark.asyncio
async def test_capture_with_cooldown_basic():
    """Basic capture with cooldown completes successfully."""
    client = MagicMock()
    client.capabilities = ["analysis"]
    client.generation = 1
    
    engine = Engine(client, allow_write=True, allow_risk=True)
    engine.call = AsyncMock(return_value={"job": 1})
    engine.wait = AsyncMock(return_value={"job": 1, "state": "done", "result": {"rms_dbfs": -12.5}})
    
    result = await engine.capture_with_cooldown(source="master", seconds=2, cooldown=0.1)
    
    assert result["state"] == "done"
    assert result["result"]["rms_dbfs"] == -12.5
    assert engine._capture_count == 1
    engine.call.assert_awaited_once_with("analysis.capture", {"source": "master", "seconds": 2})


@pytest.mark.asyncio
async def test_capture_with_cooldown_batch_limit():
    """Batch limit prevents runaway capture accumulation."""
    client = MagicMock()
    client.capabilities = ["analysis"]
    client.generation = 1
    
    engine = Engine(client, allow_write=True, allow_risk=True)
    engine.call = AsyncMock(return_value={"job": 1})
    engine.wait = AsyncMock(return_value={"job": 1, "state": "done", "result": {}})
    
    # Execute up to batch limit
    for i in range(8):
        result = await engine.capture_with_cooldown(cooldown=0)
        assert result["state"] == "done"
    
    assert engine._capture_count == 8
    
    # Next call should raise
    with pytest.raises(OmniError, match="Capture batch limit.*reached"):
        await engine.capture_with_cooldown(cooldown=0)


@pytest.mark.asyncio
async def test_capture_with_cooldown_reset():
    """Capture count can be reset after transport cycle."""
    client = MagicMock()
    client.capabilities = ["analysis"]
    client.generation = 1
    
    engine = Engine(client, allow_write=True, allow_risk=True)
    engine.call = AsyncMock(return_value={"job": 1})
    engine.wait = AsyncMock(return_value={"job": 1, "state": "done", "result": {}})
    
    # Fill batch
    for i in range(8):
        await engine.capture_with_cooldown(cooldown=0)
    
    # Reset and verify next capture works
    engine._capture_count = 0
    result = await engine.capture_with_cooldown(cooldown=0)
    assert result["state"] == "done"
    assert engine._capture_count == 1


@pytest.mark.asyncio
async def test_capture_with_cooldown_timing(monkeypatch):
    """Cooldown pause is inserted between captures."""
    client = MagicMock()
    client.capabilities = ["analysis"]
    client.generation = 1
    
    engine = Engine(client, allow_write=True, allow_risk=True)
    engine.call = AsyncMock(return_value={"job": 1})
    engine.wait = AsyncMock(return_value={"job": 1, "state": "done", "result": {}})
    
    sleep_calls = []
    original_sleep = asyncio.sleep
    async def mock_sleep(duration):
        sleep_calls.append(duration)
        await original_sleep(0)  # Don't actually sleep in tests
    
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)
    
    await engine.capture_with_cooldown(cooldown=5)
    
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 5


@pytest.mark.asyncio
async def test_capture_with_cooldown_no_cooldown_at_limit():
    """No cooldown inserted after reaching batch limit."""
    client = MagicMock()
    client.capabilities = ["analysis"]
    client.generation = 1
    
    engine = Engine(client, allow_write=True, allow_risk=True)
    engine.call = AsyncMock(return_value={"job": 1})
    engine.wait = AsyncMock(return_value={"job": 1, "state": "done", "result": {}})
    
    sleep_calls = []
    async def mock_sleep(duration):
        sleep_calls.append(duration)
    
    import asyncio
    original_sleep = asyncio.sleep
    asyncio.sleep = mock_sleep
    
    # Fill to limit
    for i in range(7):
        await engine.capture_with_cooldown(cooldown=5)
    
    assert len(sleep_calls) == 7  # Cooldown after each of first 7
    
    # 8th capture (at limit) should not insert cooldown
    sleep_calls.clear()
    await engine.capture_with_cooldown(cooldown=5)
    assert len(sleep_calls) == 0  # No cooldown at batch boundary
    
    asyncio.sleep = original_sleep
