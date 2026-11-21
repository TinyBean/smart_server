import asyncio
import contextlib

import cv2
import numpy as np
import pytest

from app.camera import Camera, JPEGParser, LatestFrame


def jpeg():
    return cv2.imencode(".jpg", np.zeros((240, 320, 3), dtype=np.uint8))[1].tobytes()


def test_fragmented_mjpeg_and_bad_parts():
    data = jpeg()
    wire = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+data+b"\r\n--frame\r\n"+data
    parser = JPEGParser()
    result = []
    for i in range(0, len(wire), 7):
        result.extend(parser.feed(wire[i:i+7]))
    assert result == [data, data]
    assert len(parser.buffer) < 2


def test_bounded_and_resynchronizes():
    parser = JPEGParser(maximum=1024)
    assert not parser.feed(b"junk"*10000)
    assert len(parser.buffer) <= 1
    assert not parser.feed(b"\xff\xd8"+b"x"*3000)
    assert len(parser.buffer) <= 1
    assert parser.feed(b"\xff\xd8broken\xff\xd8ok\xff\xd9") == [b"\xff\xd8ok\xff\xd9"]
    assert parser.discarded >= 2
    with pytest.raises((ValueError, OSError)):
        Camera.decode(b"not a jpeg")


async def test_small_frame_delivered_without_waiting_for_more_network_bytes():
    release = asyncio.Event()

    async def upstream(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=f\r\n\r\n"
                         b"--f\r\n\r\n" + jpeg() + b"\r\n")
            await writer.drain()
            await release.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    frames = LatestFrame()
    camera = Camera(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/", frames)
    task = asyncio.create_task(camera.run())
    try:
        frame = await frames.next(0, timeout=.8)
        assert frame.image.shape == (240, 320, 3)
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        server.close()
        await server.wait_closed()


async def test_one_upstream_shared_by_two_consumers_and_reconnect():
    connections = 0
    payload = jpeg()
    writers = []

    async def camera_server(reader, writer):
        nonlocal connections
        connections += 1
        writers.append(writer)
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n")
            for _ in range(8):
                writer.write(b"--frame\r\n\r\n"+payload+b"\r\n")
                await writer.drain()
                await asyncio.sleep(.04)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(camera_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    frames = LatestFrame()
    camera = Camera(f"http://127.0.0.1:{port}/stream", frames)
    task = asyncio.create_task(camera.run())
    try:
        a, b = await asyncio.gather(frames.next(0), frames.next(0))
        assert a is b and connections == 1
        first_generation = a.generation
        await asyncio.sleep(1.8)
        assert connections >= 2 and frames.generation > first_generation
        assert camera.reconnects >= 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
