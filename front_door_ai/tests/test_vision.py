import asyncio
import time

import numpy as np
import pytest
from pydantic import ValidationError

from app.camera import Frame
from app.config import Settings
from app.vision import ByteTracker, Detection, DirectionEngine


def tracks(y, key=1, confidence=.9):
    return [{"track_id": key, "bbox": [100, max(0, y*240-80), 160, y*240], "confidence": confidence}]


@pytest.mark.parametrize("mode,expected", [("zone_crossing", ["enter", "exit"]), ("door_reveal", ["exit"])])
async def test_vision_replaces_enter_only_and_preserves_exit(settings, monkeypatch, mode, expected):
    from app import vision

    class Detector:
        threads = 2

        def __init__(self, _):
            pass

        def detect(self, image):
            y = int(image[0, 0, 0])*2.4
            return [Detection((100, max(0, y-50), 160, y), .9)]

    class Tracker:
        def __init__(self, _):
            pass

        def update(self, detections, _):
            return [{"track_id": 1, "bbox": list(d.bbox), "confidence": d.confidence} for d in detections]

    class Frames:
        generation = 0
        index = 0

        async def next(self, *_args, **_kwargs):
            points = [20, 50, 80, 50, 20]
            if self.index == len(points):
                raise asyncio.CancelledError
            value = points[self.index]
            self.index += 1
            return Frame(self.index, started+self.index*.3, 0, b"", np.full((240, 320, 3), value, np.uint8))

    monkeypatch.setattr(vision, "OpenVINODetector", Detector)
    monkeypatch.setattr(vision, "ByteTracker", Tracker)
    settings = settings.model_copy(update={"entry_detection_mode": mode, "event_cooldown": 0, "detection_fps": 15})
    events = []

    async def emit(event, *_):
        events.append(event)

    started = time.monotonic()
    worker = vision.Vision(Frames(), settings, "unused", emit)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert [event["event_type"] for event in events] == expected


@pytest.mark.parametrize("points,kind", [([.2, .5, .8], "enter"), ([.8, .5, .2], "exit")])
def test_full_crossing(settings, points, kind):
    engine = DirectionEngine(settings)
    assert not engine.update(tracks(points[0]), (240, 320), 0)
    assert not engine.update(tracks(points[1]), (240, 320), .3)
    result = engine.update(tracks(points[2]), (240, 320), .6)
    assert len(result) == 1 and result[0]["event_type"] == kind
    assert not engine.update(tracks(points[2]), (240, 320), .9)


@pytest.mark.parametrize("points", [[.2, .8], [.2, .5, .2], [.5, .8], [.2, .2, .2]])
def test_incomplete_migration_has_no_event(settings, points):
    engine = DirectionEngine(settings)
    assert not [event for n, y in enumerate(points) for event in engine.update(tracks(y), (240, 320), n*.3)]


def test_lost_id_and_cooldown(settings):
    engine = DirectionEngine(settings)
    engine.update(tracks(.2), (240, 320), 0)
    engine.update(tracks(.5), (240, 320), .3)
    assert not engine.update(tracks(.8, key=2), (240, 320), .6)
    assert not engine.update(tracks(.8), (240, 320), 2)
    engine = DirectionEngine(settings)
    for n, y in enumerate([.2, .5, .8]):
        result = engine.update(tracks(y), (240, 320), n*.3)
    assert len(result) == 1
    assert not engine.update(tracks(.5), (240, 320), .9)
    assert not engine.update(tracks(.2), (240, 320), 1.2)


def test_multiple_people_independent(settings):
    engine = DirectionEngine(settings)
    engine.update(tracks(.2, 1)+tracks(.8, 2), (240, 320), 0)
    engine.update(tracks(.5, 1)+tracks(.5, 2), (240, 320), .3)
    events = engine.update(tracks(.8, 1)+tracks(.2, 2), (240, 320), .6)
    assert {e["event_type"] for e in events} == {"enter", "exit"}


def test_crossing_waits_for_confidence_recovery(settings):
    """A real field crossing reached outside at .19, then recovered to .73."""
    engine = DirectionEngine(settings)
    assert not engine.update(tracks(.8), (240, 320), 0)
    assert not engine.update(tracks(.5, confidence=.3), (240, 320), .3)
    assert not engine.update(tracks(.2, confidence=.19), (240, 320), .6)
    for now in (.9, 1.2, 1.5):
        assert not engine.update(tracks(.2, confidence=.3), (240, 320), now)
    events = engine.update(tracks(.2, confidence=.73), (240, 320), 1.8)
    assert [event["event_type"] for event in events] == ["exit"]
    assert not engine.update(tracks(.2), (240, 320), 2.1)


@pytest.mark.parametrize("cancel", ["timeout", "return", "lost", "outside_zones"])
def test_unconfirmed_crossing_cannot_be_reused(settings, cancel):
    engine = DirectionEngine(settings)
    engine.update(tracks(.8), (240, 320), 0)
    engine.update(tracks(.5), (240, 320), .3)
    assert not engine.update(tracks(.2, confidence=.19), (240, 320), .6)
    if cancel == "timeout":
        for now in (.9, 1.2, 1.5, 1.8, 2.1):
            assert not engine.update(tracks(.2, confidence=.3), (240, 320), now)
        assert not engine.update(tracks(.2), (240, 320), 2.4)
    elif cancel == "return":
        assert not engine.update(tracks(.8), (240, 320), .9)
        assert not engine.update(tracks(.2), (240, 320), 1.2)
    elif cancel == "lost":
        assert not engine.update(tracks(.2), (240, 320), 1.4)
    else:
        assert not engine.update(tracks(1.1), (240, 320), .9)
        assert not engine.update(tracks(.2), (240, 320), 1.2)


@pytest.mark.parametrize("anchor,expected", [("bottom_center", []), ("center", ["exit"])])
def test_cropped_person_crossing_uses_selected_anchor(anchor, expected):
    settings = Settings(
        tracking_point=anchor,
        outside_zone=[(0, 0), (.35, 0), (.35, .95), (0, .95)],
        transition_zone=[(.35, 0), (.65, 0), (.65, .95), (.35, .95)],
        inside_zone=[(.65, 0), (1, 0), (1, .95), (.65, .95)],
    )
    engine = DirectionEngine(settings)
    events = []
    for now, x, confidence in [(0, .8, .9), (.3, .5, .3), (.6, .2, .19), (.9, .2, .73)]:
        observed = [{"track_id": 8, "bbox": [x*320-20, 60, x*320+20, 240], "confidence": confidence}]
        events.extend(engine.update(observed, (240, 320), now))
        assert observed[0]["point"][1] == (1 if anchor == "bottom_center" else .625)
    assert [event["event_type"] for event in events] == expected


def test_uncalibrated_never_publishes():
    engine = DirectionEngine(Settings())
    for n, y in enumerate([.2, .5, .8]):
        assert not engine.update(tracks(y), (240, 320), n*.3)


def test_invalid_and_overlapping_zones(settings):
    with pytest.raises(ValidationError):
        Settings(outside_zone=[(0, 0), (1, 1), (0, 1), (1, 0)])
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "inside_zone": settings.outside_zone})
    with pytest.raises(ValidationError):
        Settings(confidence_threshold=float("nan"))


def test_real_bytetrack_keeps_identity(settings):
    pytest.importorskip("trackers")
    tracker = ByteTracker(settings)
    observed = []
    for n in range(12):
        result = tracker.update([Detection((50+n, 20, 100+n, 180), .9 if n != 8 else .3)], n*.2)
        if result:
            observed.append(result[0]["track_id"])
    assert len(observed) >= 8 and len(set(observed)) == 1
    assert tracker.update([], 10) == []


@pytest.mark.model
def test_real_openvino_model():
    import os
    from pathlib import Path

    from app.vision import OpenVINODetector
    path = Path(os.environ.get("FRONTDOOR_TEST_MODEL", "models/person-detection-retail-0013.xml"))
    if not path.exists():
        pytest.skip("model not downloaded")
    detector = OpenVINODetector(str(path))
    detections = detector.detect(np.zeros((240, 320, 3), dtype=np.uint8))
    # Low-confidence boxes are intentionally preserved for ByteTrack's second association stage.
    assert all(.1 <= box.confidence < .5 for box in detections)
    tracker = ByteTracker(Settings())
    for index in range(4):
        assert tracker.update(detections, index*.2) == []
