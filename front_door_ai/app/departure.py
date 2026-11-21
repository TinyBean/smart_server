"""A directed, calibrated route for departures that bypass the three zones."""

from dataclasses import dataclass
from math import hypot

from .config import Settings


@dataclass
class DepartureProgress:
    started: float | None = None
    origin: tuple[float, float] | None = None
    peak: float = 0
    samples: int = 0
    arrived: float | None = None

    def reset(self):
        self.started = self.origin = self.arrived = None
        self.peak = self.samples = 0

    def update(self, settings: Settings, point, shape, now, confidence, eligible):
        if not settings.departure_path:
            return False
        height, width = shape[:2]
        start, end = settings.departure_path
        dx, dy = (end[0]-start[0])*width, (end[1]-start[1])*height
        px, py = (point[0]-start[0])*width, (point[1]-start[1])*height
        length = hypot(dx, dy)
        progress = (px*dx + py*dy) / length**2
        distance = abs(px*dy - py*dx) / length / hypot(width, height)
        if distance > settings.departure_path_tolerance or not -.2 <= progress <= 1.2:
            self.reset()
            return False
        # A detour, substantial reversal or stale journey must start over at the camera.
        if self.started is not None and (now-self.started > 8 or self.peak-progress > .15):
            self.reset()
        if self.started is None:
            if progress <= .2 and confidence >= settings.confidence_threshold:
                self.started, self.origin, self.peak, self.samples = now, point, progress, 1
            return False
        self.peak = max(self.peak, progress)
        self.samples += 1
        if progress < .8:
            return False
        if self.arrived is None:
            self.arrived = now
        if not eligible or now-self.arrived > 1.5:
            self.reset()
            return False
        moved = hypot((point[0]-self.origin[0])*width, (point[1]-self.origin[1])*height) / hypot(width, height)
        if (self.samples >= 3 and now-self.started >= settings.minimum_track_age
                and confidence >= settings.confidence_threshold and moved >= settings.minimum_movement):
            # Re-arming requires another observation at the start, even after cooldown expires.
            self.reset()
            return True
        return False
