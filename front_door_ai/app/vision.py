from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import cv2
import numpy as np

from .config import Settings
from .departure import DepartureProgress
from .door import DoorRevealEngine
from .geometry import contains

LOG = logging.getLogger(__name__)


@dataclass
class Detection:
    bbox: tuple[float, float, float, float]
    confidence: float


class PersonDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[Detection]: ...


class OpenVINODetector:
    def __init__(self, model):
        import openvino as ov
        core = ov.Core()
        try:
            available = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            available = os.cpu_count() or 1
        self.threads = max(1, min(4, available))
        self.compiled = core.compile_model(model, "CPU", {
            "PERFORMANCE_HINT": "THROUGHPUT", "INFERENCE_NUM_THREADS": self.threads,
            "NUM_STREAMS": min(2, self.threads),
        })
        self.requests = [self.compiled.create_infer_request() for _ in range(2)]

    def detect(self, image):
        height, width = image.shape[:2]
        # A close, cropped torso can fill the SSD's native view and receive a very
        # weak score. Retain that view for distant people and add a smaller view
        # with context; keep the event/track confidence thresholds unchanged.
        pad_y, pad_x = max(1, height//4), max(1, width//4)
        context = cv2.copyMakeBorder(
            image, pad_y, pad_y, pad_x, pad_x, cv2.BORDER_CONSTANT, value=(114, 114, 114),
        )
        result, context_detections = self._infer_views((image, context))
        for detection in context_detections:
            coords = np.array(detection.bbox) - [pad_x, pad_y, pad_x, pad_y]
            coords = np.clip(coords, [0, 0, 0, 0], [width, height, width, height])
            if coords[2] > coords[0] and coords[3] > coords[1]:
                result.append(Detection(tuple(float(v) for v in coords), detection.confidence))
        if not result:
            return []
        # Both views describe the same image. Deduplicate before ByteTrack so one
        # person cannot acquire two identities and publish two crossing events.
        boxes = [[d.bbox[0], d.bbox[1], d.bbox[2]-d.bbox[0], d.bbox[3]-d.bbox[1]] for d in result]
        kept = cv2.dnn.NMSBoxes(boxes, [d.confidence for d in result], .1, .5)
        return [result[int(index)] for index in np.asarray(kept).reshape(-1)]

    def _infer_views(self, images):
        # Each view gets its own request/output buffer. Both share the available
        # CPU budget, capped at four. Finish both before the next camera frame.
        tensors = [np.ascontiguousarray(
            cv2.resize(image, (544, 320)).transpose(2, 0, 1)[None], dtype=np.float32,
        ) for image in images]
        started = []
        try:
            for request, tensor in zip(self.requests, tensors, strict=True):
                request.start_async([tensor], share_inputs=True)
                started.append(request)
        finally:
            failure = None
            for request in started:
                try:
                    request.wait()
                except Exception as exc:
                    failure = failure or exc
            if failure is not None:
                raise failure
        return [self._decode(request.get_output_tensor().data, image.shape)
                for request, image in zip(self.requests, images, strict=True)]

    @staticmethod
    def _decode(output, shape):
        height, width = shape[:2]
        result = []
        for _, label, confidence, x1, y1, x2, y2 in output.reshape(-1, 7):
            if label != 1 or confidence < 0.1 or not np.isfinite([confidence, x1, y1, x2, y2]).all():
                continue
            coords = np.clip([x1, y1, x2, y2], 0, 1) * [width, height, width, height]
            if coords[2] > coords[0] and coords[3] > coords[1]:
                result.append(Detection(tuple(float(v) for v in coords), float(confidence)))
        return result


class ByteTracker:
    def __init__(self, settings):
        from trackers import ByteTrackTracker
        self.tracker = ByteTrackTracker(
            frame_rate=settings.detection_fps, lost_track_buffer=30,
            track_activation_threshold=settings.confidence_threshold,
            high_conf_det_threshold=settings.confidence_threshold,
            minimum_consecutive_frames=2,
        )

    def update(self, detections, timestamp):
        import supervision as sv
        data = sv.Detections(
            xyxy=np.array([d.bbox for d in detections], dtype=np.float32).reshape(-1, 4),
            confidence=np.array([d.confidence for d in detections], dtype=np.float32),
            class_id=np.zeros(len(detections), dtype=int),
        )
        # The pinned tracker supports timestamps for irregular detection intervals.
        tracked = self.tracker.update(data, timestamp=timestamp)
        return [
            {"track_id": int(tid), "bbox": box.tolist(), "confidence": float(conf)}
            for box, conf, tid in zip(tracked.xyxy, tracked.confidence, tracked.tracker_id, strict=False)
            if tid >= 0
        ]


@dataclass
class Trajectory:
    born: float
    last_seen: float
    origin: str | None = None
    origin_point: tuple | None = None
    transitioned: bool = False
    arrival_at: float | None = None
    cooldown_until: float = 0
    history: deque = field(default_factory=lambda: deque(maxlen=45))
    departure: DepartureProgress = field(default_factory=DepartureProgress)


class DirectionEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tracks: dict[int, Trajectory] = {}

    def zone(self, point):
        # Shared boundaries belong to the transition zone.
        for name in ("transition", "outside", "inside"):
            polygon = getattr(self.settings, f"{name}_zone")
            if polygon and contains(point, polygon):
                return name
        return None

    def update(self, tracks, shape, now, camera_offset=(0, 0)):
        height, width = shape[:2]
        self.tracks = {key: t for key, t in self.tracks.items() if now - t.last_seen <= 1}
        events = []
        for track in tracks:
            key = track["track_id"]
            state = self.tracks.setdefault(key, Trajectory(now, now))
            # A gap invalidates a partially observed crossing even if ByteTrack reuses the ID.
            if now - state.last_seen > max(0.6, 1.5/self.settings.detection_fps):
                state.origin = None
                state.transitioned = False
                state.arrival_at = None
                state.departure.reset()
                state.history.clear()
            state.last_seen = now
            x1, y1, x2, y2 = track["bbox"]
            anchor_y = (y1+y2)/2 if self.settings.tracking_point == "center" else y2
            live_point = ((x1+x2)/2/width, anchor_y/height)
            point = (live_point[0]-camera_offset[0]/width, live_point[1]-camera_offset[1]/height)
            zone = self.zone(point) if self.settings.calibrated else None
            state.history.append((now, live_point))
            while state.history and now - state.history[0][0] > 3:
                state.history.popleft()
            track["point"] = live_point
            track["zone"] = zone
            track["history"] = [p for _, p in state.history]
            direction = self.zone_direction(state, zone, point, track["confidence"], shape, now)
            path_exit = state.departure.update(
                self.settings, point, shape, now, track["confidence"],
                eligible=direction is None and now >= state.cooldown_until,
            )
            if direction or path_exit:
                created = time.time()
                event = {
                    "event_id": str(uuid.uuid4()), "event_type": direction or "exit",
                    "track_id": key, "confidence": track["confidence"], "created": created,
                    "timestamp": datetime.fromtimestamp(created, UTC).isoformat(),
                }
                if path_exit:
                    event["trigger"] = "departure_path"
                    # Consume a concurrent zone crossing so it cannot confirm again later.
                    state.origin = zone if zone in {"inside", "outside"} else None
                    state.origin_point = point
                    state.transitioned = False
                    state.arrival_at = None
                state.departure.reset()
                state.cooldown_until = now + self.settings.event_cooldown
                events.append(event)
        return events

    def zone_direction(self, state, zone, point, confidence, shape, now):
        if zone is None:
            state.origin = None
            state.transitioned = False
            state.arrival_at = None
            return None
        if zone == "transition":
            state.transitioned = state.origin is not None
            state.arrival_at = None
            return None
        direction = None
        if state.origin is not None and zone != state.origin and state.transitioned:
            height, width = shape[:2]
            moved = math.hypot((point[0]-state.origin_point[0])*width,
                               (point[1]-state.origin_point[1])*height) / math.hypot(width, height)
            if state.arrival_at is None:
                state.arrival_at = now
            confirmation_open = now - state.arrival_at <= 1.5 and now >= state.cooldown_until
            if (confirmation_open and now - state.born >= self.settings.minimum_track_age
                    and confidence >= self.settings.confidence_threshold
                    and moved >= self.settings.minimum_movement):
                direction = "enter" if zone == "inside" else "exit"
            elif confirmation_open:
                # Weak destination detections may recover within the confirmation window.
                return None
        # Return to origin cancels a half-crossing; arrival arms the opposite direction.
        if zone != state.origin or state.transitioned:
            state.origin_point = point
        state.origin = zone
        state.origin_point = state.origin_point or point
        state.transitioned = False
        state.arrival_at = None
        return direction


def annotate(frame, tracks, settings, camera_offset=(0, 0)):
    image = frame.image.copy()
    height, width = image.shape[:2]
    colors = {"outside": (80, 180, 255), "transition": (60, 220, 220), "inside": (130, 220, 90),
              "door": (230, 150, 90), "arrival": (210, 120, 230)}
    for name, color in colors.items():
        poly = getattr(settings, f"{name}_zone")
        if poly:
            points = (np.array(poly)*[width, height]+camera_offset).astype(np.int32)
            cv2.polylines(image, [points], True, color, 1)
            cv2.putText(image, name, tuple(points[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
    if settings.departure_path:
        start, end = (np.array(settings.departure_path)*[width, height]+camera_offset).astype(np.int32)
        cv2.arrowedLine(image, tuple(start), tuple(end), (80, 80, 255), 2, tipLength=.18)
        cv2.putText(image, "exit path", (int(end[0]), max(12, int(end[1])-6)),
                    cv2.FONT_HERSHEY_SIMPLEX, .35, (80, 80, 255), 1)
    for track in tracks:
        x1, y1, x2, y2 = [int(v) for v in track["bbox"]]
        cv2.rectangle(image, (x1, y1), (x2, y2), (80, 230, 150), 1)
        cv2.putText(image, f"#{track['track_id']} {track['confidence']:.2f}", (x1, max(12, y1)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (80, 230, 150), 1)
        history = track.get("history", [])
        if "point" in track:
            anchor = tuple(int(v) for v in np.array(track["point"])*[width, height])
            cv2.circle(image, anchor, 3, (60, 220, 220), -1)
            cv2.putText(image, track.get("zone") or "unassigned", (x1, min(height-3, y2)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (60, 220, 220), 1)
        if len(history) > 1:
            cv2.polylines(image, [(np.array(history)*[width, height]).astype(np.int32)], False, (255, 160, 80), 1)
    return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


class Vision:
    def __init__(self, frames, settings, model_path, on_event, door_references=None):
        self.frames, self.settings, self.model_path = frames, settings, model_path
        self.on_event = on_event
        self.tracks = []
        self.overlay = None
        self.overlay_at = 0.0
        self.error = None
        self.ready = False
        self.inference_ms = 0.0
        self.inference_threads = 0
        self.stage_ms = {}
        self.dropped = 0
        self.processed = deque(maxlen=100)
        self.door_references = door_references
        self.door_status = {"observation": "unknown", "door_state": "unknown", "phase": "wait_closed"}
        self.person_boxes = []
        self.boxes_at = 0.0

    def configure(self, settings):
        self.settings = settings

    @property
    def fps(self):
        recent = [t for t in self.processed if time.monotonic()-t < 10]
        return round((len(recent)-1)/(recent[-1]-recent[0]), 2) if len(recent) > 1 else 0

    async def run(self):
        try:
            detector = await asyncio.to_thread(OpenVINODetector, self.model_path)
            self.inference_threads = detector.threads
        except Exception as exc:
            self.error = f"模型加载失败 ({type(exc).__name__})"
            LOG.error("DETECTOR_LOAD_FAILED type=%s", type(exc).__name__)
            await asyncio.Event().wait()
            return
        sequence, generation, previous_settings = 0, -1, None
        tracker = engine = None
        while True:
            try:
                frame = await self.frames.next(sequence, timeout=1)
            except TimeoutError:
                self.tracks = []
                self.overlay = None
                self.door_status = {"observation": "unknown", "door_state": "unknown", "phase": "wait_closed"}
                previous_settings = None
                continue
            started = time.monotonic()
            settings = self.settings
            if generation != frame.generation or previous_settings is not settings:
                tracker = await asyncio.to_thread(ByteTracker, settings)
                engine = DirectionEngine(settings)
                door_engine = DoorRevealEngine(settings)
                if self.door_references:
                    self.door_references.reset_alignment()
                generation, previous_settings = frame.generation, settings
            self.dropped += max(0, frame.sequence-sequence-1)
            sequence = frame.sequence
            try:
                detections = await asyncio.to_thread(detector.detect, frame.image)
                detected_at = time.monotonic()
                tracks = await asyncio.to_thread(tracker.update, detections, frame.timestamp)
                tracked_at = time.monotonic()
                # A reconnect/configuration change during inference invalidates the result.
                if generation != self.frames.generation or settings is not self.settings or time.monotonic()-frame.timestamp > 3:
                    continue
                self.person_boxes = [d.bbox for d in detections]
                self.boxes_at = frame.timestamp
                observation = (self.door_references.observe(frame.image, self.person_boxes, settings, frame.timestamp)
                               if self.door_references else {"observation": "unknown"})
                offset = observation.get("camera_offset_px", (0, 0))
                if "alignment" in observation and "camera_offset_px" not in observation:
                    # Never join crossings across two unverified coordinate systems.
                    engine.tracks.clear()
                    door_engine = DoorRevealEngine(settings)
                    events, arrivals = [], []
                else:
                    events = engine.update(tracks, frame.image.shape, frame.timestamp, offset)
                    arrivals = door_engine.update(tracks, frame.image.shape, frame.timestamp,
                                                  observation["observation"], offset)
                self.door_status = {**observation, **door_engine.status()}
                if settings.entry_detection_mode == "door_reveal":
                    events = [event for event in events if event["event_type"] == "exit"]
                    events.extend(arrivals)
                observed_at = time.monotonic()
                overlay = await asyncio.to_thread(annotate, frame, tracks, settings, offset)
                if generation != self.frames.generation or settings is not self.settings:
                    continue
                self.tracks, self.overlay, self.overlay_at = tracks, overlay, frame.timestamp
                self.inference_ms = (time.monotonic()-started)*1000
                self.stage_ms = {
                    "detector": round((detected_at-started)*1000, 1),
                    "tracker": round((tracked_at-detected_at)*1000, 1),
                    "door": round((observed_at-tracked_at)*1000, 1),
                    "overlay": round((time.monotonic()-observed_at)*1000, 1),
                }
                self.processed.append(time.monotonic())
                self.ready, self.error = True, None
                for event in events:
                    await self.on_event(event, frame, overlay)
            except Exception as exc:
                self.error = f"推理失败 ({type(exc).__name__})"
                self.ready = False
                LOG.error("INFERENCE_FAILED type=%s", type(exc).__name__)
                await asyncio.sleep(1)
            await asyncio.sleep(max(0, 1/settings.detection_fps - (time.monotonic()-started)))
