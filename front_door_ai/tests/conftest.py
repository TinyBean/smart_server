import hashlib
import time
import uuid
import wave
from pathlib import Path

import pytest

from app.config import Connections, Settings
from app.reminder import ReminderManager
from app.store import Store


@pytest.fixture
def settings():
    return Settings(outside_zone=[(0, 0), (1, 0), (1, .35), (0, .35)],
                    transition_zone=[(0, .35), (1, .35), (1, .65), (0, .65)],
                    inside_zone=[(0, .65), (1, .65), (1, 1), (0, 1)])


class FakeHA:
    connected = True

    def __init__(self):
        self.data = []
        self.completed = []
        self.fail_uid = None

    async def items(self):
        if not self.connected:
            raise ConnectionError()
        return [dict(item) for item in self.data]

    async def complete(self, uid):
        if uid == self.fail_uid:
            raise ConnectionError()
        self.completed.append(uid)
        for item in self.data:
            if item["uid"] == uid:
                item["status"] = "completed"


class FakeMQTT:
    connected = True

    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, **kwargs):
        if not self.connected:
            return False
        self.messages.append((topic, payload, kwargs))
        return True


class FakeTTS:
    last_ms = 1

    def __init__(self, cache: Path):
        self.cache = cache
        cache.mkdir(exist_ok=True)
        self.fail = False

    async def prepare(self, text, force=False):
        if self.fail:
            raise RuntimeError("synthetic TTS failure")
        version = hashlib.sha256(text.encode()).hexdigest()
        path = self.cache/f"{version}.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0"*1600)
        return {"version": version, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "duration": .1, "size": path.stat().st_size}


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path/"state.sqlite3")
    ha, mqtt = FakeHA(), FakeMQTT()
    tts = FakeTTS(tmp_path/"audio")
    config = Connections(public_base_url="http://192.168.1.2:18080")
    manager = ReminderManager(store, ha, mqtt, tts, config)
    yield manager, store, ha, mqtt, tts
    store.close()


def item(uid="a", text="带钥匙"):
    return {"uid": uid, "text": text, "status": "needs_action"}


def event(store, kind="exit", age=0):
    key = str(uuid.uuid4())
    store.add_event({"event_id": key, "event_type": kind, "created": time.time()-age})
    return key


async def prepare(rig, items=None):
    manager, _, ha, _, _ = rig
    ha.data = items if items is not None else [item()]
    await manager.sync()
    await manager.message("device/availability", "online")
    manager.audio_status({"command_id": manager.cache_command, "audio_id": "departure",
                          "version": manager.audio["version"], "status": "ready"})
    assert manager.ready


def ack(row, status="finished", **overrides):
    return {"command_id": row["command_id"], "audio_id": "departure", "version": row["version"],
            "status": status, **overrides}
