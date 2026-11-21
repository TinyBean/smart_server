import time
import uuid
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest
from conftest import event
from test_web import client, runtime  # noqa: F401

from app.web import create_apps


async def test_arrival_settings_validation_and_regeneration(runtime):  # noqa: F811
    _, admin = create_apps(runtime)
    async with client(admin) as http:
        assert (await http.get("/api/arrival")).json()["settings"]["text"] == "欢迎回家。"
        response = await http.put("/api/arrival", json={"enabled": True, "text": "  欢迎回家！  "})
        assert response.status_code == 200 and response.json()["settings"]["text"] == "欢迎回家！"
        assert runtime.store.get("arrival_settings")["text"] == "欢迎回家！"
        assert (await http.put("/api/arrival", json={"text": "\t"})).status_code == 422
        assert (await http.put("/api/arrival", json={"text": "字"*1501})).status_code == 422
        assert (await http.post("/api/arrival/regenerate")).status_code == 202
        assert runtime.arrival.force and runtime.arrival.audio is None


@pytest.fixture
async def arrival_simulation(runtime, monkeypatch):  # noqa: F811
    runtime.mqtt.connected = runtime.ha.connected = runtime.arrival.device_online = True
    monkeypatch.setattr(runtime.arrival, "sync", AsyncMock(side_effect=AssertionError("simulation must not synthesize")))
    frame = np.zeros((240, 320, 3), np.uint8)
    await runtime.frames.set(cv2.imencode(".jpg", frame)[1].tobytes(), frame)
    return {"event_id": str(uuid.uuid4()), "expected_version": None}


async def test_arrival_simulation_uses_persisted_ha_event_and_deduplicates(runtime, arrival_simulation, monkeypatch):  # noqa: F811
    published = []

    def publish(topic, message):
        assert runtime.store.event(message["event_id"])["event_type"] == "enter"
        published.append((topic, message))
        return True

    monkeypatch.setattr(runtime.mqtt, "publish", publish)
    _, admin = create_apps(runtime)
    async with client(admin) as http:
        response = await http.post("/api/events/simulate-enter", json=arrival_simulation)
        assert response.status_code == 202
        assert runtime.store.events()[0]["source"] == "simulation"
        assert published[0][0] == "vision/event" and "created" not in published[0][1]
        assert (await http.post("/api/events/simulate-enter", json=arrival_simulation)).json()["duplicate"]
        assert len(published) == 1
        collision = {**arrival_simulation, "expected_items": []}
        assert (await http.post("/api/events/simulate-exit", json=collision)).status_code == 409


@pytest.mark.parametrize("condition,status", [("offline", 503), ("busy", 409), ("changed", 409), ("disabled", 409), ("no_frame", 503)])
async def test_arrival_simulation_rejects_unready_state(runtime, arrival_simulation, condition, status):  # noqa: F811
    if condition == "offline":
        runtime.arrival.device_online = False
    elif condition == "busy":
        runtime.store.create_playback("cmd", event(runtime.store), "v", [], time.time()+30)
    elif condition == "changed":
        arrival_simulation["expected_version"] = "a"*64
    elif condition == "disabled":
        runtime.arrival.settings.enabled = False
    elif condition == "no_frame":
        await runtime.frames.clear()
    _, admin = create_apps(runtime)
    async with client(admin) as http:
        assert (await http.post("/api/events/simulate-enter", json=arrival_simulation)).status_code == status


async def test_arrival_api_ingress_only_and_uncalibrated_mode_rejected(runtime):  # noqa: F811
    media, admin = create_apps(runtime)
    async with client(admin) as http:
        settings = runtime.settings.model_dump()
        settings["entry_detection_mode"] = "door_reveal"
        assert (await http.put("/api/settings", json=settings)).status_code == 409
        assert runtime.settings.entry_detection_mode == "zone_crossing"
        assert (await http.post("/api/arrival/reference", json={"state": "closed"})).status_code == 409
    runtime.dev = False
    async with client(admin, "10.0.0.8") as http:
        assert (await http.get("/api/arrival")).status_code == 403
    async with client(media) as http:
        assert (await http.get("/api/arrival")).status_code == 404
