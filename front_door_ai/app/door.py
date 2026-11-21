"""Calibrated, local door observations and one arrival per observed opening cycle."""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zipfile import BadZipFile

import cv2
import numpy as np

from .alignment import SIZE, BackgroundAlignment
from .geometry import contains


def region_mask(zone):
    mask = np.zeros(SIZE[::-1], np.uint8)
    if zone:
        cv2.fillPoly(mask, [(np.array(zone) * (np.array(SIZE)-1)).astype(np.int32)], 1)
    return mask.astype(bool)


def features(image):
    gray = cv2.cvtColor(cv2.resize(image, SIZE), cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    dx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    dy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    return gray, cv2.magnitude(dx, dy)


def distance(first, second, mask):
    values = []
    for a, b in zip(first, second, strict=True):
        a, b = a[mask], b[mask]
        a, b = a-a.mean(), b-b.mean()
        norm = float(np.linalg.norm(a)*np.linalg.norm(b))
        if norm < 1:
            return 2.0
        values.append(1-float(np.clip(np.dot(a, b)/norm, -1, 1)))
    return .55*values[0] + .45*values[1]


class DoorReferences:
    """Snapshots are immutable once installed in Vision; capture replaces the whole object."""

    def __init__(self, path: Path, zone=(), shape=(), samples=None):
        self.path, self.zone, self.shape = path, list(zone), tuple(shape)
        self.samples = samples or {"closed": [], "open": []}
        self.mask = region_mask(self.zone)
        self.templates = {state: [features(image) for image in images] for state, images in self.samples.items()}
        self.threshold = self.margin = 0.0
        self.error = "请采集关门和开门参考画面"
        self.reset_alignment()
        self._calibrate()

    def reset_alignment(self):
        self.alignment = None
        self.alignment_zone = None

    @classmethod
    def load(cls, path):
        try:
            with np.load(path, allow_pickle=False) as data:
                meta = json.loads(str(data["metadata"]))
                return cls(path, meta["zone"], meta["shape"],
                           {state: list(data[state]) for state in ("closed", "open")})
        except (OSError, ValueError, KeyError, EOFError, TypeError, BadZipFile, cv2.error):
            return cls(path)

    def _calibrate(self):
        if self.mask.sum() < 128 or not all(self.templates.values()):
            return
        if any(float(gray[self.mask].std()) < 2 or float(edges[self.mask].std()) < 1
               for templates in self.templates.values() for gray, edges in templates):
            self.error = "门体观察区缺少可区分的边缘，请包含门框或门板纹理后重新采集"
            return
        separation = min(distance(a, b, self.mask)
                         for a in self.templates["closed"] for b in self.templates["open"])
        if separation < .18:
            self.error = "开关门参考画面过于相似，请调整门体观察区并重新采集"
            return
        self.threshold = min(.55, separation*.42)
        self.margin = max(.04, separation*.18)
        self.error = None

    def matches(self, settings, shape=None):
        return (self.error is None and np.array_equal(self.zone, settings.door_zone)
                and bool(settings.arrival_zone) and (shape is None or tuple(shape[:2]) == self.shape))

    def status(self, settings):
        return {"ready": self.matches(settings), "closed_samples": len(self.samples["closed"]),
                "open_samples": len(self.samples["open"]), "threshold": round(self.threshold, 3),
                "error": self.error if np.array_equal(self.zone, settings.door_zone)
                else "门体区域已改变，请重新采集参考画面"}

    def capture(self, state, images, settings, shape, persist=True):
        same = np.array_equal(self.zone, settings.door_zone) and self.shape == tuple(shape[:2])
        samples = {key: list(self.samples[key]) if same else [] for key in ("closed", "open")}
        # Closed capture starts a new calibration. Open captures may cover several opening angles.
        if state == "closed":
            samples = {"closed": [], "open": []}
        samples[state] = (samples[state] + [cv2.resize(image, SIZE) for image in images])[-12:]
        result = DoorReferences(self.path, settings.door_zone, shape[:2], samples)
        if persist:
            result.save()
        return result

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("wb") as output:
            np.savez_compressed(output, metadata=json.dumps({"zone": self.zone, "shape": self.shape}),
                                **{key: np.asarray(values, dtype=np.uint8) for key, values in self.samples.items()})
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, self.path)

    def observe(self, image, boxes, settings, now=None):
        result = {"observation": "unknown", "closed_score": None, "open_score": None, "visible_fraction": 0.0}
        if not self.matches(settings, image.shape):
            return result
        if self.alignment is None or self.alignment_zone != settings.arrival_zone:
            self.alignment = BackgroundAlignment(self.samples, self.mask | region_mask(settings.arrival_zone))
            self.alignment_zone = list(settings.arrival_zone)
        small = cv2.resize(image, SIZE)
        visible = np.ones(self.mask.shape, np.uint8)
        height, width = image.shape[:2]
        for x1, y1, x2, y2 in boxes:
            # Expand low-confidence human detections too; a person is never evidence of an open door.
            x1, x2 = int(x1/width*SIZE[0])-3, int(x2/width*SIZE[0])+4
            y1, y2 = int(y1/height*SIZE[1])-3, int(y2/height*SIZE[1])+4
            visible[max(0, y1):max(0, y2), max(0, x1):max(0, x2)] = 0
        shift = self.alignment.measure(small, visible, time.monotonic() if now is None else now)
        result["alignment"] = self.alignment.state
        if shift is None:
            return result
        dx, dy = shift
        result["camera_offset_px"] = [dx*width/SIZE[0], dy*height/SIZE[1]]
        if dx or dy:
            transform = np.float32([[1, 0, dx], [0, 1, dy]])
            small = cv2.warpAffine(small, transform, SIZE, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
            visible = cv2.warpAffine(visible, transform, SIZE, flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP)
        mask = self.mask & visible.astype(bool)
        result["visible_fraction"] = round(float(mask.sum()/self.mask.sum()), 3)
        if mask.sum() < 128 or result["visible_fraction"] < .55:
            return result
        current = features(small)
        scores = {state: min(distance(current, template, mask) for template in templates)
                  for state, templates in self.templates.items()}
        result.update({state+"_score": round(score, 3) for state, score in scores.items()})
        best = min(scores, key=scores.get)
        other = "closed" if best == "open" else "open"
        if scores[best] <= self.threshold and scores[other]-scores[best] >= self.margin:
            result["observation"] = best
        return result


@dataclass
class PersonOrigin:
    born: float
    last_seen: float
    bbox: tuple
    eligible: bool
    consecutive: int = 0
    last_valid: float = -100.0


def near_box(first, second, shape):
    h, w = shape[:2]
    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    overlap = max(0, min(ax2, bx2)-max(ax1, bx1))*max(0, min(ay2, by2)-max(ay1, by1))
    union = (ax2-ax1)*(ay2-ay1)+(bx2-bx1)*(by2-by1)-overlap
    shift = math.hypot((ax1+ax2-bx1-bx2)/2, (ay1+ay2-by1-by2)/2)/math.hypot(w, h)
    return (union > 0 and overlap/union >= .15) or shift < .10


class DoorRevealEngine:
    def __init__(self, settings):
        self.settings = settings
        self.people = {}
        self.confirmed = "unknown"
        self.pending = "unknown"
        self.count = 0
        self.armed = False
        self.opening_at = None
        self.cycle = None
        self.fired = False
        self.last_seen = None

    def status(self):
        phase = "wait_closed"
        if self.armed:
            phase = "fired" if self.fired else "wait_person" if self.cycle else "wait_open"
        return {"door_state": self.confirmed, "phase": phase, "door_cycle_id": self.cycle}

    def update(self, tracks, shape, now, observation, camera_offset=(0, 0)):
        gap = max(.6, 1.5/self.settings.detection_fps)
        if self.last_seen is not None and now-self.last_seen > max(1, gap):
            self.__init__(self.settings)
        self.last_seen = now
        self.people = {key: person for key, person in self.people.items() if now-person.last_seen < 10}
        if observation != "closed" and self.armed and self.opening_at is None:
            self.opening_at = now
        if observation == "unknown":
            self.pending, self.count = "unknown", 0
        else:
            self.count = self.count+1 if self.pending == observation else 1
            self.pending = observation
            if self.count >= 3 and observation != self.confirmed:
                self.confirmed = observation
                if observation == "closed":
                    self.armed, self.fired = True, False
                    self.cycle = self.opening_at = None
                    for person in self.people.values():
                        person.eligible = False
                elif self.armed:
                    self.cycle = str(uuid.uuid4())
        # A rejected opening cannot lend its candidates to a later opening.
        if observation == "closed" and self.confirmed == "closed":
            self.opening_at = None
            for person in self.people.values():
                person.eligible = False
        present = {track["track_id"] for track in tracks}
        # Keep provenance across a short loss, but require fresh consecutive observations.
        for key, person in self.people.items():
            if key not in present:
                person.consecutive = 0
        events = []
        h, w = shape[:2]
        for track in tracks:
            key, box = track["track_id"], tuple(track["bbox"])
            point = (((box[0]+box[2])/2-camera_offset[0])/w, ((box[1]+box[3])/2-camera_offset[1])/h)
            inside_reveal = bool(self.settings.arrival_zone and contains(point, self.settings.arrival_zone))
            person = self.people.get(key)
            if person is None:
                predecessors = [(old_key, old) for old_key, old in self.people.items()
                                if old_key not in present and now-old.last_seen < 1.5 and near_box(old.bbox, box, shape)]
                opening = self.armed and self.opening_at is not None and not self.fired
                eligible = opening and inside_reveal
                conflict = any(not old.eligible and now-old.last_seen < 1.5 and near_box(old.bbox, box, shape)
                               for old in self.people.values())
                if predecessors:
                    # Ambiguous association is never a newly arrived person.
                    eligible = opening and len(predecessors) == 1 and predecessors[0][1].eligible
                    born = min(old.born for _, old in predecessors)
                    for old_key, _ in predecessors:
                        del self.people[old_key]
                else:
                    born = now
                eligible = eligible and not conflict
                person = PersonOrigin(born, now, box, eligible)
                self.people[key] = person
            valid = track["confidence"] >= self.settings.confidence_threshold
            if valid:
                person.consecutive = person.consecutive+1 if now-person.last_valid <= gap else 1
                person.last_valid = now
            else:
                person.consecutive = 0
            person.bbox, person.last_seen = box, now
            track["arrival_candidate"] = bool(person.eligible and not self.fired)
            if (self.cycle and not self.fired and observation != "closed" and person.eligible
                    and valid and person.consecutive >= 3 and now-person.born >= self.settings.minimum_track_age):
                created = time.time()
                events.append({"event_id": str(uuid.uuid4()), "event_type": "enter", "track_id": key,
                               "confidence": track["confidence"], "created": created,
                               "timestamp": datetime.fromtimestamp(created, UTC).isoformat(),
                               "trigger": "door_reveal", "door_cycle_id": self.cycle})
                self.fired = True
        return events
