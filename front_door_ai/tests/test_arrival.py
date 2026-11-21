import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from conftest import event, prepare

from app.arrival import ArrivalManager
from app.config import ArrivalSettings


async def ready_arrival(rig):
    departure, store, ha, mqtt, tts = rig
    arrival = ArrivalManager(store, ha, mqtt, tts, departure.config)
    await arrival.sync()
    await arrival.message("device/availability", "online")
    arrival.audio_status({"command_id": arrival.cache_command, "audio_id": "arrival",
                          "version": arrival.audio["version"], "status": "ready"})
    return arrival


def receipt(row, status="finished", **overrides):
    return {"command_id": row["command_id"], "audio_id": row["audio_id"],
            "version": row["version"], "status": status, **overrides}


async def test_welcome_does_not_read_or_complete_todos(rig, monkeypatch):
    departure, store, ha, _, _ = rig
    monkeypatch.setattr(ha, "items", AsyncMock(side_effect=AssertionError("arrival read to-do")))
    ha.connected = False
    arrival = await ready_arrival(rig)
    assert arrival.ready
    eid = event(store, kind="enter")
    assert arrival.play(eid) == "requested"
    row = store.latest_playback("arrival")
    assert row["items"] == []
    arrival.audio_status(receipt(row))
    await departure.reconcile()
    assert store.playback(row["command_id"])["state"] == "finished"
    await arrival.reconcile()
    assert store.playback(row["command_id"])["state"] == "complete"
    assert not ha.completed and store.latest_playback() is None
    assert arrival.play(eid) == "duplicate"


async def test_channels_share_busy_lock_and_receipts_cannot_cross(rig):
    departure, store, ha, _, _ = rig
    await prepare(rig)
    arrival = await ready_arrival(rig)
    assert departure.play(event(store)) == "requested"
    row = store.latest_playback()
    arrival.audio_status(receipt(row, audio_id="arrival"))
    assert store.playback(row["command_id"])["state"] == "requested"
    assert arrival.play(event(store, kind="enter")) == "busy_or_offline"
    assert not ha.completed
    departure.audio_status(receipt(row))
    await departure.reconcile()
    assert ha.completed == ["a"]
    assert arrival.play(event(store, kind="enter")) == "requested"
    assert departure.play(event(store)) == "busy_or_offline"


async def test_changed_text_invalidates_and_stale_synthesis_discarded(rig):
    arrival = await ready_arrival(rig)
    original = arrival.audio["version"]
    started, release = asyncio.Event(), asyncio.Event()
    real_prepare = arrival.tts.prepare

    async def slow(text, force=False):
        started.set()
        await release.wait()
        return await real_prepare(text, force=force)

    arrival.tts.prepare = slow
    arrival.configure(ArrivalSettings(text="第一版欢迎语"))
    task = asyncio.create_task(arrival.sync())
    await started.wait()
    arrival.configure(ArrivalSettings(text="第二版欢迎语"))
    release.set()
    await task
    assert arrival.audio is None and not arrival.ready
    await arrival.sync()
    assert arrival.audio["version"] != original and arrival.text == "第二版欢迎语"
    assert not arrival.ready


async def test_fallback_disabled_expiry_and_cross_direction(rig):
    departure, store, ha, mqtt, tts = rig
    arrival = ArrivalManager(store, ha, mqtt, tts, departure.config)
    arrival.device_online = True
    assert arrival.play(event(store)) == "expired_event"
    assert arrival.play(event(store, kind="enter", age=11)) == "expired_event"
    arrival.configure(ArrivalSettings(enabled=False))
    assert arrival.play(event(store, kind="enter")) == "disabled"
    arrival.configure(ArrivalSettings())
    assert arrival.play(event(store, kind="enter")) == "fallback"
    assert mqtt.messages[-1][1]["sound"] == "welcome"
    assert arrival.play(event(store, kind="enter")) == "busy_or_offline"
    assert not ha.completed


async def test_reconnect_rechecks_cache_and_acknowledges_stale_receipt(rig):
    arrival = await ready_arrival(rig)
    old, version = arrival.cache_command, arrival.audio["version"]
    arrival.mqtt_connected()
    await arrival.sync()
    await arrival.message("device/availability", "online")
    assert old != arrival.cache_command
    arrival.audio_status({"command_id": old, "version": version, "audio_id": "arrival", "status": "ready"})
    assert not arrival.ready
    assert rig[3].messages[-1][1]["command"] == "ack_audio_status"
    arrival.audio_status({"command_id": arrival.cache_command, "version": version,
                          "audio_id": "arrival", "status": "ready"})
    assert arrival.ready


async def test_arrival_restart_and_late_failed_receipt_preserve_todos(rig):
    arrival = await ready_arrival(rig)
    _, store, ha, mqtt, tts = rig
    arrival.play(event(store, kind="enter"))
    row = store.latest_playback("arrival")
    store.update_playback(row["command_id"], "timed_out")
    restarted = ArrivalManager(store, ha, mqtt, tts, arrival.config)
    restarted.audio_status(receipt(row))
    await restarted.reconcile()
    assert store.playback(row["command_id"])["state"] == "complete" and not ha.completed
    assert restarted.settings.text == "欢迎回家。"


async def test_expired_fallback_releases_shared_lock(rig):
    arrival = await ready_arrival(rig)
    arrival.audio = None
    store = rig[1]
    arrival.play(event(store, kind="enter"))
    with store.db:
        store.db.execute("UPDATE playback SET deadline=?", (time.time()-1,))
    assert not store.playback_busy()


def test_blank_welcome_rejected():
    with pytest.raises(ValueError):
        ArrivalSettings(text=" \n\t ")
