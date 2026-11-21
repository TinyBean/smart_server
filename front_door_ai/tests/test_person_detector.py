from types import SimpleNamespace

import numpy as np
import pytest

from app.config import Settings
from app.vision import ByteTracker, OpenVINODetector


@pytest.mark.parametrize("available,expected", [(1, 1), (2, 2), (4, 4), (16, 4)])
def test_detector_respects_cpu_affinity_and_caps_resource_use(monkeypatch, available, expected):
    import openvino

    from app import vision
    captured = {}

    class Core:
        def compile_model(self, model, device, options):
            captured.update(options)
            return SimpleNamespace(create_infer_request=object)

    monkeypatch.setattr(openvino, "Core", Core)
    # A container may see a larger host count but have a smaller assigned CPU set.
    monkeypatch.setattr(vision.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(vision.os, "sched_getaffinity", lambda _pid: set(range(available)), raising=False)
    detector = OpenVINODetector("model.xml")
    assert detector.threads == expected
    assert captured["INFERENCE_NUM_THREADS"] == expected
    assert captured["NUM_STREAMS"] == min(2, expected)
    assert len(detector.requests) == 2


def test_detector_cpu_count_fallback_without_affinity(monkeypatch):
    import openvino

    from app import vision
    monkeypatch.delattr(vision.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(vision.os, "cpu_count", lambda: None)
    monkeypatch.setattr(openvino, "Core", lambda: SimpleNamespace(
        compile_model=lambda *_args: SimpleNamespace(create_infer_request=object)))
    assert OpenVINODetector("model.xml").threads == 1


def detector_for(*outputs):
    detector = OpenVINODetector.__new__(OpenVINODetector)
    pending = iter(outputs)
    active = []

    class Request:
        def start_async(self, inputs, share_inputs):
            assert not active or self not in active
            assert inputs[0].shape == (1, 3, 320, 544)
            assert inputs[0].dtype == np.float32 and inputs[0].flags.c_contiguous
            assert share_inputs
            self.tensor = inputs[0]
            self.data = np.array(next(pending), dtype=np.float32).reshape(1, 1, -1, 7)
            active.append(self)

        def wait(self):
            assert len(active) == 2, "Both views must start before either is awaited"

        def get_output_tensor(self):
            assert self in active
            active.remove(self)
            return SimpleNamespace(data=self.data)

    detector.requests = [Request(), Request()]
    return detector


def test_partial_back_context_starts_track_without_lowering_confidence():
    # The field reproduction is weak at native scale but confident with context.
    # Coordinates describe the same cropped person in the 320x240 and 480x360 views.
    weak = [[0, 1, .12, .1, .25, .6, 1]]
    strong = [[0, 1, .97, 112/480, 120/360, 272/480, 330/360]]
    detector = detector_for(weak, strong, weak, strong)
    tracker = ByteTracker(Settings())
    image = np.zeros((240, 320, 3), np.uint8)
    first = detector.detect(image)
    assert len(first) == 1
    assert first[0].bbox == pytest.approx((32, 60, 192, 240))
    assert first[0].confidence == pytest.approx(.97)
    tracker.update(first, 1)
    tracked = tracker.update(detector.detect(image), 1.2)
    assert len(tracked) == 1


def test_context_preserves_separate_native_person_and_rejects_padding_only_boxes():
    primary = [[0, 1, .9, .7, .1, .95, .9]]
    context = [
        [0, 1, .95, 90/480, 90/360, 190/480, 320/360],
        [0, 1, .99, 0, 0, 60/480, 40/360],  # Entirely outside the real image.
        [0, 1, float("nan"), .2, .2, .8, .8],
        [0, 2, .99, .2, .2, .8, .8],
    ]
    detections = detector_for(primary, context).detect(np.zeros((240, 320, 3), np.uint8))
    assert len(detections) == 2
    assert sorted(d.confidence for d in detections) == pytest.approx([.9, .95])
    assert all(0 <= d.bbox[0] < d.bbox[2] <= 320 and 0 <= d.bbox[1] < d.bbox[3] <= 240
               for d in detections)


def test_context_handles_empty_outputs():
    assert detector_for([], []).detect(np.zeros((241, 321, 3), np.uint8)) == []


@pytest.mark.parametrize("failure_at", ["start", "wait"])
def test_inference_failure_drains_every_started_request(failure_at):
    detector = OpenVINODetector.__new__(OpenVINODetector)
    waited = []

    class Request:
        def __init__(self, index):
            self.index = index

        def start_async(self, *_args, **_kwargs):
            if failure_at == "start" and self.index == 1:
                raise RuntimeError("start failed")

        def wait(self):
            waited.append(self.index)
            if failure_at == "wait" and self.index == 0:
                raise RuntimeError("inference failed")

    detector.requests = [Request(0), Request(1)]
    with pytest.raises(RuntimeError):
        detector.detect(np.zeros((240, 320, 3), np.uint8))
    assert waited == ([0] if failure_at == "start" else [0, 1])
