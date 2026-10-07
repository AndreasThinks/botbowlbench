"""SQLite persistence for models, tournaments, matches and the live event feed."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    config TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- round_robin | gauntlet
    name TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | running | completed | cancelled
    focus_model TEXT,                -- the newly added model for a gauntlet
    settings TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
    seq INTEGER NOT NULL,
    home_model TEXT NOT NULL REFERENCES models(id),
    away_model TEXT NOT NULL REFERENCES models(id),
    status TEXT NOT NULL,            -- queued | running | completed | error | cancelled
    home_score INTEGER,
    away_score INTEGER,
    winner TEXT,                     -- home | away | draw
    home_stats TEXT,
    away_stats TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    duration REAL,
    frames INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status, tournament_id, seq);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    ts REAL NOT NULL,
    side TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_match ON events(match_id, id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_local = threading.local()
_DB_PATH = None


# columns added after the first release; created on start-up for existing databases
MIGRATIONS = {
    "matches": [("seed", "INTEGER"), ("meta", "TEXT"), ("admissible", "INTEGER")],
}


def init(path: str):
    global _DB_PATH
    _DB_PATH = path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, typ in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def _connect():
    c = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def conn():
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _DB_PATH:
        c = _connect()
        _local.conn = c
        _local.path = _DB_PATH
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def rows(sql: str, args=()) -> List[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def row(sql: str, args=()) -> Optional[dict]:
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql: str, args=()) -> int:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


# ---- meta -------------------------------------------------------------------------------------
def get_meta(key, default=None):
    r = row("SELECT value FROM meta WHERE key=?", (key,))
    return default if r is None else r["value"]


def set_meta(key, value):
    execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


# ---- events -----------------------------------------------------------------------------------
def add_event(match_id: str, side: Optional[str], kind: str, payload: dict):
    execute("INSERT INTO events(match_id, ts, side, kind, payload) VALUES(?,?,?,?,?)",
            (match_id, time.time(), side, kind, json.dumps(payload, default=str)))


def get_events(match_id: str, after: int = 0, limit: int = 500) -> List[dict]:
    out = rows("SELECT id, ts, side, kind, payload FROM events WHERE match_id=? AND id>? ORDER BY id LIMIT ?",
               (match_id, after, limit))
    for e in out:
        e["payload"] = json.loads(e["payload"])
    return out


# ---- decoding helpers ---------------------------------------------------------------------------
def decode_match(m: Optional[dict]) -> Optional[dict]:
    if m is None:
        return None
    for k in ("home_stats", "away_stats", "meta"):
        if m.get(k):
            m[k] = json.loads(m[k])
    return m
