"""Measure small camera translations against stationary background patches."""
import cv2
import numpy as np

SIZE = (160, 120)
PATCH = 5
SEARCH = 8
MAX_SHIFT = 6  # pixels at the reference resolution


def gray(image):
    return cv2.GaussianBlur(cv2.cvtColor(cv2.resize(image, SIZE), cv2.COLOR_BGR2GRAY), (3, 3), 0)


def peak_delta(a, b, c):
    """Parabolic interpolation around the best integer template match."""
    curvature = float(a) - 2*float(b) + float(c)
    return .5*(float(a)-float(c))/curvature if curvature < -.001 else 0.


class BackgroundAlignment:
    def __init__(self, samples, excluded):
        reference = np.median([gray(im) for im in samples["closed"]], axis=0).astype(np.uint8)
        opened = [gray(im) for im in samples["open"]]
        margin = PATCH + SEARCH
        fixed = cv2.erode((~excluded).astype(np.uint8), np.ones((2*margin+1, 2*margin+1), np.uint8),
                          borderType=cv2.BORDER_CONSTANT, borderValue=0)
        points = cv2.goodFeaturesToTrack(reference, 40, .015, 9, mask=fixed)
        self.patches = []
        for x, y in ([] if points is None else points.astype(int).reshape(-1, 2)):
            region = np.s_[y-PATCH:y+PATCH+1, x-PATCH:x+PATCH+1]
            patch = reference[region].copy()
            # The same landmark must exist with the door both open and closed.
            if patch.std() >= 6 and all(cv2.matchTemplate(im[region], patch, cv2.TM_CCOEFF_NORMED).item()
                                        >= .85 for im in opened):
                self.patches.append((x, y, patch))
        self.shift = None
        self.state = "unavailable" if len(self.patches) < 3 else "pending"
        self.next_check = 0.0
        self.last_good = float("-inf")
        self.failures = 0

    def measure(self, image, visible, now):
        if self.state == "unavailable":
            return (0., 0.)  # Legacy calibrations may cover the entire frame.
        if now < self.next_check:
            return self.shift
        self.next_check = now + .5
        if not visible.all() and self.shift is not None and now - self.last_good < 8:
            self.state = "held"
            return self.shift
        mono = gray(image)
        offsets, positions = [], []
        available = 0
        margin = PATCH + SEARCH
        for x, y, patch in self.patches:
            region = np.s_[y-margin:y+margin+1, x-margin:x+margin+1]
            if not visible[region].all():
                continue
            available += 1
            response = cv2.matchTemplate(mono[region], patch, cv2.TM_CCOEFF_NORMED)
            _, score, _, (i, j) = cv2.minMaxLoc(response)
            if score < .82:
                continue
            dx = peak_delta(*response[j, i-1:i+2]) if 0 < i < 2*SEARCH else 0.
            dy = peak_delta(*response[j-1:j+2, i]) if 0 < j < 2*SEARCH else 0.
            offsets.append((i-SEARCH+dx, j-SEARCH+dy))
            positions.append((x, y))

        if len(offsets) >= 3:
            offsets = np.array(offsets)
            shift = np.median(offsets, axis=0)
            agree = np.max(np.abs(offsets-shift), axis=1) <= 1
            if agree.sum() >= max(3, .6*available) and np.max(np.ptp(np.array(positions)[agree], axis=0)) >= 20:
                shift = np.median(offsets[agree], axis=0)
                if np.max(np.abs(shift)) <= MAX_SHIFT:
                    self.shift = tuple(0. if abs(v) < .25 else round(float(v), 2) for v in shift)
                    self.state, self.failures, self.last_good = "aligned", 0, now
                    self.next_check = now + 1
                    return self.shift
        if available < 3 or (available < len(self.patches) and now-self.last_good <= 30):
            self.state = "occluded"
            if now-self.last_good > 30:
                self.shift = None
            return self.shift
        self.failures += 1
        self.state = "recalibrate" if self.failures >= 3 else "pending"
        self.shift = None
        return None
