from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path

import numpy as np

from .arrival import ArrivalManager
from .camera import Camera, LatestFrame
from .config import atomic_json, load_config
from .door import SIZE, DoorReferences, region_mask
from .ha import HAClient
from .mqtt import MQTT
from .qwen_tts import QwenTTS
from .reminder import ReminderManager
from .store import Store
from .tts import PiperTTS
from .vision import Vision

LOG = logging.getLogger(__name__)


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = deque(maxlen=300)

    def emit(self, record):
        # Own log messages use identifiers/counters, never tokens or reminder text.
        if record.name.startswith("app"):
            self.lines.append({"time": record.created, "level": record.levelname, "message": record.getMessage()})


class Runtime:
    def __init__(self, data_dir: Path, dev=False):
        self.data_dir, self.dev = data_dir, dev
        data_dir.mkdir(parents=True, exist_ok=True)
        self.config, self.settings = load_config(data_dir)
        self.store = Store(data_dir / "state.sqlite3")
        self.frames = LatestFrame()
        self.camera = Camera(self.config.camera_url, self.frames)
        self.mqtt = MQTT(self.config)
        self.ha = HAClient(self.config.todo_entity, lambda: self.reminders.request_sync())
        if self.config.tts_provider == "qwen":
            self.tts = QwenTTS(data_dir / "reminder-cache",
                              api_key=self.config.qwen_api_key.get_secret_value() or os.getenv("DASHSCOPE_API_KEY", ""),
                              url=self.config.qwen_tts_url, model=self.config.qwen_tts_model,
                              voice=self.config.qwen_tts_voice)
        else:
            self.tts = PiperTTS(self.config.piper_model, data_dir / "reminder-cache")
        self.reminders = ReminderManager(self.store, self.ha, self.mqtt, self.tts, self.config)
        self.arrival = ArrivalManager(self.store, self.ha, self.mqtt, self.tts, self.config)
        self.mqtt.on_message = self.message
        self.mqtt.on_connected = self.mqtt_connected
        self.door_references = DoorReferences.load(data_dir / "door-reference.npz")
        self.calibration_lock = asyncio.Lock()
        self.vision = Vision(self.frames, self.settings, self.config.detector_model, self.event, self.door_references)
        self.log_buffer = LogBuffer()
        logging.getLogger().addHandler(self.log_buffer)
        self.tasks = []
        self.errors = {}
        self.event_count = 0
        self.started = time.monotonic()
        self.event_dir = data_dir / "events"
        self.event_dir.mkdir(exist_ok=True)

    async def message(self, suffix, message):
        await self.reminders.message(suffix, message)
        await self.arrival.message(suffix, message)

    def mqtt_connected(self):
        self.reminders.mqtt_connected()
        self.arrival.mqtt_connected()

    def arrival_detection_status(self):
        return {"mode": self.settings.entry_detection_mode,
                "calibration": self.door_references.status(self.settings), **self.vision.door_status}

    def status(self):
        frame = self.frames.fresh()
        if self.errors:
            state = "ERROR"
        elif frame is None:
            state = "CAMERA_CONNECTING"
        elif not self.vision.ready or not self.mqtt.connected or not self.ha.connected:
            state = "DEGRADED"
        else:
            state = "RUNNING"
        return {"state": state, "camera": frame is not None, "mqtt": self.mqtt.connected,
                "ha": self.ha.connected, "ai_ready": self.vision.ready, "ai_error": self.vision.error,
                "calibrated": self.settings.calibrated, "input_fps": self.camera.fps,
                "departure_path_ready": bool(self.settings.departure_path),
                "detection_fps": self.vision.fps, "inference_ms": round(self.vision.inference_ms, 1),
                "inference_stages_ms": self.vision.stage_ms,
                "inference_threads": self.vision.inference_threads,
                "decode_ms": round(self.camera.decode_ms, 1), "persons": len(self.vision.tracks) if frame else 0,
                "last_frame_age_ms": round((time.monotonic()-frame.timestamp)*1000) if frame else None,
                "dropped_frames": self.vision.dropped, "bad_frames": self.camera.bad_frames,
                "camera_reconnects": self.camera.reconnects, "mqtt_reconnects": self.mqtt.reconnects,
                "mqtt_dropped": self.mqtt.dropped, "event_count": self.event_count,
                "uptime_seconds": round(time.monotonic()-self.started),
                "memory_rss_mb": self.memory_rss_mb(), "errors": self.errors,
                "arrival_detection": self.arrival_detection_status()}

    @staticmethod
    def memory_rss_mb():
        try:
            # Linux / HA OS: current resident memory, not the all-time high-water mark.
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            return round(pages * os.sysconf("SC_PAGE_SIZE") / 1024**2, 1)
        except (OSError, AttributeError, ValueError, IndexError):
            return None

    async def event(self, event, frame, overlay):
        # Persist event identity before publishing so HA can immediately refer to it.
        event["snapshot"] = f"/api/events/{event['event_id']}/snapshot"
        self.store.add_event(event)
        self.event_count += 1
        self.mqtt.publish("vision/event", {k: v for k, v in event.items() if k != "created"})
        try:
            await asyncio.to_thread(self.write_snapshots, event["event_id"], frame.jpeg, overlay)
        except OSError:
            LOG.error("SNAPSHOT_WRITE_FAILED event_id=%s", event["event_id"])
        LOG.info("DOOR_EVENT type=%s event_id=%s", event["event_type"], event["event_id"])

    def write_snapshots(self, event_id, raw, overlay):
        for suffix, content in (("raw", raw), ("overlay", overlay)):
            target = self.event_dir / f"{event_id}-{suffix}.jpg"
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            os.replace(temporary, target)

    async def configure(self, settings):
        async with self.calibration_lock:
            if settings.entry_detection_mode == "door_reveal" and not self.door_references.matches(settings):
                raise ValueError("请先保存门体区与人员出现区，并完成开、关门参考采集")
            atomic_json(self.data_dir / "config.json", settings.model_dump())
            self.settings = settings
            self.vision.configure(settings)

    async def capture_door_reference(self, state):
        async with self.calibration_lock:
            settings = self.settings
            if not settings.door_zone or not settings.arrival_zone:
                raise ValueError("请先绘制并保存门体区与人员出现区")
            images, generation, shape, sequence = [], self.frames.generation, None, 0
            mask = region_mask(settings.door_zone)
            async with asyncio.timeout(5):
                while len(images) < 5:
                    frame = await self.frames.next(sequence, timeout=2)
                    sequence = frame.sequence
                    if frame.generation != generation or time.monotonic()-frame.timestamp > .8:
                        raise ValueError("画面已中断，请恢复后重新采集")
                    if not self.vision.ready or time.monotonic()-self.vision.boxes_at > .8:
                        raise ValueError("请等待人体检测就绪后采集")
                    h, w = frame.image.shape[:2]
                    for x1, y1, x2, y2 in self.vision.person_boxes:
                        x1, x2 = max(0, int(x1/w*SIZE[0])-3), min(SIZE[0], int(x2/w*SIZE[0])+4)
                        y1, y2 = max(0, int(y1/h*SIZE[1])-3), min(SIZE[1], int(y2/h*SIZE[1])+4)
                        if np.any(mask[y1:y2, x1:x2]):
                            raise ValueError("门体观察区被人遮挡，请让开后重新采集")
                    images.append(frame.image.copy())
                    shape = frame.image.shape
                    await asyncio.sleep(.2)
            references = await asyncio.to_thread(self.door_references.capture, state, images, settings, shape, False)
            if self.frames.generation != generation:
                raise ValueError("采集期间摄像头重连，请重新采集")
            references.save()
            self.door_references = self.vision.door_references = references
            self.settings = settings.model_copy()
            self.vision.configure(self.settings)
            return self.arrival_detection_status()

    async def supervise(self, name, worker):
        while True:
            try:
                self.errors.pop(name, None)
                await worker()
                raise RuntimeError("worker unexpectedly stopped")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[name] = type(exc).__name__
                LOG.error("WORKER_RESTART name=%s type=%s", name, type(exc).__name__)
                await asyncio.sleep(5)

    async def housekeeping(self):
        while True:
            self.mqtt.publish("vision/status", {"online": True, **self.status()}, retain=True)
            self.reminders.publish_status()
            self.arrival.publish_status()
            for event_id in self.store.prune_events(self.settings.snapshot_retention_days, self.settings.snapshot_max_events):
                for suffix in ("raw", "overlay"):
                    (self.event_dir / f"{event_id}-{suffix}.jpg").unlink(missing_ok=True)
            self.store.expire_playbacks(time.time())
            await asyncio.to_thread(self.cleanup_files, self.protected_audio())
            await asyncio.sleep(15)

    def protected_audio(self):
        protected = {row["version"] for row in self.store.playbacks()
                     if row["state"] in {"requested", "playing", "finished", "completing", "timed_out"}}
        if self.reminders.audio:
            protected.add(self.reminders.audio["version"])
        if self.arrival.audio:
            protected.add(self.arrival.audio["version"])
        return protected

    def cleanup_files(self, protected):
        for path in self.tts.cache.glob("*.wav"):
            if path.stem not in protected and time.time()-path.stat().st_mtime > 7*86400:
                path.unlink(missing_ok=True)

    async def start(self):
        for name, worker in (("camera", self.camera.run), ("vision", self.vision.run),
                             ("mqtt", self.mqtt.run), ("mqtt_dispatch", self.mqtt.dispatch),
                             ("ha", self.ha.run), ("reminders", self.reminders.run),
                             ("arrival", self.arrival.run),
                             ("monitoring", self.housekeeping)):
            self.tasks.append(asyncio.create_task(self.supervise(name, worker), name=name))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        logging.getLogger().removeHandler(self.log_buffer)
