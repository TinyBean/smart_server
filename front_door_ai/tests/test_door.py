import cv2
import numpy as np
import pytest

from app.config import Settings
from app.door import DoorReferences, DoorRevealEngine


def configuration():
    return Settings(door_zone=[(0, 0), (1, 0), (1, 1), (0, 1)],
                    arrival_zone=[(.5, 0), (1, 0), (1, 1), (.5, 1)])


def person(key=1, x=.75, confidence=.9):
    return {"track_id": key, "bbox": [x*320-20, 50, x*320+20, 220], "confidence": confidence}


class Scene:
    def __init__(self):
        self.engine = DoorRevealEngine(configuration())
        self.time = 0

    def step(self, door, people=(), count=1):
        result = []
        for _ in range(count):
            self.time += .2
            result.extend(self.engine.update(list(people), (240, 320), self.time, door))
        return result


def test_open_then_reveal_once_until_close():
    scene = Scene()
    assert not scene.step("closed", count=3)
    events = scene.step("open", [person()], count=5)
    assert len(events) == 1
    assert events[0]["trigger"] == "door_reveal" and events[0]["event_type"] == "enter"
    assert not scene.step("open", [person(), person(2)], count=6)
    assert not scene.step("closed", count=3)
    events2 = scene.step("open", [person(3, x=.95)], count=5)
    assert len(events2) == 1 and events2[0]["door_cycle_id"] != events[0]["door_cycle_id"]


@pytest.mark.parametrize("scenario", ["no_person", "closed_passage", "already_open", "indoor", "wrong_origin"])
def test_non_arrival_scenarios(scenario):
    scene = Scene()
    if scenario == "already_open":
        assert not scene.step("open", [person()], count=10)
        return
    assert not scene.step("closed", [person()] if scenario == "indoor" else [], count=4)
    if scenario == "closed_passage":
        assert not scene.step("closed", [person()], count=10)
    elif scenario == "wrong_origin":
        assert not scene.step("open", [person(x=.2)], count=4)
        assert not scene.step("open", [person(x=.75)], count=8)
    else:
        assert not scene.step("open", [] if scenario == "no_person" else [person()], count=10)


def test_candidate_during_open_confirmation_and_occlusion():
    scene = Scene()
    scene.step("closed", count=3)
    assert not scene.step("unknown", [person()], count=2)
    assert len(scene.step("open", [person()], count=3)) == 1
    assert not scene.step("unknown", [person(2)], count=6)
    assert scene.engine.status()["phase"] == "fired"


def test_unknown_never_establishes_an_opening():
    scene = Scene()
    scene.step("closed", count=3)
    assert not scene.step("unknown", [person()], count=20)
    assert scene.engine.cycle is None


def test_indoor_id_switch_does_not_become_arrival():
    scene = Scene()
    scene.step("closed", [person()], count=3)
    scene.step("open", count=3)
    assert not scene.step("open", [person(2)], count=6)


def test_ambiguous_origin_is_excluded():
    scene = Scene()
    scene.step("closed", [person(1, .70), person(2, .80)], count=3)
    scene.step("open", count=3)
    assert not scene.step("open", [person(3)], count=6)


def test_duplicate_live_indoor_track_does_not_become_arrival():
    scene = Scene()
    scene.step("closed", [person()], count=3)
    assert not scene.step("open", [person(), person(2)], count=6)


def test_id_switch_after_leaving_reveal_region_preserves_origin():
    scene = Scene()
    scene.step("closed", count=3)
    scene.step("open", [person(x=.55)], count=2)
    assert not scene.step("open", [person(2, x=.48)], count=2)
    assert len(scene.step("open", [person(2, x=.48)])) == 1


def test_low_confidence_and_lost_track_need_fresh_confirmation():
    scene = Scene()
    scene.step("closed", count=3)
    assert not scene.step("open", [person(confidence=.2)], count=5)
    assert len(scene.step("open", [person()], count=3)) == 1


def test_frame_gap_and_restart_require_closed_again():
    scene = Scene()
    scene.step("closed", count=3)
    scene.step("open", count=3)
    scene.time += 2
    assert not scene.step("open", [person()], count=5)
    assert scene.engine.status()["phase"] == "wait_closed"
    restarted = Scene()
    assert not restarted.step("open", [person()], count=5)


def test_lost_observation_preserves_origin_but_restarts_stability():
    scene = Scene()
    scene.step("closed", count=3)
    scene.step("open", count=3)
    assert not scene.step("open", [person()], count=2)
    assert not scene.step("open")
    assert not scene.step("open", [person()], count=2)
    assert len(scene.step("open", [person()])) == 1


def test_false_open_observation_cannot_supply_later_candidate():
    scene = Scene()
    scene.step("closed", count=3)
    scene.step("open", [person()])
    scene.step("closed", [person()], count=3)
    assert not scene.step("open", [person()], count=5)


def images():
    closed = np.full((240, 320, 3), 55, np.uint8)
    cv2.rectangle(closed, (50, 20), (280, 225), (150, 150, 150), 7)
    cv2.line(closed, (220, 35), (220, 215), (210, 210, 210), 5)
    opened = np.full_like(closed, 55)
    cv2.line(opened, (60, 0), (120, 239), (170, 170, 170), 12)
    cv2.rectangle(opened, (180, 80), (290, 180), (110, 110, 110), 6)
    return closed, opened


def test_reference_calibration_brightness_occlusion_and_persistence(tmp_path):
    settings = configuration()
    closed, opened = images()
    refs = DoorReferences(tmp_path/"reference.npz")
    refs = refs.capture("closed", [closed]*3, settings, closed.shape)
    assert not refs.matches(settings)
    refs = refs.capture("open", [opened]*3, settings, opened.shape)
    assert refs.matches(settings)
    assert refs.observe(closed, [], settings)["observation"] == "closed"
    assert refs.observe(opened, [], settings)["observation"] == "open"
    brighter = cv2.convertScaleAbs(closed, alpha=.9, beta=25)
    assert refs.observe(brighter, [], settings)["observation"] == "closed"
    assert refs.observe(opened, [[0, 0, 320, 240]], settings)["observation"] == "unknown"
    unrelated = np.random.default_rng(4).integers(0, 255, closed.shape, dtype=np.uint8)
    assert refs.observe(unrelated, [], settings)["observation"] == "unknown"
    assert DoorReferences.load(refs.path).observe(opened, [], settings)["observation"] == "open"
    changed = settings.model_copy(update={"door_zone": [(0, 0), (.5, 0), (.5, 1), (0, 1)]})
    assert not refs.matches(changed)
    assert not refs.matches(settings, (480, 640))
    refs = refs.capture("closed", [closed], settings, closed.shape)
    assert not refs.matches(settings) and not refs.samples["open"]


def test_similar_references_refused_and_overlapping_detection_regions_allowed(tmp_path):
    settings = configuration()
    closed, _ = images()
    refs = DoorReferences(tmp_path/"refs.npz").capture("closed", [closed], settings, closed.shape)
    refs = refs.capture("open", [closed], settings, closed.shape)
    assert not refs.matches(settings) and "过于相似" in refs.error


def test_background_translation_uses_stationary_landmarks_and_expires_on_obstruction(tmp_path):
    settings = Settings(door_zone=[(.45,.15),(.83,.15),(.83,.85),(.45,.85)],
                        arrival_zone=[(.44,.15),(.9,.15),(.9,1),(.44,1)])
    rng = np.random.default_rng(7)
    closed = cv2.resize(rng.integers(10, 240, (100, 140, 3), np.uint8), (320, 240))
    opened = closed.copy()
    opened[34:210, 140:270] = cv2.flip(closed[34:210, 140:270], 1)
    refs = DoorReferences(tmp_path/"door.npz")
    refs = refs.capture("closed", [closed], settings, closed.shape)
    refs = refs.capture("open", [opened], settings, closed.shape)
    refs = DoorReferences.load(refs.path)

    def shifted(image, x, y):
        return cv2.warpAffine(image, np.float32([[1,0,x],[0,1,y]]), (320,240), borderMode=cv2.BORDER_REPLICATE)

    for t, (image, label) in enumerate(((closed, "closed"), (opened, "open"))):
        result = refs.observe(shifted(image, -3, 5), [], settings, now=t)
        assert result["observation"] == label
        assert result["camera_offset_px"] == pytest.approx([-3,5], abs=1)
    blocked = [[0,0,320,240]]
    assert refs.observe(closed, blocked, settings, now=3)["observation"] == "unknown"
    assert refs.observe(closed, blocked, settings, now=35)["alignment"] == "occluded"
    for now in (36, 37, 38):
        result = refs.observe(shifted(closed, 35, 20), [], settings, now=now)
        assert result["observation"] == "unknown"
    assert result["alignment"] == "recalibrate"
    assert refs.observe(closed, [], settings, now=39)["observation"] == "closed"
