from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

import cv2
import httpx
import numpy as np

LOG = logging.getLogger(__name__)


class JPEGParser:
    """Bounded JPEG framing, independent of multipart header/chunk boundaries."""

    def __init__(self, maximum=2 * 1024 * 1024):
        self.buffer = bytearray()
        self.maximum = maximum
        self.discarded = 0

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer[:] = self.buffer[-1:]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            nested = self.buffer.find(b"\xff\xd8", 2)
            if nested >= 0 and (end < 0 or nested < end):
                del self.buffer[:nested]
                self.discarded += 1
                continue
            if end < 0:
                if len(self.buffer) > self.maximum:
                    self.buffer[:] = self.buffer[-1:]
                    self.discarded += 1
                break
            if end + 2 <= self.maximum:
                frames.append(bytes(self.buffer[:end + 2]))
            else:
                self.discarded += 1
            del self.buffer[:end + 2]
        return frames


@dataclass(frozen=True)
class Frame:
    sequence: int
    timestamp: float
    generation: int
    jpeg: bytes
    image: np.ndarray


class LatestFrame:
    def __init__(self):
        self.frame: Frame | None = None
        self.sequence = 0
        self.generation = 0
        self.condition = asyncio.Condition()

    async def set(self, jpeg, image):
        async with self.condition:
            self.sequence += 1
            self.frame = Frame(self.sequence, time.monotonic(), self.generation, jpeg, image)
            self.condition.notify_all()

    async def clear(self):
        async with self.condition:
            self.frame = None
            self.generation += 1
            self.condition.notify_all()

    def fresh(self, max_age=3):
        frame = self.frame
        return frame if frame and time.monotonic() - frame.timestamp <= max_age else None

    async def next(self, sequence, timeout=3):
        async with self.condition:
            await asyncio.wait_for(self.condition.wait_for(
                lambda: self.frame is not None and self.frame.sequence > sequence,
            ), timeout)
            return self.frame


class Camera:
    def __init__(self, url: str, frames: LatestFrame):
        self.url = url
        self.frames = frames
        self.connected = False
        self.reconnects = 0
        self.bad_frames = 0
        self.received = deque(maxlen=150)
        self.decode_ms = 0.0

    @property
    def fps(self):
        now = time.monotonic()
        recent = [t for t in self.received if now - t < 10]
        return round((len(recent) - 1) / (recent[-1] - recent[0]), 2) if len(recent) > 1 else 0

    @staticmethod
    def decode(jpeg):
        # Reject excessive decoded dimensions before OpenCV allocates an image.
        # Camera input is QVGA; 1920x1080 leaves room for future sources.
        import io

        from PIL import Image
        with Image.open(io.BytesIO(jpeg)) as header:
            if header.width * header.height > 1920 * 1080:
                raise ValueError("camera frame exceeds pixel limit")
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("invalid JPEG")
        return image

    async def run(self):
        if not self.url:
            await asyncio.Event().wait()
        delay = 1
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=5), trust_env=False) as client:
            while True:
                parser = JPEGParser()
                try:
                    async with client.stream("GET", self.url) as response:
                        response.raise_for_status()
                        last_valid = time.monotonic()
                        # Do not accumulate a fixed chunk: small QVGA frames must be visible immediately.
                        async for chunk in response.aiter_bytes():
                            for jpeg in parser.feed(chunk):
                                started = time.monotonic()
                                try:
                                    image = await asyncio.to_thread(self.decode, jpeg)
                                except (ValueError, OSError, cv2.error):
                                    self.bad_frames += 1
                                    continue
                                self.decode_ms = (time.monotonic()-started)*1000
                                await self.frames.set(jpeg, image)
                                self.received.append(time.monotonic())
                                self.connected = True
                                last_valid = time.monotonic()
                                delay = 1
                            if time.monotonic() - last_valid > 5:
                                raise TimeoutError("no valid camera frame")
                        raise ConnectionError("camera stream ended")
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ConnectionError, TimeoutError):
                    LOG.warning("CAMERA_RECONNECT delay=%s", delay)
                finally:
                    self.connected = False
                    await self.frames.clear()
                self.reconnects += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
