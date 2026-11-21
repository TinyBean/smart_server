from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator, model_validator

Coordinate = Annotated[float, Field(ge=0, le=1)]
Point = tuple[Coordinate, Coordinate]


class Settings(BaseModel):
    """Ingress-owned settings. Connection secrets are deliberately a separate type."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    confidence_threshold: float = Field(0.5, ge=0.1, le=1)
    detection_fps: float = Field(5, ge=1, le=15)
    minimum_track_age: float = Field(0.5, ge=0, le=10)
    minimum_movement: float = Field(0.05, ge=0, le=1)
    event_cooldown: float = Field(5, ge=0, le=60)
    tracking_point: Literal["bottom_center", "center"] = "bottom_center"
    entry_detection_mode: Literal["zone_crossing", "door_reveal"] = "zone_crossing"
    door_zone: list[Point] = Field(default_factory=list, max_length=20)
    arrival_zone: list[Point] = Field(default_factory=list, max_length=20)
    outside_zone: list[Point] = Field(default_factory=list, max_length=20)
    transition_zone: list[Point] = Field(default_factory=list, max_length=20)
    inside_zone: list[Point] = Field(default_factory=list, max_length=20)
    departure_path: list[Point] = Field(default_factory=list, max_length=2)
    departure_path_tolerance: float = Field(0.06, ge=0.01, le=0.25)
    snapshot_retention_days: int = Field(7, ge=1, le=7)
    snapshot_max_events: int = Field(500, ge=1, le=500)

    @field_validator("departure_path")
    @classmethod
    def valid_departure_path(cls, points):
        if not points:
            return points
        if len(points) != 2:
            raise ValueError("出门路线需要两个点：摄像头旁起点、门口终点；清空可停用")
        from math import hypot
        if hypot(points[1][0]-points[0][0], points[1][1]-points[0][1]) < 0.1:
            raise ValueError("出门路线太短，请拉开摄像头旁起点与门口终点")
        return points

    @field_validator("outside_zone", "transition_zone", "inside_zone", "door_zone", "arrival_zone")
    @classmethod
    def valid_polygon(cls, points):
        if not points:
            return points
        if len(points) < 3 or len(set(points)) != len(points):
            raise ValueError("区域至少需要三个不同顶点")
        from .geometry import polygon_valid
        if not polygon_valid(points):
            raise ValueError("区域不能自交或退化成线")
        return points

    @model_validator(mode="after")
    def disjoint_zones(self):
        from .geometry import polygons_overlap
        zones = [self.outside_zone, self.transition_zone, self.inside_zone]
        for i, first in enumerate(zones):
            for second in zones[i + 1:]:
                if first and second and polygons_overlap(first, second):
                    raise ValueError("区域内部不能重叠，可以共享边界")
        return self

    @property
    def calibrated(self) -> bool:
        return bool(self.outside_zone and self.transition_zone and self.inside_zone)


class ArrivalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    text: str = Field("欢迎回家。", min_length=1, max_length=1500)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("欢迎语不能为空")
        return value


class DepartureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    text: str = Field("出门别忘了带手机和钥匙。", min_length=1, max_length=1500)
    include_todos: bool = True

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("出门提醒不能为空")
        return value


class Connections(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_url: str = ""
    mqtt_host: str = "core-mosquitto"
    mqtt_port: int = Field(1883, ge=1, le=65535)
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    device_id: str = Field("frontdoor", pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    mqtt_prefix: str = Field("home/frontdoor", pattern=r"^[a-zA-Z0-9_/-]{1,128}$")
    public_base_url: str = ""
    todo_entity: str = Field("todo.departure_reminders", pattern=r"^todo\.[a-z0-9_]+$")
    tts_provider: Literal["piper", "qwen"] = "piper"
    qwen_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    qwen_tts_url: str = "wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/inference"
    qwen_tts_model: str = Field("qwen-audio-3.1-tts-flash", min_length=1, max_length=128)
    qwen_tts_voice: str = Field("longjielidou_v3.1", min_length=1, max_length=128)
    piper_model: str = "/config/piper/voice.onnx"
    detector_model: str = "/models/person-detection-retail-0013.xml"

    @field_validator("qwen_tts_url")
    @classmethod
    def qwen_url(cls, value):
        import re
        from urllib.parse import urlsplit
        url = urlsplit(value)
        host = url.hostname or ""
        official = (host in {"cn-beijing.maas.qianwenaiapi.com", "dashscope.aliyuncs.com",
                             "dashscope-intl.aliyuncs.com"}
                    or re.fullmatch(r"[a-zA-Z0-9-]+\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com", host))
        if (url.scheme != "wss" or not official or url.username or url.password or url.query or url.fragment
                or url.port not in {None, 443} or url.path != "/api-ws/v1/inference"):
            raise ValueError("请使用千问平台或阿里云百炼官方 WSS inference 地址")
        return value

    @field_validator("camera_url", "public_base_url")
    @classmethod
    def http_url(cls, value, info: ValidationInfo):
        from urllib.parse import urlsplit
        if value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("地址必须为不含凭据的 HTTP/HTTPS URL")
            if url.fragment:
                raise ValueError("地址不能含 fragment")
            if info.field_name == "public_base_url" and url.query:
                raise ValueError("下载基础地址不能含 query")
        return value.rstrip("/")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def load_config(data_dir: Path) -> tuple[Connections, Settings]:
    options = data_dir / "options.json"
    settings = data_dir / "config.json"
    return (
        Connections.model_validate_json(options.read_text("utf-8")) if options.exists() else Connections(),
        Settings.model_validate_json(settings.read_text("utf-8")) if settings.exists() else Settings(),
    )
