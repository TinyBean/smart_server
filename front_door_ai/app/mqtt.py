from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time

import paho.mqtt.client as paho

LOG = logging.getLogger(__name__)


class MQTT:
    """Fresh client per connection: never replay a previous session's event queue."""

    def __init__(self, config):
        self.config = config
        self.connected = False
        self.client = None
        self.queue = asyncio.Queue(maxsize=128)
        self.reconnects = 0
        self.dropped = 0
        self.on_message = None
        self.on_connected = None

    def topic(self, suffix):
        return f"{self.config.mqtt_prefix}/{suffix}"

    def publish(self, suffix, payload, retain=False, full_topic=False):
        if not self.connected or self.client is None:
            return False
        encoded = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        result = self.client.publish(suffix if full_topic else self.topic(suffix), encoded, qos=1, retain=retain)
        return result.rc == paho.MQTT_ERR_SUCCESS

    def enqueue(self, topic, payload, retained):
        try:
            self.queue.put_nowait((topic, payload, retained))
        except asyncio.QueueFull:
            self.dropped += 1

    async def dispatch(self):
        while True:
            topic, payload, retained = await self.queue.get()
            try:
                if len(payload) > 16384:
                    continue
                if topic == "homeassistant/status":
                    if payload == b"online":
                        self.discovery()
                    continue
                suffix = topic.removeprefix(self.config.mqtt_prefix + "/")
                if suffix in {"reminder/command", "device/audio_status"} and retained:
                    continue
                value = payload.decode("utf-8") if suffix == "device/availability" else json.loads(payload)
                if self.on_message:
                    await self.on_message(suffix, value)
            except Exception as exc:
                LOG.warning("MQTT_MESSAGE_REJECTED type=%s", type(exc).__name__)
            finally:
                self.queue.task_done()

    def discovery(self):
        cfg = self.config
        root = f"front_door_ai_{cfg.device_id}"
        device = {"identifiers": [root], "name": "Front Door AI", "manufacturer": "Front Door AI", "model": "V1.1"}
        common = {"device": device, "availability_topic": self.topic("vision/availability"),
                  "payload_available": "online", "payload_not_available": "offline"}
        entities = [
            ("event", "activity", {"name": "Activity", "state_topic": self.topic("vision/event"), "event_types": ["enter", "exit"]}),
            ("sensor", "input_fps", {"name": "Input FPS", "state_topic": self.topic("vision/status"),
                                      "value_template": "{{ value_json.input_fps }}", "unit_of_measurement": "fps"}),
            ("sensor", "persons", {"name": "Persons", "state_topic": self.topic("vision/status"), "value_template": "{{ value_json.persons }}"}),
            ("sensor", "reminder_count", {"name": "Reminder count", "state_topic": self.topic("reminder/status"), "value_template": "{{ value_json.reminder_count }}"}),
            ("binary_sensor", "reminder_ready", {"name": "Reminder ready", "state_topic": self.topic("reminder/status"),
                "value_template": "{{ 'ON' if value_json.esp32_cached else 'OFF' }}"}),
        ]
        for component, key, fields in entities:
            self.publish(f"homeassistant/{component}/{root}/{key}/config",
                         {**common, "unique_id": f"{root}_{key}", **fields}, retain=True, full_topic=True)

    def make_client(self, loop, ended):
        # Function scope gives every connection its own callback closures.
        client = paho.Client(paho.CallbackAPIVersion.VERSION2,
                                 client_id=f"front-door-ai-{self.config.device_id}",
                                 clean_session=True, reconnect_on_failure=False)
        client.connect_timeout = 5
        client.max_queued_messages_set(32)
        client.will_set(self.topic("vision/availability"), "offline", qos=1, retain=True)
        if self.config.mqtt_username:
            client.username_pw_set(self.config.mqtt_username, self.config.mqtt_password)
        if self.config.mqtt_tls:
            client.tls_set_context(ssl.create_default_context())

        def connected(_client, _userdata, _flags, reason, _properties):
            if reason.is_failure:
                loop.call_soon_threadsafe(ended.set)
                return
            for suffix in ("reminder/command", "device/audio_status", "device/availability"):
                _client.subscribe(self.topic(suffix), qos=1)
            _client.subscribe("homeassistant/status", qos=1)
            loop.call_soon_threadsafe(online)

        def online():
            if self.client is not client:
                return
            self.connected = True
            self.discovery()
            self.publish("vision/availability", "online", retain=True)
            if self.on_connected:
                self.on_connected()

        def disconnected(*_args):
            loop.call_soon_threadsafe(offline)

        def offline():
            if self.client is client:
                self.connected = False
            ended.set()

        client.on_connect = connected
        client.on_disconnect = disconnected
        client.on_message = lambda _c, _u, msg: loop.call_soon_threadsafe(
            self.enqueue, msg.topic, bytes(msg.payload), msg.retain,
        )
        return client

    async def run(self):
        loop = asyncio.get_running_loop()
        delay = 1
        while True:
            ended = asyncio.Event()
            client = self.make_client(loop, ended)
            self.client = client
            start = time.monotonic()
            try:
                await asyncio.to_thread(client.connect, self.config.mqtt_host, self.config.mqtt_port, 30)
                client.loop_start()
                await ended.wait()
            except asyncio.CancelledError:
                if self.connected:
                    info = client.publish(self.topic("vision/availability"), "offline", qos=1, retain=True)
                    await asyncio.to_thread(info.wait_for_publish, 2)
                raise
            except Exception as exc:
                LOG.warning("MQTT_RECONNECT type=%s", type(exc).__name__)
            finally:
                self.connected = False
                self.client = None
                client.disconnect()
                await asyncio.to_thread(client.loop_stop)
            self.reconnects += 1
            delay = 1 if time.monotonic()-start > 30 else min(delay*2, 30)
            await asyncio.sleep(delay)
