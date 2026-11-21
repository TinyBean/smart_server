"""Local MJPEG source for fan-out/reconnect tests. The test pattern is not a person detector fixture."""
import argparse
import asyncio
import time

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse


def create_app(fps=10, disconnect_after=0, corrupt_every=0):
    app = FastAPI()
    counters = {"connections": 0, "active": 0, "peak_active": 0, "frames": 0}

    @app.get("/stats")
    async def stats():
        return counters

    @app.get("/stream")
    async def stream():
        async def chunks():
            counters["connections"] += 1
            counters["active"] += 1
            counters["peak_active"] = max(counters["peak_active"], counters["active"])
            sent = 0
            try:
                while not disconnect_after or sent < disconnect_after:
                    image = np.full((240, 320, 3), (35, 31, 21), dtype=np.uint8)
                    cv2.putText(image, "TEST CAMERA / 10s GRID", (15, 50), cv2.FONT_HERSHEY_SIMPLEX, .5, (130, 220, 180), 1)
                    cv2.putText(image, f"{time.time():.3f}", (15, 120), cv2.FONT_HERSHEY_SIMPLEX, .7, (240, 240, 240), 1)
                    x = int(time.monotonic()*40) % 320
                    cv2.line(image, (x, 150), (x, 220), (70, 210, 190), 3)
                    sent += 1
                    counters["frames"] += 1
                    jpeg = (b"\xff\xd8bad\xff\xd9" if corrupt_every and sent % corrupt_every == 0
                            else cv2.imencode(".jpg", image)[1].tobytes())
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+jpeg+b"\r\n"
                    await asyncio.sleep(1/fps)
            finally:
                counters["active"] -= 1
        return StreamingResponse(chunks(), media_type="multipart/x-mixed-replace; boundary=frame")

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18481)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--disconnect-after", type=int, default=0, help="Close each stream after N frames")
    parser.add_argument("--corrupt-every", type=int, default=0, help="Send an invalid JPEG every N frames")
    args = parser.parse_args()
    if not 1 <= args.fps <= 30 or args.disconnect_after < 0 or args.corrupt_every < 0:
        parser.error("fps must be 1..30 and fault intervals must be non-negative")
    uvicorn.run(create_app(args.fps, args.disconnect_after, args.corrupt_every),
                host="127.0.0.1", port=args.port, access_log=False)
