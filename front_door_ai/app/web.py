from __future__ import annotations

import asyncio
import html
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ArrivalSettings, DepartureSettings, Settings

STATIC = Path(__file__).parent / "static"


class DepartureSimulation(BaseModel):
    event_id: UUID
    expected_items: list[str] = Field(max_length=100)
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ArrivalSimulation(BaseModel):
    event_id: UUID
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DoorReferenceCapture(BaseModel):
    state: Literal["closed", "open"]


def media_routes(app, runtime, prefix=""):
    @app.get(prefix+"/healthz")
    async def health():
        return {"alive": True, "state": runtime.status()["state"]}

    @app.get(prefix+"/readyz")
    async def ready():
        status = runtime.status()
        return JSONResponse(status, status_code=200 if status["state"] == "RUNNING" else 503)

    @app.get(prefix+"/snapshot")
    async def snapshot(overlay: bool = False):
        frame = runtime.frames.fresh()
        if frame is None:
            raise HTTPException(503, "摄像头尚无新画面")
        content = frame.jpeg
        if prefix and overlay and runtime.vision.overlay and time.monotonic()-runtime.vision.overlay_at < 3:
            content = runtime.vision.overlay
        return Response(content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get(prefix+"/stream")
    async def stream(request: Request):
        if not runtime.frames.fresh():
            raise HTTPException(503, "摄像头尚无新画面")

        async def chunks():
            sequence = 0
            while not await request.is_disconnected():
                try:
                    frame = await runtime.frames.next(sequence, timeout=3)
                except TimeoutError:
                    break
                if time.monotonic()-frame.timestamp > 3:
                    break
                sequence = frame.sequence
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame.jpeg)).encode()+b"\r\n\r\n"+frame.jpeg+b"\r\n")

        return StreamingResponse(chunks(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get(prefix+"/audio/{cache_id}.wav")
    async def audio(cache_id: str, request: Request):
        if not re.fullmatch(r"[0-9a-f]{64}", cache_id):
            raise HTTPException(404)
        path = runtime.tts.cache / f"{cache_id}.wav"
        if not path.is_file():
            raise HTTPException(404)
        # Cache IDs identify synthesis inputs, not necessarily identical bytes after forced regeneration.
        stat = path.stat()
        etag = f'"{cache_id}-{stat.st_mtime_ns}-{stat.st_size}"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return FileResponse(path, media_type="audio/wav", headers=headers)


def create_apps(runtime):
    media = FastAPI(title="Front Door AI Media", docs_url=None, redoc_url=None, openapi_url=None)
    admin = FastAPI(title="Front Door AI", docs_url=None, redoc_url=None, openapi_url=None)
    media_routes(media, runtime)
    media_routes(admin, runtime, "/api")
    simulation_lock = asyncio.Lock()

    @admin.middleware("http")
    async def ingress(request: Request, call_next):
        allowed = {"127.0.0.1", "::1"} if runtime.dev else {"172.30.32.2"}
        if request.client is None or request.client.host not in allowed:
            return JSONResponse({"detail": "Ingress access only"}, status_code=403)
        if request.method in {"POST", "PUT"}:
            size = 0
            body = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 32768:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
                body.append(chunk)
            request._body = b"".join(body)
        return await call_next(request)

    @admin.get("/")
    async def index(request: Request):
        base = request.headers.get("x-ingress-path", "").rstrip("/")+"/"
        page = (STATIC / "index.html").read_text("utf-8").replace("__BASE__", html.escape(base, quote=True))
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @admin.get("/api/status")
    async def status():
        return runtime.status()

    @admin.get("/api/tracks")
    async def tracks():
        return runtime.vision.tracks if runtime.frames.fresh() else []

    @admin.get("/api/events")
    async def events(limit: int = Query(100, ge=1, le=500)):
        return runtime.store.events(limit)

    @admin.post("/api/events/simulate-exit", status_code=202)
    async def simulate_exit(value: DepartureSimulation):
        # Use the same persisted event -> HA automation -> playback path as vision.
        # Never bypass the event-age, playback deduplication or receipt checks.
        async with simulation_lock:
            key = str(value.event_id)
            previous = runtime.store.event(key)
            if previous:
                if previous.get("source") != "simulation" or previous["event_type"] != "exit":
                    raise HTTPException(409, "事件编号已被使用")
                return {"accepted": True, "event_id": key, "duplicate": True}
            if not (runtime.mqtt.connected and runtime.ha.connected and runtime.reminders.device_online):
                raise HTTPException(503, "请等待 HA、MQTT 和 ESP32 全部连接")
            if not runtime.reminders.enabled:
                raise HTTPException(409, "请先启用出门播报")
            now = time.time()
            if any(row.get("source") == "simulation" and now-row["created"] < 10
                   for row in runtime.store.events()):
                raise HTTPException(429, "请等待 10 秒后再模拟")
            if any(row["state"] in {"requested", "playing", "finished", "completing"}
                   or (row["state"] == "fallback" and row["deadline"] > now)
                   for row in runtime.store.playbacks()):
                raise HTTPException(409, "上一条播放仍在处理，请稍后再试")
            if runtime.reminders.settings.include_todos:
                await runtime.reminders.sync()
            state = runtime.reminders.status()
            if (sorted(item["uid"] for item in state["items"]) != sorted(value.expected_items)
                    or state["tts_cache_version"] != value.expected_version):
                raise HTTPException(409, "出门提醒或待办已变化，请刷新后重试")
            if (state["items"] or state["tts_cache_version"]) and not runtime.reminders.ready:
                raise HTTPException(409, "请等待当前提醒完成合成和 ESP32 缓存")
            frame = runtime.frames.fresh()
            if frame is None:
                raise HTTPException(503, "摄像头尚无新画面")
            created = time.time()
            event = {"event_id": key, "event_type": "exit", "source": "simulation",
                     "track_id": 0, "confidence": 0, "created": created,
                     "timestamp": datetime.fromtimestamp(created, UTC).isoformat()}
            await runtime.event(event, frame, frame.jpeg)
            return {"accepted": True, "event_id": key, "duplicate": False}

    @admin.get("/api/events/{event_id}/snapshot")
    async def event_snapshot(event_id: UUID, raw: bool = False):
        path = runtime.event_dir / f"{event_id}-{'raw' if raw else 'overlay'}.jpg"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    @admin.get("/api/reminders")
    async def reminders():
        return runtime.reminders.status()

    @admin.put("/api/reminders")
    async def configure_reminders(value: DepartureSettings):
        runtime.reminders.configure(value)
        return runtime.reminders.status()

    @admin.get("/api/arrival")
    async def arrival():
        return {**runtime.arrival.status(), "detection": runtime.arrival_detection_status()}

    @admin.put("/api/arrival")
    async def configure_arrival(value: ArrivalSettings):
        runtime.arrival.configure(value)
        return runtime.arrival.status()

    @admin.post("/api/arrival/regenerate", status_code=202)
    async def regenerate_arrival():
        runtime.arrival.audio = runtime.arrival.device_version = runtime.arrival.cache_command = None
        runtime.arrival.request_sync(force=True)
        return {"accepted": True}

    @admin.post("/api/arrival/reference")
    async def capture_reference(value: DoorReferenceCapture):
        try:
            return await runtime.capture_door_reference(value.state)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except TimeoutError:
            raise HTTPException(503, "采集超时，请检查摄像头画面") from None

    @admin.post("/api/events/simulate-enter", status_code=202)
    async def simulate_enter(value: ArrivalSimulation):
        async with simulation_lock:
            key = str(value.event_id)
            previous = runtime.store.event(key)
            if previous:
                if previous.get("source") != "simulation" or previous["event_type"] != "enter":
                    raise HTTPException(409, "事件编号已被使用")
                return {"accepted": True, "event_id": key, "duplicate": True}
            if not (runtime.mqtt.connected and runtime.ha.connected and runtime.arrival.device_online):
                raise HTTPException(503, "请等待 HA、MQTT 和 ESP32 全部连接")
            if not runtime.arrival.enabled:
                raise HTTPException(409, "请先启用进门播报")
            now = time.time()
            if any(row.get("source") == "simulation" and now-row["created"] < 10
                   for row in runtime.store.events()):
                raise HTTPException(429, "请等待 10 秒后再模拟")
            if runtime.store.playback_busy(now):
                raise HTTPException(409, "上一条播放仍在处理，请稍后再试")
            state = runtime.arrival.status()
            if state["tts_cache_version"] != value.expected_version:
                raise HTTPException(409, "欢迎语版本已变化，请刷新后重试")
            if state["tts_cache_version"] and not runtime.arrival.ready:
                raise HTTPException(409, "请等待欢迎语完成 ESP32 缓存")
            frame = runtime.frames.fresh()
            if frame is None:
                raise HTTPException(503, "摄像头尚无新画面")
            event = {"event_id": key, "event_type": "enter", "source": "simulation",
                     "track_id": 0, "confidence": 0, "created": now,
                     "timestamp": datetime.fromtimestamp(now, UTC).isoformat()}
            await runtime.event(event, frame, frame.jpeg)
            return {"accepted": True, "event_id": key, "duplicate": False}

    @admin.post("/api/reminders/sync", status_code=202)
    async def sync():
        runtime.reminders.request_sync()
        return {"accepted": True}

    @admin.post("/api/reminders/regenerate", status_code=202)
    async def regenerate():
        runtime.reminders.audio = runtime.reminders.device_version = runtime.reminders.cache_command = None
        runtime.reminders.request_sync(force=True)
        return {"accepted": True}

    @admin.get("/api/settings")
    async def settings():
        return runtime.settings.model_dump()

    @admin.put("/api/settings")
    async def configure(value: Settings):
        try:
            await runtime.configure(value)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return value.model_dump()

    @admin.get("/api/logs")
    async def logs():
        return list(runtime.log_buffer.lines)

    admin.mount("/static", StaticFiles(directory=STATIC), name="static")
    return media, admin
