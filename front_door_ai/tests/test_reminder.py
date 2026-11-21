import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from conftest import ack, event, item, prepare

from app.config import DepartureSettings
from app.reminder import ReminderManager
from app.store import Store
from app.tts import text_for


async def test_departure_synthesizes_goodbye_and_reminders_together(rig, monkeypatch):
    manager, store, ha, mqtt, tts = rig
    synthesize = AsyncMock(wraps=tts.prepare)
    monkeypatch.setattr(tts, "prepare", synthesize)
    await prepare(rig, [item(), item("b", "带雨伞")])
    text = "出门别忘了带手机和钥匙。提醒你：带钥匙，还有带雨伞。"
    synthesize.assert_awaited_once_with(text, force=False)
    assert manager.status()["text"] == text
    assert not ha.completed

    assert manager.play(event(store)) == "requested"
    commands = [payload for _, payload, _ in mqtt.messages
                if payload.get("command") in {"play", "play_cached"}]
    assert len(commands) == 1
    assert commands[0]["command"] == "play_cached"
    assert commands[0]["version"] == manager.audio["version"]


@pytest.mark.parametrize("items", [[], [item(text=" \n\t ")]])
async def test_empty_reminders_synthesize_editable_goodbye(rig, monkeypatch, items):
    manager, store, ha, mqtt, tts = rig
    synthesize = AsyncMock(wraps=tts.prepare)
    monkeypatch.setattr(tts, "prepare", synthesize)
    manager.configure(DepartureSettings(text="一路顺风。"))
    await prepare(rig, items)
    synthesize.assert_awaited_once_with("一路顺风。", force=False)
    assert manager.text == "一路顺风。"
    assert manager.play(event(store)) == "requested"
    assert mqtt.messages[-1][1]["command"] == "play_cached"
    assert not ha.completed


async def test_clock_precedes_cache_and_play(rig):
    manager, store, _, mqtt, _ = rig
    await prepare(rig)
    commands = [p for topic, p, _ in mqtt.messages if topic == "device/command"]
    cache_index = next(i for i, p in enumerate(commands) if p["command"] == "cache_audio")
    assert commands[cache_index-1]["command"] == "sync_time"
    assert abs(commands[cache_index-1]["unix_time"]-time.time()) < 5
    manager.play(event(store))
    assert mqtt.messages[-2][1]["command"] == "sync_time"
    assert mqtt.messages[-1][1]["command"] == "play_cached"


async def test_finished_only_completes_frozen_batch(rig):
    manager, store, ha, mqtt, _ = rig
    await prepare(rig)
    eid = event(store)
    assert manager.play(eid) == "requested"
    row = store.playbacks()[0]
    ha.data.append(item("b", "带雨伞"))
    manager.audio_status(ack(row, "playing"))
    await manager.reconcile()
    assert ha.completed == []
    manager.audio_status(ack(row))
    await manager.reconcile()
    assert ha.completed == ["a"]
    assert ha.data[1]["status"] == "needs_action"
    assert store.playback(row["command_id"])["state"] == "complete"
    assert all(not options.get("retain", False) for topic, _, options in mqtt.messages if topic == "device/command")


async def test_duplicates_and_unknown_ack(rig):
    manager, store, ha, mqtt, _ = rig
    await prepare(rig)
    eid = event(store)
    manager.play(eid)
    row = store.playbacks()[0]
    assert manager.play(eid) == "duplicate"
    manager.audio_status(ack(row, version="wrong"))
    manager.audio_status(ack(row, command_id="unknown"))
    manager.audio_status({"audio_id": "departure", "status": "finished"})
    await manager.reconcile()
    assert ha.completed == []
    manager.audio_status(ack(row))
    manager.audio_status(ack(row))
    await manager.reconcile()
    manager.audio_status(ack(row))
    await manager.reconcile()
    assert ha.completed == ["a"]
    assert len([p for _, p, _ in mqtt.messages if p.get("command") == "play_cached"]) == 1


async def test_rename_remove_and_duplicate_text(rig):
    manager, store, ha, _, _ = rig
    await prepare(rig, [item("a"), item("b"), item("c", "文件")])
    manager.play(event(store))
    row = store.playbacks()[0]
    ha.data[0]["text"] = "新任务"
    ha.data.pop(2)
    ha.data.append(item("d"))
    manager.audio_status(ack(row))
    await manager.reconcile()
    assert ha.completed == ["b"]
    assert ha.data[0]["status"] == "needs_action"
    assert ha.data[-1]["status"] == "needs_action"


async def test_failed_playback_keeps_reminders(rig):
    manager, store, ha, _, _ = rig
    await prepare(rig)
    manager.play(event(store))
    manager.audio_status(ack(store.playbacks()[0], "error"))
    await manager.reconcile()
    assert not ha.completed
    manager.audio_status(ack(store.playbacks()[0]))
    await manager.reconcile()
    assert not ha.completed


async def test_partial_completion_retries_after_restart(rig, tmp_path):
    manager, store, ha, mqtt, tts = rig
    await prepare(rig, [item("a"), item("b")])
    manager.play(event(store))
    row = store.playbacks()[0]
    manager.audio_status(ack(row))
    ha.fail_uid = "b"
    with pytest.raises(ConnectionError):
        await manager.reconcile()
    assert ha.completed == ["a"]
    store.close()
    reopened = Store(tmp_path/"state.sqlite3")
    restarted = ReminderManager(reopened, ha, mqtt, tts, manager.config)
    ha.fail_uid = None
    try:
        await restarted.reconcile()
        assert ha.completed == ["a", "b"]
        assert reopened.playback(row["command_id"])["state"] == "complete"
        assert restarted.status()["last_reminder_playback"]["status"] == "complete"
    finally:
        reopened.close()


async def test_ha_down_after_finished_is_durable(rig):
    manager, store, ha, _, _ = rig
    await prepare(rig)
    manager.play(event(store))
    row = store.playbacks()[0]
    ha.connected = False
    manager.audio_status(ack(row))
    with pytest.raises(ConnectionError):
        await manager.reconcile()
    assert store.playback(row["command_id"])["state"] == "completing"
    ha.connected = True
    await manager.reconcile()
    assert ha.completed == ["a"]


async def test_fallback_for_not_ready_and_synthesis_failure(rig):
    manager, store, ha, mqtt, tts = rig
    ha.data = [item()]
    tts.fail = True
    with pytest.raises(RuntimeError):
        await manager.sync()
    manager.device_online = True
    assert manager.play(event(store)) == "fallback"
    assert mqtt.messages[-1][1]["sound"] == "goodbye"
    assert not ha.completed


async def test_fallback_serializes_departures(rig):
    manager, store, _, mqtt, _ = rig
    manager.device_online = True
    first = event(store)
    assert manager.play(first) == "fallback"
    assert manager.play(first) == "duplicate"
    assert manager.play(event(store)) == "busy_or_offline"
    assert len([p for _, p, _ in mqtt.messages if p.get("command") == "play"]) == 1
    store.db.execute("UPDATE playback SET deadline=?", (time.time()-1,))
    assert manager.play(event(store)) == "fallback"


async def test_cache_failure_retry_and_reconnect_stale_ack(rig):
    manager, _, _, _, _ = rig
    await prepare(rig)
    old_key = manager.cache_command
    version = manager.audio["version"]
    manager.mqtt_connected()
    assert not manager.ready
    await manager.sync()
    await manager.message("device/availability", "online")
    assert old_key != manager.cache_command
    manager.audio_status({"command_id": old_key, "audio_id": "departure", "version": version, "status": "ready"})
    assert not manager.ready
    manager.audio_status({"command_id": manager.cache_command, "audio_id": "departure", "version": version, "status": "error"})
    assert not manager.ready
    manager.cache_sent_at = time.time()-31
    last_key = manager.cache_command
    manager.ensure_cache()
    assert manager.cache_command != last_key


async def test_expiry_busy_and_late_superseded_ack(rig):
    manager, store, ha, _, _ = rig
    await prepare(rig)
    assert manager.play(event(store, age=12)) == "expired_event"
    assert manager.play(event(store, kind="enter")) == "expired_event"
    manager.play(event(store))
    row = store.playbacks()[0]
    assert manager.play(event(store)) == "busy_or_offline"
    store.update_playback(row["command_id"], "timed_out")
    assert manager.play(event(store)) == "requested"
    manager.audio_status(ack(row))
    await manager.reconcile()
    assert not ha.completed


async def test_late_ack_without_newer_transaction_is_accepted(rig):
    manager, store, ha, _, _ = rig
    await prepare(rig)
    manager.play(event(store))
    row = store.playbacks()[0]
    store.update_playback(row["command_id"], "timed_out")
    manager.audio_status(ack(row))
    await manager.reconcile()
    assert ha.completed == ["a"]


async def test_ack_after_recovery_window_cannot_complete_items(rig):
    manager, store, ha, _, _ = rig
    await prepare(rig)
    manager.play(event(store))
    row = store.playbacks()[0]
    with store.db:
        store.db.execute("UPDATE playback SET created=?, deadline=? WHERE command_id=?",
                         (time.time()-86500, time.time()-86400, row["command_id"]))
    manager.audio_status(ack(row))
    await manager.reconcile()
    assert ha.completed == []
    assert store.playback(row["command_id"])["state"] == "superseded"


async def test_edit_same_count_changes_cache(rig):
    manager, _, ha, _, _ = rig
    await prepare(rig)
    version = manager.audio["version"]
    ha.data[0]["text"] = "记得带电脑"
    await manager.sync()
    assert manager.audio["version"] != version
    assert not manager.ready


def test_text_builder():
    assert text_for([]) == "出门别忘了带手机和钥匙。"
    assert text_for([item(text=" \n\t ")]) == "出门别忘了带手机和钥匙。"
    assert text_for([item()]) == "出门别忘了带手机和钥匙。提醒你：带钥匙。"
    assert text_for([item(), item("b", "带雨伞")]) == "出门别忘了带手机和钥匙。提醒你：带钥匙，还有带雨伞。"
    assert text_for([item(text=" 带钥匙\n"), item("b", " "), item("c", "带雨伞")]) == (
        "出门别忘了带手机和钥匙。提醒你：带钥匙，还有带雨伞。"
    )
    with pytest.raises(ValueError):
        text_for([item(text="字"*1600)])


def test_text_limit_includes_goodbye():
    budget = 1500 - len("出门别忘了带手机和钥匙。提醒你：。")
    assert len(text_for([item(text="字"*budget)])) == 1500
    with pytest.raises(ValueError, match="1500"):
        text_for([item(text="字"*(budget+1))])


async def test_text_only_never_reads_or_completes_todos_and_survives_restart(rig, monkeypatch):
    manager, store, ha, mqtt, tts = rig
    manager.configure(DepartureSettings(text="  出门顺利！  ", include_todos=False))
    ha.connected = False
    monkeypatch.setattr(ha, "items", AsyncMock(side_effect=AssertionError("text-only read to-do")))
    monkeypatch.setattr(ha, "complete", AsyncMock(side_effect=AssertionError("text-only completed to-do")))
    await prepare(rig)
    manager.last_sync = time.time()-60
    assert manager.ready
    assert manager.text == "出门顺利！" and manager.items == []
    manager.play(event(store))
    row = store.latest_playback()
    assert row["items"] == []
    manager.audio_status(ack(row))
    restarted = ReminderManager(store, ha, mqtt, tts, manager.config)
    await restarted.reconcile()
    assert store.playback(row["command_id"])["state"] == "complete"
    assert restarted.settings == manager.settings
    ha.items.assert_not_awaited()
    ha.complete.assert_not_awaited()


async def test_departure_edit_discards_inflight_audio_and_stale_device_receipts(rig):
    manager, _, _, _, tts = rig
    await prepare(rig)
    old_command, old_version = manager.cache_command, manager.audio["version"]
    started, release = asyncio.Event(), asyncio.Event()
    real_prepare = tts.prepare

    async def slow(text, force=False):
        started.set()
        await release.wait()
        return await real_prepare(text, force=force)

    tts.prepare = slow
    manager.configure(DepartureSettings(text="第一版。"))
    task = asyncio.create_task(manager.sync())
    await started.wait()
    manager.configure(DepartureSettings(text="第二版。", include_todos=False))
    release.set()
    await task
    assert manager.audio is None and not manager.ready
    await manager.sync()
    assert manager.text == "第二版。" and manager.items == []
    manager.audio_status({"command_id": old_command, "version": old_version,
                          "audio_id": "departure", "status": "ready"})
    assert not manager.ready
    manager.audio_status({"command_id": manager.cache_command, "version": manager.audio["version"],
                          "audio_id": "departure", "status": "ready"})
    assert manager.ready


async def test_disabling_departure_prevents_fallback_and_todo_reads(rig, monkeypatch):
    manager, store, ha, _, _ = rig
    await prepare(rig)
    manager.configure(DepartureSettings(enabled=False))
    monkeypatch.setattr(ha, "items", AsyncMock(side_effect=AssertionError("disabled read to-do")))
    await manager.sync()
    assert not manager.ready and manager.audio is None
    assert manager.play(event(store)) == "disabled"
    assert not store.playbacks()


async def test_custom_text_with_todos_and_total_limit(rig):
    manager, _, ha, _, _ = rig
    manager.configure(DepartureSettings(text="一路顺风"))
    await prepare(rig)
    assert manager.text == "一路顺风。提醒你：带钥匙。"
    manager.configure(DepartureSettings(text="字"*1500))
    with pytest.raises(ValueError, match="1500"):
        await manager.sync()
    assert manager.audio is None and not manager.ready and not ha.completed


async def test_departure_telemetry_does_not_publish_editable_text(rig):
    manager, _, _, mqtt, _ = rig
    manager.configure(DepartureSettings(text="私人提醒。"))
    await prepare(rig)
    manager.publish_status()
    topic, payload, options = mqtt.messages[-1]
    assert topic == "reminder/status" and options["retain"]
    assert not {"items", "text", "settings"}.intersection(payload)
