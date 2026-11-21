import asyncio
import contextlib
import json

import pytest
from websockets.asyncio.server import serve

from app.ha import HAClient


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


class MockHA:
    def __init__(self):
        self.items = [{"uid": "a", "summary": "带钥匙", "status": "needs_action"}]
        self.calls = []
        self.connections = 0

    async def handler(self, socket):
        self.connections += 1
        await socket.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await socket.recv())
        if auth != {"type": "auth", "access_token": "test-token"}:
            await socket.send(json.dumps({"type": "auth_invalid"}))
            return
        await socket.send(json.dumps({"type": "auth_ok"}))
        async for raw in socket:
            message = json.loads(raw)
            self.calls.append(message)
            result = None
            if message.get("service") == "get_items":
                assert message["return_response"] is True
                result = {"response": {"todo.departure_reminders": {"items": self.items}}}
            elif message.get("service") == "update_item":
                assert message["service_data"]["item"] == "a"
                self.items[0]["status"] = "completed"
                result = {"response": None}
            await socket.send(json.dumps({"id": message["id"], "type": "result", "success": True, "result": result}))


async def test_real_websocket_service_response_and_completion(monkeypatch):
    mock = MockHA()
    async with serve(mock.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("FRONTDOOR_HA_WS", f"ws://127.0.0.1:{port}")
        monkeypatch.setenv("FRONTDOOR_HA_TOKEN", "test-token")
        changed = asyncio.Event()
        ha = HAClient("todo.departure_reminders", changed.set)
        task = asyncio.create_task(ha.run())
        try:
            await asyncio.wait_for(changed.wait(), 5)
            items = await ha.items()
            assert items == [{"uid": "a", "text": "带钥匙", "status": "needs_action"}]
            await ha.complete("a")
            assert (await ha.items())[0]["status"] == "completed"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert not ha.connected


async def test_ha_disconnected_fails_closed():
    ha = HAClient("todo.departure_reminders", lambda: None)
    with pytest.raises(ConnectionError):
        await ha.items()
