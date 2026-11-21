import sqlite3
import time
import uuid

import pytest

from app.store import Store


def test_migrate_v1_preserves_departure_transactions(tmp_path):
    path = tmp_path/"old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE playback (command_id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                version TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
                state TEXT NOT NULL, items TEXT NOT NULL, completed TEXT NOT NULL DEFAULT '[]');
            INSERT INTO playback VALUES ('cmd','evt','version',1,9,'finished','[]','[]');
            PRAGMA user_version=1;
        """)
    store = Store(path)
    try:
        assert store.playback("cmd")["audio_id"] == "departure"
        assert store.playback("cmd")["state"] == "finished"
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.latest_playback("arrival") is None
    finally:
        store.close()


def test_future_database_refused(tmp_path):
    path = tmp_path/"future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=3")
    with pytest.raises(RuntimeError):
        Store(path)


def test_retention_enforces_age_and_count_together(tmp_path):
    store = Store(tmp_path/"events.sqlite3")
    try:
        keys = []
        for age in (9*86400, 3, 2, 1):
            key = str(uuid.uuid4())
            keys.append(key)
            store.add_event({"event_id": key, "created": time.time()-age})
        assert set(store.prune_events(7, 2)) == set(keys[:2])
        assert {event["event_id"] for event in store.events()} == set(keys[2:])
    finally:
        store.close()
