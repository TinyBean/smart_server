"""Qwen-Audio TTS task protocol with a local, atomically published PCM16 WAV cache."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
import wave
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from .tts import describe_wav


class QwenTTSError(RuntimeError):
    """Safe to show to the user; never includes server messages or credentials."""


class QwenTTS:
    timeout = 90
    max_pcm_bytes = 16000 * 2 * 180
    parameters = {"text_type": "PlainText", "format": "pcm", "sample_rate": 16000,
                  "volume": 50, "rate": 1.0, "pitch": 1.0, "enable_ssml": False}

    def __init__(self, cache: Path, *, api_key: str, url: str, model: str, voice: str):
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        self._api_key = api_key.strip()
        self.url, self.model, self.voice = url, model, voice
        self.last_ms = 0.0
        self._lock = asyncio.Lock()

    @property
    def configuration_error(self):
        if not self._api_key:
            return "请在 HA 应用配置中填写 qwen_api_key 并重启应用"
        return None

    async def prepare(self, text, force=False):
        if not text.strip() or len(text) > 1500:
            raise ValueError("合成文字须为 1..1500 字")
        signature = ["qwen-pcm16-v1", text, self.url, self.model, self.voice, self.parameters]
        key = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = self.cache / f"{key}.wav"
        started = time.monotonic()
        async with self._lock:
            if path.exists() and not force:
                try:
                    info = await asyncio.to_thread(describe_wav, path)
                except (OSError, ValueError, EOFError, wave.Error):
                    pass  # Replace a corrupt cache only after a complete new synthesis.
                else:
                    self.last_ms = (time.monotonic()-started)*1000
                    return {"version": key, **info}
            if self.configuration_error:
                raise QwenTTSError(self.configuration_error)
            temporary = self.cache / f"{key}.{uuid.uuid4().hex}.tmp.wav"
            try:
                async with asyncio.timeout(self.timeout):
                    await self._synthesize(text, temporary)
                info = await asyncio.to_thread(describe_wav, temporary)
                os.replace(temporary, path)
            except asyncio.CancelledError:
                raise
            except QwenTTSError:
                raise
            except TimeoutError:
                raise QwenTTSError("阿里语音合成超时，请检查网络后重试") from None
            except InvalidStatus as exc:
                if exc.response.status_code in {401, 403}:
                    raise QwenTTSError("阿里语音鉴权失败，请检查 API Key、地域和模型权限") from None
                raise QwenTTSError("阿里语音连接失败，请稍后重试") from None
            except Exception:
                # Upstream errors can echo input text or headers; don't expose them.
                raise QwenTTSError("阿里语音合成未完成，请检查配置和网络后重试") from None
            finally:
                temporary.unlink(missing_ok=True)
                self.last_ms = (time.monotonic()-started)*1000
            return {"version": key, **info}

    async def _synthesize(self, text, path):
        task_id = str(uuid.uuid4())

        def command(action, payload):
            return json.dumps({"header": {"action": action, "task_id": task_id, "streaming": "duplex"},
                               "payload": payload}, ensure_ascii=False)

        async with connect(self.url, additional_headers={"Authorization": f"Bearer {self._api_key}"},
                           open_timeout=15, close_timeout=2, max_size=self.max_pcm_bytes, max_queue=4,
                           proxy=None) as socket:
            await socket.send(command("run-task", {
                "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                "model": self.model, "parameters": {**self.parameters, "voice": self.voice}, "input": {},
            }))
            active = False
            byte_count = 0
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                async for message in socket:
                    if isinstance(message, bytes):
                        if not active or byte_count+len(message) > self.max_pcm_bytes:
                            raise QwenTTSError("阿里语音数据异常或音频超过 180 秒")
                        audio.writeframesraw(message)
                        byte_count += len(message)
                        continue
                    event = json.loads(message)
                    header = event["header"]
                    if header.get("task_id") != task_id:
                        raise QwenTTSError("阿里语音任务回执不匹配")
                    kind = header.get("event")
                    if kind == "task-failed":
                        raise QwenTTSError("阿里语音合成失败，请检查模型、音色权限及账户额度")
                    if kind == "task-started" and not active:
                        active = True
                        await socket.send(command("continue-task", {"input": {"text": text}}))
                        await socket.send(command("finish-task", {"input": {}}))
                    elif kind == "task-finished" and active:
                        if not byte_count or byte_count % 2:
                            raise QwenTTSError("阿里语音返回了空音频或不完整的 PCM 数据")
                        return
                    elif kind != "result-generated" or not active:
                        raise QwenTTSError("阿里语音任务顺序异常")
            raise QwenTTSError("阿里语音连接提前关闭，音频未完成")
