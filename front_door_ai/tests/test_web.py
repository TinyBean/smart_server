import hashlib
import time
import uuid
import wave
from unittest.mock import AsyncMock

import cv2
import httpx
import numpy as np
import pytest
from conftest import event

from app.config import DepartureSettings
from app.runtime import Runtime
from app.web import create_apps


@pytest.fixture
def runtime(tmp_path):
    value = Runtime(tmp_path, dev=True)
    yield value
    value.store.close()
    import logging
    logging.getLogger().removeHandler(value.log_buffer)


def client(app, host="127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 1)), base_url="http://test")


async def test_ingress_prefix_and_access_boundary(runtime):
    media, admin = create_apps(runtime)
    runtime.dev = False
    async with client(admin, "172.30.32.2") as http:
        page = await http.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/test-token"})
        assert page.status_code == 200
        assert '<base href="/api/hassio_ingress/test-token/">' in page.text
        assert (await http.get("/static/app.js")).status_code == 200
        assert (await http.get("/static/preview.js")).status_code == 200
    async with client(admin, "10.1.1.2") as http:
        assert (await http.get("/api/settings", headers={"X-Forwarded-For": "172.30.32.2"})).status_code == 403
    async with client(media) as http:
        assert (await http.get("/api/settings")).status_code == 404
        assert (await http.get("/healthz")).status_code == 200


async def test_api_write_validation_and_no_secrets(runtime, settings):
    _, admin = create_apps(runtime)
    async with client(admin) as http:
        response = await http.put("/api/settings", json=settings.model_dump())
        assert response.status_code == 200 and runtime.settings.calibrated
        assert (runtime.data_dir/"config.json").exists()
        assert "mqtt_password" not in (await http.get("/api/settings")).text
        invalid = settings.model_dump()
        invalid["detection_fps"] = -10
        assert (await http.put("/api/settings", json=invalid)).status_code == 422
        assert (await http.put("/api/settings", content=b"x"*32769)).status_code == 413
        assert (await http.post("/api/reminders/sync")).status_code == 202
        assert runtime.reminders.wake.is_set()
        assert (await http.post("/api/reminders/regenerate")).status_code == 202
        assert runtime.reminders.force


async def test_departure_route_persistence_validation_and_disable(runtime):
    from app.config import load_config

    _, admin = create_apps(runtime)
    async with client(admin) as http:
        settings = (await http.get("/api/settings")).json()
        assert settings["departure_path"] == []
        settings["departure_path"] = [[.38, .97], [.62, .65]]
        settings["departure_path_tolerance"] = .04
        response = await http.put("/api/settings", json=settings)
        assert response.status_code == 200
        assert (await http.get("/api/settings")).json() == response.json()
        assert load_config(runtime.data_dir)[1].model_dump(mode="json") == response.json()
        assert runtime.vision.settings.departure_path == [(.38, .97), (.62, .65)]
        assert runtime.status()["departure_path_ready"]
        for changes in ({"departure_path": [[.38, .97]]}, {"departure_path_tolerance": 0}):
            assert (await http.put("/api/settings", json={**settings, **changes})).status_code == 422
        assert runtime.status()["departure_path_ready"]
        settings["departure_path"] = []
        assert (await http.put("/api/settings", json=settings)).status_code == 200
        assert not load_config(runtime.data_dir)[1].departure_path
        assert not runtime.status()["departure_path_ready"]


async def test_departure_settings_validation_persistence_and_independent_channels(runtime):
    media, admin = create_apps(runtime)
    async with client(admin) as http:
        initial = (await http.get("/api/reminders")).json()["settings"]
        assert initial == {"enabled": True, "text": "出门别忘了带手机和钥匙。", "include_todos": True}
        response = await http.put("/api/reminders", json={"enabled": True, "text": "  一路顺风！ ", "include_todos": False})
        assert response.status_code == 200
        assert response.json()["settings"]["text"] == "一路顺风！"
        assert runtime.store.get("departure_settings") == response.json()["settings"]
        assert response.json()["tts_cache_version"] is None
        assert runtime.reminders.wake.is_set()
        assert (await http.get("/api/arrival")).json()["settings"]["text"] == "欢迎回家。"
        for invalid in ({"text": "\n\t"}, {"text": "字"*1501}, {"unexpected": True}):
            assert (await http.put("/api/reminders", json=invalid)).status_code == 422
        page = (await http.get("/")).text
        assert 'data-tab="announcements"' in page
        assert 'data-tab="arrival"' not in page and 'data-tab="reminders"' not in page
    async with client(media) as http:
        assert (await http.put("/api/reminders", json=initial)).status_code == 404
    async with client(admin, "10.1.1.2") as http:
        assert (await http.put("/api/reminders", json=initial)).status_code == 403


async def test_ingress_snapshot_delivers_newest_frame_without_cache(runtime):
    _, admin = create_apps(runtime)
    runtime.dev = False
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    async with client(admin, "172.30.32.2") as http:
        assert (await http.get("/api/snapshot")).status_code == 503
        for brightness in (0, 150):
            image[:] = brightness
            jpg = cv2.imencode(".jpg", image)[1].tobytes()
            await runtime.frames.set(jpg, image)
            response = await http.get("/api/snapshot")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/jpeg"
            assert response.headers["cache-control"] == "no-store"
            assert response.content == jpg


async def test_snapshot_audio_etag_and_path_validation(runtime):
    media, _ = create_apps(runtime)
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    jpg = cv2.imencode(".jpg", image)[1].tobytes()
    key = hashlib.sha256(b"test").hexdigest()
    file = runtime.tts.cache/f"{key}.wav"
    with wave.open(str(file), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0"*1600)
    async with client(media) as http:
        assert (await http.get("/snapshot")).status_code == 503
        await runtime.frames.set(jpg, image)
        response = await http.get("/snapshot")
        assert response.content == jpg and response.headers["content-type"] == "image/jpeg"
        response = await http.get(f"/audio/{key}.wav")
        assert int(response.headers["content-length"]) == file.stat().st_size
        assert response.content == file.read_bytes()
        assert (await http.get(f"/audio/{key}.wav", headers={"If-None-Match": response.headers["etag"]})).status_code == 304
        assert (await http.get("/audio/invalid.wav")).status_code == 404


async def test_events_use_sqlite_on_owner_loop(runtime):
    _, admin = create_apps(runtime)
    key = event(runtime.store)
    async with client(admin) as http:
        response = await http.get("/api/events")
        assert response.status_code == 200
        assert response.json()[0]["event_id"] == key


@pytest.fixture
async def simulation_ready(runtime, monkeypatch):
    runtime.mqtt.connected = runtime.ha.connected = True
    runtime.reminders.device_online = True
    monkeypatch.setattr(runtime.reminders, "sync", AsyncMock())
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    await runtime.frames.set(cv2.imencode(".jpg", image)[1].tobytes(), image)
    return {"event_id": str(uuid.uuid4()), "expected_items": [], "expected_version": None}


async def test_simulation_persists_before_mqtt_and_deduplicates(runtime, simulation_ready, monkeypatch):
    messages = []

    def publish(topic, payload):
        assert runtime.store.event(payload["event_id"])["source"] == "simulation"
        messages.append((topic, payload))
        return True

    monkeypatch.setattr(runtime.mqtt, "publish", publish)
    _, admin = create_apps(runtime)
    async with client(admin) as http:
        first = await http.post("/api/events/simulate-exit", json=simulation_ready)
        assert first.status_code == 202 and not first.json()["duplicate"]
        event = runtime.store.events()[0]
        assert event["event_type"] == "exit" and event["source"] == "simulation"
        assert len(messages) == 1 and messages[0][0] == "vision/event"
        assert "created" not in messages[0][1]
        assert (await http.get(event["snapshot"])).status_code == 200
        assert (await http.post("/api/events/simulate-exit", json=simulation_ready)).json()["duplicate"]
        assert len(messages) == 1
        simulation_ready["event_id"] = str(uuid.uuid4())
        assert (await http.post("/api/events/simulate-exit", json=simulation_ready)).status_code == 429
        assert len(runtime.store.events()) == 1


@pytest.mark.parametrize("condition,status", [
    ("offline", 503), ("no_frame", 503), ("items_changed", 409),
    ("version_changed", 409), ("not_cached", 409), ("playing", 409), ("disabled", 409),
    ("text_not_cached", 409),
])
async def test_simulation_refuses_unready_or_changed_state(runtime, simulation_ready, condition, status, monkeypatch):
    if condition == "offline":
        runtime.reminders.device_online = False
    elif condition == "no_frame":
        monkeypatch.setattr(runtime.frames, "fresh", lambda: None)
    elif condition in {"items_changed", "not_cached"}:
        runtime.reminders.items = [{"uid": "new", "text": "new", "status": "needs_action"}]
        if condition == "not_cached":
            simulation_ready["expected_items"] = ["new"]
    elif condition == "version_changed":
        simulation_ready["expected_version"] = "a"*64
    elif condition == "playing":
        runtime.store.create_playback("playing", "existing", "", [], time.time()+60, state="playing")
    elif condition == "disabled":
        runtime.reminders.configure(DepartureSettings(enabled=False))
    elif condition == "text_not_cached":
        runtime.reminders.configure(DepartureSettings(include_todos=False))
        runtime.reminders.audio = {"version": "b"*64}
        simulation_ready["expected_version"] = "b"*64
    _, admin = create_apps(runtime)
    async with client(admin) as http:
        response = await http.post("/api/events/simulate-exit", json=simulation_ready)
        assert response.status_code == status
        assert runtime.store.events() == []


async def test_simulation_is_ingress_only(runtime, simulation_ready):
    media, admin = create_apps(runtime)
    runtime.dev = False
    async with client(media) as http:
        assert (await http.post("/api/events/simulate-exit", json=simulation_ready)).status_code == 404
    async with client(admin, "10.1.1.2") as http:
        assert (await http.post("/api/events/simulate-exit", json=simulation_ready)).status_code == 403


async def test_slow_stream_consumer_skips_intermediate_frames(runtime):
    media, _ = create_apps(runtime)

    class Request:
        async def is_disconnected(self):
            return False

    image = np.zeros((240, 320, 3), dtype=np.uint8)
    await runtime.frames.set(b"first", image)
    endpoint = next(route.endpoint for route in media.routes if route.path == "/stream")
    fast = (await endpoint(Request())).body_iterator
    slow = (await endpoint(Request())).body_iterator
    try:
        assert b"first" in await anext(fast)
        assert b"first" in await anext(slow)
        for index in range(100):
            await runtime.frames.set(f"frame-{index:03}".encode(), image)
            assert f"frame-{index:03}".encode() in await anext(fast)
        # No per-client queue: a paused response resumes at the newest frame.
        assert b"frame-099" in await anext(slow)
    finally:
        await fast.aclose()
        await slow.aclose()
