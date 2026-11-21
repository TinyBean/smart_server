import asyncio
import hashlib
import json
import os
import uuid

import httpx
import paho.mqtt.client as paho
import pytest
from conftest import FakeTTS, event
from test_ha import MockHA, until
from websockets.asyncio.server import serve

from app.config import Connections
from app.ha import HAClient
from app.mqtt import MQTT
from app.reminder import ReminderManager
from app.runtime import Runtime
from app.web import create_apps


@pytest.mark.integration
async def test_mosquitto_ha_esp32_round_trip(tmp_path, monkeypatch):
    host = os.environ.get("FRONTDOOR_MQTT_HOST")
    if not host:
        pytest.skip("set FRONTDOOR_MQTT_HOST to run against real Mosquitto")
    port = int(os.environ.get("FRONTDOOR_MQTT_PORT", "18884"))
    identity = uuid.uuid4().hex[:12]
    prefix = f"tests/frontdoor/{identity}"
    loop = asyncio.get_running_loop()
    messages = asyncio.Queue()
    connected = asyncio.Event()
    probe = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"esp-sim-{identity}")

    def on_connect(client, _userdata, _flags, _reason, _properties):
        client.subscribe(prefix+"/#", qos=1)
        client.subscribe("homeassistant/#", qos=1)

    probe.on_connect = on_connect
    probe.on_subscribe = lambda *_: loop.call_soon_threadsafe(connected.set)
    probe.on_message = lambda _c, _u, message: loop.call_soon_threadsafe(
        messages.put_nowait, (message.topic, bytes(message.payload), message.retain),
    )
    await asyncio.to_thread(probe.connect, host, port, 30)
    probe.loop_start()
    await asyncio.wait_for(connected.wait(), 5)
    mock = MockHA()
    runtime = Runtime(tmp_path, dev=True)
    config = Connections(mqtt_host=host, mqtt_port=port, mqtt_prefix=prefix, device_id=identity,
                         public_base_url="http://device-download")
    mqtt = MQTT(config)
    tts = FakeTTS(runtime.tts.cache)
    tasks = []
    try:
        async with serve(mock.handler, "127.0.0.1", 0) as server:
            monkeypatch.setenv("FRONTDOOR_HA_WS", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            monkeypatch.setenv("FRONTDOOR_HA_TOKEN", "test-token")
            ha = HAClient(config.todo_entity, lambda: manager.request_sync())
            manager = ReminderManager(runtime.store, ha, mqtt, tts, config)
            mqtt.on_message, mqtt.on_connected = manager.message, manager.mqtt_connected
            tasks = [asyncio.create_task(worker()) for worker in (mqtt.run, mqtt.dispatch, ha.run, manager.run)]
            await until(lambda: mqtt.connected and ha.connected)
            probe.publish(prefix+"/device/availability", "online", qos=1, retain=True)

            async def receive_command(command):
                async with asyncio.timeout(15):
                    while True:
                        topic, body, retained = await messages.get()
                        if topic == prefix+"/device/command":
                            payload = json.loads(body)
                            if payload["command"] == command:
                                assert retained is False
                                return payload

            cache = await receive_command("cache_audio")
            media, _ = create_apps(runtime)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=media), base_url="http://device-download") as http:
                audio = await http.get(cache["url"])
                assert audio.status_code == 200
                assert hashlib.sha256(audio.content).hexdigest() == cache["sha256"]
            probe.publish(prefix+"/device/audio_status", json.dumps({
                "command_id": cache["command_id"], "version": cache["version"],
                "audio_id": "departure", "status": "ready",
            }), qos=1)
            await until(lambda: manager.ready)
            eid = event(runtime.store)
            mqtt.publish("vision/event", {"event_type": "exit", "event_id": eid})
            probe.publish(prefix+"/reminder/command", json.dumps({"command": "play_departure", "event_id": eid}), qos=1)
            playback = await receive_command("play_cached")
            finished = {"command_id": playback["command_id"], "version": playback["version"],
                        "audio_id": "departure", "status": "finished"}
            probe.publish(prefix+"/device/audio_status", json.dumps(finished), qos=1)
            await receive_command("ack_audio_status")
            await until(lambda: mock.items[0]["status"] == "completed")
            probe.publish(prefix+"/device/audio_status", json.dumps(finished), qos=1)
            await receive_command("ack_audio_status")
            assert len([call for call in mock.calls if call.get("service") == "update_item"]) == 1
            assert runtime.store.playbacks()[0]["state"] == "complete"
            # Discovery is retained; events are not retained for a newly joined observer.
            retained_probe = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"observer-{identity}")
            retained_messages = []
            retained_probe.on_message = lambda _c, _u, msg: retained_messages.append((msg.topic, msg.retain))
            await asyncio.to_thread(retained_probe.connect, host, port, 30)
            retained_probe.loop_start()
            retained_probe.subscribe(prefix+"/vision/#", qos=1)
            retained_probe.subscribe(f"homeassistant/event/front_door_ai_{identity}/activity/config", qos=1)
            await until(lambda: len(retained_messages) >= 2)
            assert all(not topic.endswith("vision/event") for topic, _ in retained_messages)
            assert any(topic.endswith("/config") and retained for topic, retained in retained_messages)
            retained_probe.disconnect()
            await asyncio.to_thread(retained_probe.loop_stop)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Remove only this test's retained topics; shared brokers remain untouched otherwise.
        for suffix in ("device/availability", "vision/availability", "reminder/status"):
            probe.publish(prefix+"/"+suffix, b"", qos=1, retain=True)
        for component, key in (("event", "activity"), ("sensor", "input_fps"), ("sensor", "persons"),
                               ("sensor", "reminder_count"), ("binary_sensor", "reminder_ready")):
            probe.publish(f"homeassistant/{component}/front_door_ai_{identity}/{key}/config", b"", qos=1, retain=True)
        probe.disconnect()
        await asyncio.to_thread(probe.loop_stop)
        await runtime.close()
