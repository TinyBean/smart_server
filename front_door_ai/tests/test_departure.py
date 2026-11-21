import asyncio
import time

import numpy as np
import pytest
from pydantic import ValidationError

from app.camera import Frame
from app.config import Settings
from app.vision import DirectionEngine, annotate

SHAPE = (240, 320)
PATH = [(120/320, 235/240), (200/320, 155/240)]


def observation(progress, *, key=1, confidence=.9, offset=(0, 0), anchor="center", shape=SHAPE):
    start, end = PATH
    x, y = (start[i]+(end[i]-start[i])*progress+offset[i] for i in (0, 1))
    height, width = shape
    x, y = x*width, y*height
    # Cropped upper-body boxes: center is the chosen observation point.
    box = [x-15, y-5 if anchor == "center" else y-10, x+15, y+5 if anchor == "center" else y]
    return [{"track_id": key, "bbox": box, "confidence": confidence}]


@pytest.fixture
def route_settings():
    return Settings(tracking_point="center", departure_path=PATH)


def replay(engine, positions, *, start=0, step=.3, **kwargs):
    return [event for index, point in enumerate(positions)
            for event in engine.update(observation(point, **kwargs), kwargs.get("shape", SHAPE), start+index*step)]


@pytest.mark.parametrize("anchor", ["center", "bottom_center"])
@pytest.mark.parametrize("shape", [(240, 320), (1080, 1920), (960, 540)])
def test_camera_side_departure_without_three_zones(route_settings, anchor, shape):
    route_settings.tracking_point = anchor
    engine = DirectionEngine(route_settings)
    events = replay(engine, [0, .3, .6, .9, 1, 1], anchor=anchor, shape=shape)
    assert len(events) == 1
    assert events[0]["event_type"] == "exit" and events[0]["trigger"] == "departure_path"
    assert events[0]["track_id"] == 1


def test_camera_route_with_no_observed_inside_zone(route_settings):
    settings = route_settings.model_copy(update={
        "inside_zone": [(0, 0), (.44, 0), (.44, .95), (0, .95)],
        "transition_zone": [(.44, 0), (.60, 0), (.60, 1), (.44, 1)],
        "outside_zone": [(.60, 0), (1, 0), (1, 1), (.60, 1)],
    })
    assert [e["event_type"] for e in replay(DirectionEngine(settings), [0, .3, .6, 1])] == ["exit"]
    settings.departure_path = []
    assert replay(DirectionEngine(settings), [0, .3, .6, 1]) == []


@pytest.mark.parametrize("positions", [
    [1, .7, .4, 0],  # Entering toward the camera.
    [.5, .7, 1],  # First seen halfway down the route.
    [0, .03, 0, .03, 0],  # Standing still / detector jitter.
    [0, .3, .6, .3, .7, 1],  # Reversal invalidates the origin.
    [0, .4, .6],  # Has not reached the door.
    [.9, .95, 1, 1],  # Appeared at the destination.
])
def test_incomplete_and_reverse_routes_do_not_emit(route_settings, positions):
    assert replay(DirectionEngine(route_settings), positions) == []


def test_route_rearms_only_after_return_to_start(route_settings):
    engine = DirectionEngine(route_settings)
    assert len(replay(engine, [0, .3, .6, .9])) == 1
    assert not replay(engine, [1]*24, start=1.2)  # Linger past cooldown.
    assert not replay(engine, [.6, .3, 0], start=8.4)
    assert len(replay(engine, [.3, .6, .9], start=9.3)) == 1


@pytest.mark.parametrize("cancel", ["gap", "new_id", "detour", "timeout", "low_origin"])
def test_invalid_route_origin_cannot_be_reused(route_settings, cancel):
    engine = DirectionEngine(route_settings)
    assert not replay(engine, [0, .3], confidence=.2 if cancel == "low_origin" else .9)
    if cancel == "gap":
        assert not replay(engine, [.6, 1], start=1.1)
    elif cancel == "new_id":
        assert not replay(engine, [.6, 1], start=.6, key=2)
    elif cancel == "detour":
        assert not replay(engine, [.5], start=.6, offset=(.3, 0))
        assert not replay(engine, [.7, 1], start=.9)
    elif cancel == "timeout":
        assert not replay(engine, [.3]*28, start=.6)
        assert not replay(engine, [.6, 1], start=9)
    else:
        assert not replay(engine, [.6, 1], start=.6)


def test_route_confidence_can_recover_briefly_at_door(route_settings):
    engine = DirectionEngine(route_settings)
    assert not replay(engine, [0, .3, .6])
    assert not replay(engine, [.9, 1], start=.9, confidence=.19)
    events = replay(engine, [1], start=1.5, confidence=.73)
    assert len(events) == 1 and events[0]["confidence"] == .73


def test_route_does_not_confirm_after_confidence_timeout(route_settings):
    engine = DirectionEngine(route_settings)
    assert not replay(engine, [0, .3, .6])
    assert not replay(engine, [1]*7, start=.9, confidence=.19)
    assert not replay(engine, [1], start=3)


def test_small_destination_jitter_does_not_extend_confidence_window(route_settings):
    engine = DirectionEngine(route_settings)
    assert not replay(engine, [0, .3, .6])
    assert not replay(engine, [.81, .79, .81, .79, .81, .79], start=.9, confidence=.19)
    assert not replay(engine, [.81], start=2.7)


@pytest.mark.parametrize("setting,value", [("minimum_track_age", 3), ("minimum_movement", .9)])
def test_route_respects_detection_thresholds(route_settings, setting, value):
    setattr(route_settings, setting, value)
    assert not replay(DirectionEngine(route_settings), [0, .3, .6, .9, 1])


def test_multiple_people_do_not_share_route_progress(route_settings):
    engine = DirectionEngine(route_settings)
    events = []
    for index, position in enumerate([0, .3, .6, .9]):
        tracks = observation(position, key=1) + observation(1-position, key=2)
        events.extend(engine.update(tracks, SHAPE, index*.3))
    assert [(e["track_id"], e["event_type"]) for e in events] == [(1, "exit")]


@pytest.mark.parametrize("boundary", [.67, .70, .78, .85])
def test_route_and_zone_crossing_emit_only_once_even_without_cooldown(route_settings, boundary):
    # Both rules describe the same departure, with zone arrival before, during or after route arrival.
    route_settings = route_settings.model_copy(update={
        "event_cooldown": 0,
        "inside_zone": [(0, .9), (1, .9), (1, 1), (0, 1)],
        "transition_zone": [(0, boundary), (1, boundary), (1, .9), (0, .9)],
        "outside_zone": [(0, 0), (1, 0), (1, boundary), (0, boundary)],
    })
    events = replay(DirectionEngine(route_settings), [0, .3, .6, .85, 1, 1, 1])
    assert [e["event_type"] for e in events] == ["exit"]


def test_route_cannot_publish_deferred_event_after_cooldown(route_settings):
    engine = DirectionEngine(route_settings)
    assert len(replay(engine, [0, .3, .6, .9])) == 1
    assert not replay(engine, [.6, .3, 0, .3, .6, .9]+[1]*20, start=1.2)


@pytest.mark.parametrize("path", [
    [(0, 0)], [(0, 0), (0, 0)], [(0, 0), (.01, .01)],
    [(0, 0), (1, 1), (0, 1)], [(0, 0), (1.1, .5)], [(0, 0), (float("nan"), 1)],
])
def test_route_validation(path):
    with pytest.raises(ValidationError):
        Settings(departure_path=path)


def test_route_is_opt_in_and_can_be_cleared(route_settings):
    assert not replay(DirectionEngine(Settings(tracking_point="center")), [0, .3, .6, 1])
    route_settings.departure_path = []
    assert not replay(DirectionEngine(route_settings), [0, .3, .6, 1])


def test_route_annotation_is_rendered(route_settings):
    import cv2

    frame = Frame(1, 0, 0, b"", np.zeros((*SHAPE, 3), dtype=np.uint8))
    image = cv2.imdecode(np.frombuffer(annotate(frame, [], route_settings), dtype=np.uint8), cv2.IMREAD_COLOR)
    # Red arrow near the midpoint of the example camera-to-door route.
    assert image[195, 160, 2] > 150
    assert image[195, 160, 2] > image[195, 160, 0]+60


@pytest.mark.parametrize("mode", ["zone_crossing", "door_reveal"])
@pytest.mark.parametrize("reset", [None, "reconnect", "configure"])
async def test_vision_routes_work_in_both_entry_modes_and_reset_on_changes(route_settings, monkeypatch, mode, reset):
    from app import vision

    route_settings = route_settings.model_copy(update={"entry_detection_mode": mode, "detection_fps": 15})
    started = time.monotonic()

    class Detector:
        threads = 2

        def __init__(self, _):
            pass

        def detect(self, image):
            track = observation(float(image[0, 0, 0])/100)[0]
            return [vision.Detection(tuple(track["bbox"]), track["confidence"])]

    class Tracker:
        def __init__(self, _):
            pass

        def update(self, detections, _):
            return [{"track_id": 1, "bbox": list(d.bbox), "confidence": d.confidence} for d in detections]

    class Frames:
        generation = 0
        index = 0

        async def next(self, *_args, **_kwargs):
            points = [0, 30, 60, 90, 100]
            if self.index == len(points):
                raise asyncio.CancelledError
            if self.index == 2:
                if reset == "reconnect":
                    self.generation += 1
                elif reset == "configure":
                    worker.configure(route_settings.model_copy())
            value = points[self.index]
            self.index += 1
            return Frame(self.index, started+self.index*.3, self.generation, b"",
                         np.full((*SHAPE, 3), value, np.uint8))

    monkeypatch.setattr(vision, "OpenVINODetector", Detector)
    monkeypatch.setattr(vision, "ByteTracker", Tracker)
    events = []

    async def emit(event, *_):
        events.append(event)

    worker = vision.Vision(Frames(), route_settings, "unused", emit)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert [e["event_type"] for e in events] == ([] if reset else ["exit"])
