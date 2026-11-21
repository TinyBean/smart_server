import asyncio
import hashlib
import json
import wave

import pytest
from pydantic import ValidationError
from websockets.asyncio.server import serve

from app.config import Connections
from app.qwen_tts import QwenTTS, QwenTTSError
from app.runtime import Runtime
from app.tts import text_for


class SpeechServer:
    def __init__(self):
        self.calls = []
        self.connections = 0
        self.mode = "success"
        self.started = asyncio.Event()
        self.pcm = b"\x01\x00" * 1600

    async def handler(self, socket):
        self.connections += 1
        assert socket.request.headers["Authorization"] == "Bearer test-secret"
        run = json.loads(await socket.recv())
        self.calls.append(run)
        key = run["header"]["task_id"]

        async def event(kind, **fields):
            await socket.send(json.dumps({"header": {"event": kind, "task_id": key, **fields}, "payload": {}}))

        if self.mode == "mismatch":
            await event("task-started", task_id="another-task")
            return
        if self.mode == "early-audio":
            await socket.send(self.pcm)
            return
        await event("task-started")
        for action in ("continue-task", "finish-task"):
            message = json.loads(await socket.recv())
            assert message["header"] == {"action": action, "task_id": key, "streaming": "duplex"}
            self.calls.append(message)
        if self.mode == "wait":
            self.started.set()
            await socket.wait_closed()
            return
        if self.mode != "empty":
            # Frame boundaries need not coincide with 16-bit sample boundaries.
            await socket.send(self.pcm[:3])
            await socket.send(self.pcm[3:])
        if self.mode == "failed":
            await event("task-failed", error_code="InvalidParameter", error_message="test-secret private reminder")
        elif self.mode != "disconnect":
            await event("result-generated")
            await event("task-finished")


@pytest.fixture
async def speech(tmp_path):
    fake = SpeechServer()

    def authenticate(connection, request):
        if fake.mode == "unauthorized":
            return connection.respond(401, "private server error")
        return None

    async with serve(fake.handler, "127.0.0.1", 0, process_request=authenticate) as server:
        port = server.sockets[0].getsockname()[1]
        config = Connections()
        tts = QwenTTS(tmp_path / "cache", api_key="test-secret", url=f"ws://127.0.0.1:{port}",
                      model=config.qwen_tts_model, voice=config.qwen_tts_voice)
        yield tts, fake


async def test_complete_cloud_speech_and_cache_reuse(speech):
    tts, fake = speech
    text = text_for([{"text": "带雨伞"}])
    first = await tts.prepare(text)
    run, content, finish = fake.calls
    assert run["header"]["action"] == "run-task"
    assert run["payload"]["model"] == "qwen-audio-3.1-tts-flash"
    assert run["payload"]["parameters"]["voice"] == "longjielidou_v3.1"
    assert run["payload"]["parameters"]["format"] == "pcm"
    assert run["payload"]["parameters"]["sample_rate"] == 16000
    assert content["payload"] == {"input": {"text": "出门别忘了带手机和钥匙。提醒你：带雨伞。"}}
    assert finish["payload"] == {"input": {}}
    path = tts.cache / f"{first['version']}.wav"
    with wave.open(str(path), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert audio.readframes(audio.getnframes()) == fake.pcm
    assert first["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first["duration"] == .1
    modified = path.stat().st_mtime_ns
    assert await tts.prepare(text) == first
    assert fake.connections == 1 and path.stat().st_mtime_ns == modified
    assert await tts.prepare(text, force=True) == first
    assert fake.connections == 2
    assert not list(tts.cache.glob("*.tmp.wav"))


async def test_text_and_voice_change_invalidate_cache(speech):
    tts, fake = speech
    first = await tts.prepare("带雨伞")
    second = await tts.prepare("取快递")
    tts.voice = "longanhuan_v3.1"
    third = await tts.prepare("取快递")
    assert len({result["version"] for result in (first, second, third)}) == 3
    assert fake.connections == 3


async def test_concurrent_identical_requests_only_synthesize_once(speech):
    tts, fake = speech
    first, second = await asyncio.gather(tts.prepare("test"), tts.prepare("test"))
    assert first == second
    assert fake.connections == 1


async def test_authentication_failure_is_safe_and_actionable(speech):
    tts, fake = speech
    fake.mode = "unauthorized"
    with pytest.raises(QwenTTSError, match="API Key") as error:
        await tts.prepare("private reminder")
    assert "private" not in str(error.value)
    assert "test-secret" not in str(error.value)
    assert list(tts.cache.iterdir()) == []


@pytest.mark.parametrize("mode", ["empty", "failed", "disconnect", "mismatch", "early-audio"])
async def test_incomplete_tasks_never_publish_audio(speech, mode):
    tts, fake = speech
    fake.mode = mode
    with pytest.raises(QwenTTSError) as error:
        await tts.prepare("private reminder")
    assert "test-secret" not in str(error.value)
    assert "private reminder" not in str(error.value)
    assert list(tts.cache.iterdir()) == []


async def test_invalid_pcm_length_is_rejected(speech):
    tts, fake = speech
    fake.pcm += b"\x00"
    with pytest.raises(QwenTTSError, match="PCM"):
        await tts.prepare("test")
    assert list(tts.cache.iterdir()) == []


async def test_audio_duration_is_bounded(speech):
    tts, _ = speech
    tts.max_pcm_bytes = 1000
    with pytest.raises(QwenTTSError):
        await tts.prepare("test")
    assert list(tts.cache.iterdir()) == []


async def test_timeout_and_cancellation_clean_up(speech):
    tts, fake = speech
    fake.mode = "wait"
    tts.timeout = .2
    with pytest.raises(QwenTTSError, match="超时"):
        await tts.prepare("test")
    assert list(tts.cache.iterdir()) == []
    tts.timeout = 90
    fake.started.clear()
    pending = asyncio.create_task(tts.prepare("test"))
    await asyncio.wait_for(fake.started.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert list(tts.cache.iterdir()) == []


async def test_failed_force_preserves_previous_file(speech):
    tts, fake = speech
    first = await tts.prepare("test")
    path = tts.cache / f"{first['version']}.wav"
    previous = path.read_bytes()
    fake.mode = "failed"
    with pytest.raises(QwenTTSError):
        await tts.prepare("test", force=True)
    assert path.read_bytes() == previous
    assert not list(tts.cache.glob("*.tmp.wav"))


async def test_corrupt_cache_is_rebuilt(speech):
    tts, fake = speech
    first = await tts.prepare("test")
    path = tts.cache / f"{first['version']}.wav"
    path.write_bytes(path.read_bytes()[:-20])
    assert await tts.prepare("test") == first
    assert fake.connections == 2


async def test_missing_key_does_not_connect(speech):
    tts, fake = speech
    tts._api_key = ""
    with pytest.raises(QwenTTSError, match="qwen_api_key"):
        await tts.prepare("test")
    assert fake.connections == 0


async def test_runtime_selects_qwen_without_exposing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    (tmp_path / "options.json").write_text(json.dumps({"tts_provider": "qwen"}), encoding="utf-8")
    runtime = Runtime(tmp_path, dev=True)
    try:
        assert isinstance(runtime.tts, QwenTTS)
        assert runtime.tts.configuration_error is None
        assert runtime.reminders.status()["tts_provider"] == "qwen"
        assert "test-secret" not in json.dumps(runtime.reminders.status())
    finally:
        await runtime.close()


async def test_missing_key_is_visible_even_without_reminders(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    (tmp_path / "options.json").write_text(json.dumps({"tts_provider": "qwen"}), encoding="utf-8")
    runtime = Runtime(tmp_path, dev=True)
    try:
        assert "qwen_api_key" in runtime.reminders.status()["error"]
        assert not runtime.reminders.status()["tts_ready"]
    finally:
        await runtime.close()


def test_api_key_is_redacted():
    config = Connections(qwen_api_key="test-secret")
    assert "test-secret" not in repr(config)
    assert "test-secret" not in config.model_dump_json()
    assert config.qwen_api_key.get_secret_value() == "test-secret"


@pytest.mark.parametrize("url", ["ws://dashscope.aliyuncs.com/api-ws/v1/inference",
                                 "wss://example.com/api-ws/v1/inference",
                                 "wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/inference?api_key=secret"])
def test_invalid_endpoints_rejected(url):
    with pytest.raises(ValidationError):
        Connections(qwen_tts_url=url)


def test_official_workspace_endpoint_accepted():
    url = "wss://workspace123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    assert Connections(qwen_tts_url=url).qwen_tts_url == url
