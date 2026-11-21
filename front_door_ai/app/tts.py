from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import wave
from pathlib import Path

# Match the fixed goodbye prompt installed on the device.
DEPARTURE_GREETING = "出门别忘了带手机和钥匙。"


def describe_wav(path):
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("WAV format must be 16kHz mono PCM16")
        frames = audio.getnframes()
        duration = frames/16000
        if not 0 < duration <= 180:
            raise ValueError("WAV duration must be 0..180 seconds")
        if len(audio.readframes(frames)) != frames*2:
            raise ValueError("WAV audio is truncated")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "duration": duration,
            "size": path.stat().st_size}


def text_for(items, greeting=DEPARTURE_GREETING):
    parts = [" ".join(item["text"].split()) for item in items]
    parts = [part for part in parts if part]
    text = greeting
    if parts:
        reminders = parts[0] if len(parts) == 1 else "，".join(parts[:-1]) + "，还有" + parts[-1]
        separator = "" if text.endswith(("。", "！", "？", ".", "!", "?", "；", ";")) else "。"
        text += separator + "提醒你：" + reminders + "。"
    if len(text) > 1500:
        raise ValueError("完整播报超过 1500 字，请缩短出门提醒或 HA 待办")
    return text


class PiperTTS:
    settings = {"sample_rate": 16000, "sample_width": 2, "channels": 1, "length_scale": 1.0}

    def __init__(self, model: str, cache: Path):
        self.model, self.cache = Path(model), cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self._signature = None
        self._fingerprint = None
        self.last_ms = 0.0

    def fingerprint(self):
        paths = [self.model, Path(str(self.model)+".json")]
        signature = tuple((p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
        if signature != self._signature:
            digest = hashlib.sha256()
            for path in paths:
                with path.open("rb") as file:
                    for block in iter(lambda: file.read(1024*1024), b""):
                        digest.update(block)
            self._signature, self._fingerprint = signature, digest.hexdigest()
        return self._fingerprint

    async def prepare(self, text, force=False):
        fingerprint = await asyncio.to_thread(self.fingerprint)
        key = hashlib.sha256(json.dumps([text, fingerprint, self.settings], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = self.cache / f"{key}.wav"
        started = time.monotonic()
        if force or not path.exists():
            temporary = self.cache / f"{key}.tmp.wav"
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.tts_worker", str(self.model), str(temporary),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(process.communicate(json.dumps({"text": text}).encode()), 90)
                if process.returncode != 0:
                    raise RuntimeError("Piper synthesis failed; check local voice model and piper installation")
                self.describe(temporary)
                os.replace(temporary, path)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                temporary.unlink(missing_ok=True)
        info = await asyncio.to_thread(self.describe, path)
        self.last_ms = (time.monotonic()-started)*1000
        return {"version": key, **info}

    describe = staticmethod(describe_wav)
