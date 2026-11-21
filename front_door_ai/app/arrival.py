"""Welcome audio shares transport/transactions, but never reads or completes to-dos."""
from __future__ import annotations

import time

from .config import ArrivalSettings
from .reminder import ReminderManager


class ArrivalManager(ReminderManager):
    settings_model = ArrivalSettings
    settings_key = "arrival_settings"
    audio_id = "arrival"
    event_type = "enter"
    play_command = "play_arrival"
    fallback_sound = "welcome"

    @property
    def ready(self):
        return bool(self.enabled and self.audio and self.device_online and self.mqtt.connected
                    and self.device_version == self.audio["version"] and not self.invalidated)

    def status(self):
        value = super().status()
        value["last_arrival_playback"] = value.pop("last_reminder_playback")
        return value

    async def _sync(self):
        revision = self.revision
        if not self.enabled:
            self.audio = self.device_version = self.cache_command = None
            self.invalidated, self.error = False, None
            return
        if self.audio and not self.invalidated and not self.force:
            self.ensure_cache()
            return
        force, self.force = self.force, False
        old = self.audio
        try:
            audio = await self.prepare_audio(self.settings.text, force=force)
        except Exception:
            self.audio = self.device_version = None
            raise
        if revision != self.revision:
            self.wake.set()
            return
        self.audio, self.invalidated, self.error = audio, False, None
        self.last_sync = time.time()
        if old != audio:
            self.device_version = self.cache_command = None
        self.ensure_cache()

    async def reconcile(self):
        for row in self.store.playbacks():
            if row["audio_id"] == self.audio_id and row["state"] in {"finished", "completing"}:
                self.store.update_playback(row["command_id"], "complete")
