from __future__ import annotations

import asyncio
import json
import logging
import os

from websockets.asyncio.client import connect

LOG = logging.getLogger(__name__)


class HAClient:
    def __init__(self, entity, changed):
        self.entity, self.changed = entity, changed
        self.url = os.environ.get("FRONTDOOR_HA_WS", "ws://supervisor/core/websocket")
        self.token = os.environ.get("FRONTDOOR_HA_TOKEN", os.environ.get("SUPERVISOR_TOKEN", ""))
        self.connected = False
        self.socket = None
        self.next_id = 0
        self.pending = {}

    async def call(self, kind, **data):
        if not self.connected:
            raise ConnectionError("HA not connected")
        self.next_id += 1
        key = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.socket.send(json.dumps({"id": key, "type": kind, **data}))
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(key, None)

    async def items(self):
        result = await self.call("call_service", domain="todo", service="get_items",
                                 target={"entity_id": self.entity},
                                 service_data={"status": ["needs_action", "completed"]}, return_response=True)
        items = result["response"][self.entity]["items"]
        if not isinstance(items, list) or len(items) > 5000:
            raise ValueError("invalid todo response")
        return [{"uid": str(item["uid"]), "text": str(item["summary"]), "status": item["status"]}
                for item in items]

    async def complete(self, uid):
        await self.call("call_service", domain="todo", service="update_item",
                        target={"entity_id": self.entity}, service_data={"item": uid, "status": "completed"})

    async def run(self):
        delay = 1
        while True:
            try:
                async with connect(self.url, proxy=None, open_timeout=5, close_timeout=3,
                                   max_size=2*1024*1024) as socket:
                    self.socket = socket
                    first = json.loads(await asyncio.wait_for(socket.recv(), 5))
                    if first.get("type") != "auth_required":
                        raise ConnectionError("unexpected HA auth flow")
                    await socket.send(json.dumps({"type": "auth", "access_token": self.token}))
                    auth = json.loads(await asyncio.wait_for(socket.recv(), 5))
                    if auth.get("type") != "auth_ok":
                        raise ConnectionError("HA authentication failed")
                    self.next_id += 1
                    await socket.send(json.dumps({"id": self.next_id, "type": "subscribe_events", "event_type": "state_changed"}))
                    self.connected = True
                    delay = 1
                    self.changed()
                    async for raw in socket:
                        message = json.loads(raw)
                        if message.get("type") == "result":
                            future = self.pending.get(message["id"])
                            if future is not None and not future.done():
                                if message.get("success"):
                                    future.set_result(message.get("result"))
                                else:
                                    future.set_exception(RuntimeError("HA service action failed"))
                        elif message.get("type") == "event":
                            if message.get("event", {}).get("data", {}).get("entity_id") == self.entity:
                                self.changed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("HA_RECONNECT type=%s delay=%s", type(exc).__name__, delay)
            finally:
                self.connected = False
                self.socket = None
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(ConnectionError("HA disconnected"))
            await asyncio.sleep(delay)
            delay = min(delay*2, 30)
