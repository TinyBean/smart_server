from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .config import DepartureSettings
from .qwen_tts import QwenTTSError
from .tts import text_for

LOG = logging.getLogger(__name__)


class ReminderManager:
    settings_model = DepartureSettings
    settings_key = "departure_settings"
    audio_id = "departure"
    event_type = "exit"
    play_command = "play_departure"
    fallback_sound = "goodbye"

    def __init__(self, store, ha, mqtt, tts, config):
        self.store, self.ha, self.mqtt, self.tts, self.config = store, ha, mqtt, tts, config
        self.items = []
        self.settings = self.settings_model.model_validate(self.store.get(self.settings_key, {}))
        self.text = self.settings.text
        self.audio = None
        self.device_online = False
        self.device_version = None
        self.cache_command = None
        self.cache_sent_at = 0
        self.last_sync = 0
        self.revision = 0
        self.invalidated = True
        self.wake = asyncio.Event()
        self.force = False
        self.error = None
        self._sync_lock = asyncio.Lock()
        if not hasattr(tts, "synthesis_lock"):
            tts.synthesis_lock = asyncio.Lock()

    @property
    def enabled(self):
        return self.settings.enabled

    @property
    def has_content(self):
        return self.enabled and bool(self.text)

    def configure(self, value):
        self.store.put(self.settings_key, value.model_dump())
        self.settings, self.text = value, value.text
        self.items = []
        self.audio = self.device_version = self.cache_command = None
        self.error = None
        self.request_sync()

    async def prepare_audio(self, text, force=False):
        async with self.tts.synthesis_lock:
            return await self.tts.prepare(text, force=force)

    def request_sync(self, force=False):
        self.revision += 1
        self.invalidated = True
        self.force = self.force or force
        self.wake.set()

    def mqtt_connected(self):
        self.device_online = False
        self.device_version = None
        self.cache_command = None
        self.request_sync()

    def sync_clock(self):
        # Same ordered, nonretained MQTT connection as executable commands.
        # The ESP advances this local HA time with its monotonic clock.
        return self.mqtt.publish("device/command", {
            "protocol_version": 1, "command": "sync_time", "unix_time": time.time(),
        })

    @property
    def ready(self):
        todos_fresh = not self.settings.include_todos or (self.ha.connected and time.time()-self.last_sync <= 20)
        return bool(self.enabled and self.audio and self.device_online and self.mqtt.connected and todos_fresh
                    and self.device_version == self.audio["version"] and not self.invalidated
                    )

    def status(self):
        latest = self.store.latest_playback(self.audio_id)
        return {
            "settings": self.settings.model_dump(),
            "reminder_count": len(self.items), "items": self.items, "text": self.text,
            "tts_provider": self.config.tts_provider,
            "tts_ready": self.audio is not None, "tts_cache_version": self.audio["version"] if self.audio else None,
            "esp32_cache_version": self.device_version, "esp32_cached": self.ready,
            "last_tts_ms": round(self.tts.last_ms, 1), "last_sync": self.last_sync,
            "last_reminder_playback": ({"command_id": latest["command_id"], "status": latest["state"],
                                        "time": latest["created"]} if latest else None),
            "error": self.error or getattr(self.tts, "configuration_error", None),
        }

    def publish_status(self):
        # Avoid broadcasting todo text in retained MQTT telemetry.
        value = self.status()
        value.pop("items")
        value.pop("text")
        value.pop("settings", None)
        self.mqtt.publish("reminder/status" if self.audio_id == "departure" else "arrival/status", value, retain=True)

    async def sync(self):
        async with self._sync_lock:
            await self._sync()

    async def _sync(self):
        revision = self.revision
        settings = self.settings
        if not self.enabled:
            self.items = []
            self.audio = self.device_version = self.cache_command = None
            self.invalidated, self.error = False, None
            return
        all_items = await self.ha.items() if settings.include_todos else []
        if revision != self.revision:
            self.wake.set()
            return
        items = [item for item in all_items if item["status"] == "needs_action"]
        if len({item["uid"] for item in items}) != len(items):
            raise ValueError("duplicate todo UID")
        if items != self.items:
            LOG.info("REMINDER_LIST_CHANGED count=%s", len(items))
            self.invalidated = True
        old_version = self.audio["version"] if self.audio else None
        old_sha = self.audio["sha256"] if self.audio else None
        try:
            text = text_for(items, settings.text)
            if len(items) > 100:
                raise ValueError("too many active reminders (maximum 100)")
            # While synthesis runs, old audio is never advertised as current.
            if text != self.text or self.force:
                self.audio = self.device_version = self.cache_command = None
                self.invalidated = True
            self.items, self.text = items, text
            force, self.force = self.force, False
            audio = await self.prepare_audio(text, force=force)
        except Exception:
            self.audio = self.device_version = self.cache_command = None
            raise
        if revision != self.revision:
            self.wake.set()
            return
        self.audio = audio
        self.last_sync = time.time()
        self.invalidated = False
        self.error = None
        if old_version != (audio["version"] if audio else None) or old_sha != (audio["sha256"] if audio else None):
            self.device_version = None
            self.cache_command = None
        self.store.put("reminder_state", {
            "desired_version": audio["version"] if audio else None,
            "items": items, "last_sync": self.last_sync,
        })
        self.ensure_cache()

    def ensure_cache(self):
        if not self.audio or self.invalidated or not self.device_online or not self.mqtt.connected:
            return
        if self.device_version == self.audio["version"]:
            return
        if not self.config.public_base_url:
            self.error = "请配置 ESP32 可访问的 public_base_url"
            return
        if self.cache_command and time.time()-self.cache_sent_at < 30:
            return
        if not self.sync_clock():
            return
        key = str(uuid.uuid4())
        if self.mqtt.publish("device/command", {
            "protocol_version": 1, "command": "cache_audio", "command_id": key,
            "audio_id": self.audio_id, "version": self.audio["version"],
            "url": f"{self.config.public_base_url}/audio/{self.audio['version']}.wav",
            "sha256": self.audio["sha256"], "expires_at": time.time()+120,
        }):
            self.cache_command, self.cache_sent_at = key, time.time()
            self.store.put("cache:"+key, {"audio_id": self.audio_id, "created": time.time(), **self.audio})

    async def message(self, suffix, message):
        if suffix == "device/availability":
            self.device_online = message == "online"
            self.device_version = None
            self.cache_command = None
            if self.device_online:
                self.sync_clock()
            self.ensure_cache()
        elif suffix == "reminder/command" and isinstance(message, dict):
            if message.get("command") == "sync":
                self.request_sync()
            elif message.get("command") == self.play_command:
                self.play(message.get("event_id"))
        elif suffix == "device/audio_status" and isinstance(message, dict):
            self.audio_status(message)
        self.publish_status()

    def play(self, event_id):
        if not isinstance(event_id, str) or len(event_id) > 64:
            return "invalid_event"
        event = self.store.event(event_id)
        now = time.time()
        if not event or event["event_type"] != self.event_type or not 0 <= now-event["created"] <= 10:
            return "expired_event"
        if self.store.seen_event(event_id):
            return "duplicate"
        if not self.enabled:
            return "disabled"
        self.store.expire_playbacks(now)
        busy = self.store.playback_busy(now)
        key = str(uuid.uuid4())
        if busy or not self.mqtt.connected or not self.device_online:
            self.store.create_playback(key, event_id, "", [], now, state="skipped", audio_id=self.audio_id)
            return "busy_or_offline"
        dynamic = self.ready and self.has_content
        # If an old playback timed out, its delayed ACK must never consume a new batch.
        for row in self.store.playbacks():
            if row["state"] == "timed_out":
                self.store.update_playback(row["command_id"], "superseded")
        version = self.audio["version"] if dynamic else ""
        duration = self.audio["duration"] if dynamic else 15
        self.store.create_playback(key, event_id, version, self.items if dynamic else [], now+duration+(30 if dynamic else 10),
                                   state="requested" if dynamic else "fallback", audio_id=self.audio_id)
        payload = {"protocol_version": 1, "command_id": key, "event_id": event_id, "expires_at": now+10}
        if dynamic:
            payload.update(command="play_cached", audio_id=self.audio_id, version=version)
        else:
            payload.update(command="play", sound=self.fallback_sound)
        if not self.sync_clock() or not self.mqtt.publish("device/command", payload):
            self.store.update_playback(key, "failed")
            return "publish_failed"
        LOG.info("PLAY_REQUESTED command_id=%s dynamic=%s", key, dynamic)
        return "requested" if dynamic else "fallback"

    def receipt(self, key, version):
        self.mqtt.publish("device/command", {
            "protocol_version": 1, "command": "ack_audio_status", "command_id": key,
            "audio_id": self.audio_id, "version": version,
        })

    def audio_status(self, message):
        key, version, status = (message.get(k) for k in ("command_id", "version", "status"))
        if not isinstance(key, str) or not isinstance(version, str) or message.get("audio_id") != self.audio_id:
            return
        self.store.expire_playbacks(time.time())
        if key == self.cache_command and self.audio and version == self.audio["version"]:
            if status == "ready":
                # Device can prove liveness with a response to this session's cache request.
                self.device_online = True
                self.device_version = version
                self.store.put(self.audio_id+"_device_cache", {"version": version, "command_id": key, "status": "ready"})
                self.receipt(key, version)
                LOG.info("DEVICE_CACHE_READY version=%s", version[:12])
            elif status == "error":
                self.device_version = None
                self.error = "ESP32 音频缓存失败，将自动重试"
                self.store.put(self.audio_id+"_device_cache", {"version": version, "command_id": key, "status": "error"})
                self.receipt(key, version)
            return
        cached = self.store.get("cache:"+key)
        if cached and cached["audio_id"] == self.audio_id and cached["version"] == version:
            if status in {"ready", "error"}:
                self.receipt(key, version)
            return
        row = self.store.playback(key)
        if not row or row["audio_id"] != self.audio_id or row["version"] != version or not version:
            return
        if row["state"] in {"complete", "failed", "superseded"}:
            if status in {"finished", "error"}:
                self.receipt(key, version)
            return
        if status == "playing" and row["state"] == "requested":
            self.store.update_playback(key, "playing")
        elif status == "finished" and row["state"] in {"requested", "playing", "timed_out", "finished", "completing"}:
            # The durable acknowledgement is recorded BEFORE telling the device it may forget it.
            if row["state"] not in {"finished", "completing"}:
                self.store.update_playback(key, "finished")
            self.receipt(key, version)
            self.wake.set()
        elif status == "error" and row["state"] in {"requested", "playing", "timed_out"}:
            self.store.update_playback(key, "failed")
            self.receipt(key, version)
            self.error = "ESP32 播放失败，提醒已保留"

    async def reconcile(self):
        for row in self.store.playbacks():
            if row["audio_id"] != self.audio_id or row["state"] not in {"finished", "completing"}:
                continue
            key = row["command_id"]
            completed = row["completed"]
            self.store.update_playback(key, "completing")
            for item in row["items"]:
                if item["uid"] in completed:
                    continue
                # Re-read before each completion; never match by display text.
                current = {i["uid"]: i for i in await self.ha.items()}
                candidate = current.get(item["uid"])
                if candidate and candidate["status"] == "needs_action" and candidate["text"] == item["text"]:
                    await self.ha.complete(item["uid"])
                completed.append(item["uid"])
                self.store.update_playback(key, "completing", completed)
            self.store.update_playback(key, "complete", completed)
            LOG.info("REMINDERS_COMPLETED command_id=%s count=%s", key, len(completed))
            self.request_sync()

    async def run(self):
        while True:
            self.wake.clear()
            self.store.expire_playbacks(time.time())
            if self.device_online and self.mqtt.connected:
                self.sync_clock()
            try:
                # Completion is independent of TTS availability.
                await self.reconcile()
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.invalidated = True
                self.error = str(exc) if isinstance(exc, QwenTTSError) else f"提醒同步/合成不可用 ({type(exc).__name__})"
                LOG.warning("REMINDER_RETRY type=%s", type(exc).__name__)
            self.publish_status()
            try:
                await asyncio.wait_for(self.wake.wait(), 10)
            except TimeoutError:
                pass
