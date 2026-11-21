"""Synthesize a harmless sample and verify cache reuse; never plays audio or changes HA to-dos."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from app.config import Connections
from app.qwen_tts import QwenTTS
from app.tts import text_for


async def verify(config, cache):
    tts = QwenTTS(cache, api_key=config.qwen_api_key.get_secret_value() or os.getenv("DASHSCOPE_API_KEY", ""),
                  url=config.qwen_tts_url, model=config.qwen_tts_model, voice=config.qwen_tts_voice)
    text = text_for([{"text": "语音合成接入测试"}])
    first = await tts.prepare(text)
    synthesis_ms = tts.last_ms
    path = cache / f"{first['version']}.wav"
    timestamp = path.stat().st_mtime_ns
    second = await tts.prepare(text)
    assert first == second and path.stat().st_mtime_ns == timestamp
    print(json.dumps({"passed": True, "model": tts.model, "voice": tts.voice,
                      "synthesis_ms": round(synthesis_ms, 1), "cache_ms": round(tts.last_ms, 1),
                      "file": str(path), **first}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--options", type=Path, help="HA options.json; otherwise use DASHSCOPE_API_KEY")
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    config = Connections.model_validate_json(args.options.read_text("utf-8")) if args.options else Connections()
    asyncio.run(verify(config, args.cache))
