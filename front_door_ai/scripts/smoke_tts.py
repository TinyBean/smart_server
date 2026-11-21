"""Exercise the real Piper subprocess and verify a reusable PCM16/16kHz cache."""
import argparse
import asyncio
import json
from pathlib import Path

from app.tts import PiperTTS, text_for


async def verify(model, cache):
    tts = PiperTTS(model, cache)
    text = text_for([{"text": "带雨伞"}, {"text": "取快递"}])
    first = await tts.prepare(text, force=True)
    synthesis_ms = tts.last_ms
    path = cache / f"{first['version']}.wav"
    timestamp = path.stat().st_mtime_ns
    second = await tts.prepare(text)
    assert first == second and path.stat().st_mtime_ns == timestamp
    assert not list(cache.glob("*.tmp.wav"))
    print(json.dumps({"passed": True, "synthesis_ms": round(synthesis_ms, 1),
                      "cache_ms": round(tts.last_ms, 1), "file": str(path), **first}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Local Piper ONNX; matching .onnx.json is required")
    parser.add_argument("--cache", type=Path, required=True, help="A disposable verification cache directory")
    args = parser.parse_args()
    asyncio.run(verify(args.model, args.cache))
