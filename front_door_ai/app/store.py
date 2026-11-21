from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    """Only the asyncio owner accesses SQLite; workers return their results to it."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("数据库版本高于程序版本，禁止降级写入")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS playback (
                command_id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                version TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
                state TEXT NOT NULL, items TEXT NOT NULL, completed TEXT NOT NULL DEFAULT '[]');
        """)
        with self.db:
            if "audio_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(playback)")}:
                self.db.execute("ALTER TABLE playback ADD COLUMN audio_id TEXT NOT NULL DEFAULT 'departure'")
            self.db.execute("PRAGMA user_version=2")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def add_event(self, event):
        with self.db:
            self.db.execute("INSERT INTO events VALUES (?,?,?)", (
                event["event_id"], event["created"], json.dumps(event),
            ))

    def event(self, event_id):
        row = self.db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def events(self, limit=100):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM events ORDER BY created DESC LIMIT ?", (limit,),
        )]

    def prune_events(self, days, maximum):
        rows = self.db.execute("SELECT id,created FROM events ORDER BY created DESC").fetchall()
        removed = [row["id"] for i, row in enumerate(rows) if i >= maximum or row["created"] < time.time()-days*86400]
        with self.db:
            self.db.executemany("DELETE FROM events WHERE id=?", [(key,) for key in removed])
        return removed

    def create_playback(self, command_id, event_id, version, items, deadline, state="requested", audio_id="departure"):
        with self.db:
            self.db.execute("INSERT INTO playback VALUES (?,?,?,?,?,?,?,?,?)", (
                command_id, event_id, version, time.time(), deadline, state, json.dumps(items), "[]", audio_id,
            ))

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        value["items"] = json.loads(value["items"])
        value["completed"] = json.loads(value["completed"])
        return value

    def playback(self, command_id):
        return self.decode(self.db.execute("SELECT * FROM playback WHERE command_id=?", (command_id,)).fetchone())

    def seen_event(self, event_id):
        return self.db.execute("SELECT 1 FROM playback WHERE event_id=?", (event_id,)).fetchone() is not None

    def playbacks(self):
        return [self.decode(row) for row in self.db.execute("SELECT * FROM playback ORDER BY created")]

    def latest_playback(self, audio_id="departure"):
        return self.decode(self.db.execute(
            "SELECT * FROM playback WHERE state!='skipped' AND audio_id=? ORDER BY created DESC LIMIT 1",
            (audio_id,),
        ).fetchone())

    def playback_busy(self, now=None):
        now = time.time() if now is None else now
        self.expire_playbacks(now)
        return any(row["state"] in {"requested", "playing", "finished", "completing"}
                   or (row["state"] == "fallback" and row["deadline"] > now)
                   for row in self.playbacks())

    def update_playback(self, command_id, state, completed=None):
        with self.db:
            if completed is None:
                self.db.execute("UPDATE playback SET state=? WHERE command_id=?", (state, command_id))
            else:
                self.db.execute("UPDATE playback SET state=?,completed=? WHERE command_id=?", (
                    state, json.dumps(completed), command_id,
                ))

    def expire_playbacks(self, now):
        with self.db:
            self.db.execute("UPDATE playback SET state='timed_out' WHERE state IN ('requested','playing') AND deadline<?", (now,))
            self.db.execute("UPDATE playback SET state='superseded' WHERE state='timed_out' AND created<?", (now-86400,))
            self.db.execute("DELETE FROM playback WHERE state IN ('complete','failed','superseded','fallback','skipped') AND created<?", (now-7*86400,))

    def close(self):
        self.db.close()
